"""Owner-facing persistence and request fences for retention and module availability."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from core.modules import effective_modules, register_modules
from modules.settings.models import ModuleLifecycleRecord, RetentionSettingsRecord
from modules.settings.schemas import (
    ModuleLifecycleEntry,
    ModuleLifecycleRead,
    ModuleLifecycleUpdate,
    RetentionSettingsRead,
    RetentionSettingsUpdate,
)

OWNER_ID = 1
MODULES = register_modules()
OWNER_READ = Annotated[AuthSession, Depends(require_owner)]
OWNER_WRITE = Annotated[AuthSession, Depends(require_owner_write)]

_ALWAYS_AVAILABLE_ROUTES = frozenset({
    ("DELETE", "/api/v1/sources/{source_id}"),
    ("DELETE", "/api/v1/documents/{document_id}"),
    ("DELETE", "/api/v1/conversations/{conversation_id}"),
    ("DELETE", "/api/v1/memories/{memory_id}"),
    ("GET", "/api/v1/settings/memory-privacy"),
    ("PUT", "/api/v1/settings/memory-privacy"),
    ("POST", "/api/v1/settings/memory-privacy/purge"),
    ("POST", "/api/v1/agent-runs/{run_id}/cancel"),
    ("POST", "/api/v1/responses/{response_id}/cancel"),
    ("POST", "/api/v1/approvals/{approval_id}/approve"),
    ("POST", "/api/v1/approvals/{approval_id}/deny"),
    ("POST", "/api/v1/automations/runs/{run_id}/actions/{ordinal}/decision"),
    ("POST", "/api/v1/mcp/connections/{connection_id}/disable"),
    ("POST", "/api/v1/mcp/inbound-clients/{client_id}/revoke"),
    ("GET", "/api/v1/system/maintenance"),
})


async def get_disabled_modules(session: AsyncSession) -> tuple[set[str], int, bool]:
    """Read the persisted explicit disable set, revision, and first-write state."""
    row = await session.get(ModuleLifecycleRecord, OWNER_ID)
    if row is None:
        return set(), 1, False
    values = row.disabled_modules if isinstance(row.disabled_modules, list) else []
    return {value for value in values if isinstance(value, str)}, row.configuration_revision, True


async def module_is_enabled(session: AsyncSession, module_id: str) -> bool:
    """Read current dependency-derived availability for one registered module."""
    state = await read_module_lifecycle(session)
    return next((item.enabled for item in state.modules if item.id == module_id), False)


async def read_module_lifecycle(session: AsyncSession) -> ModuleLifecycleRead:
    """Return descriptor availability after applying persisted disables through dependency closure."""
    disabled, revision, persisted = await get_disabled_modules(session)
    enabled = effective_modules(disabled, MODULES)
    entries = []
    for module_id, descriptor in MODULES.items():
        blocked = [dependency for dependency in descriptor.dependencies
                   if dependency not in enabled or not enabled[dependency].enabled]
        if not descriptor.enabled:
            blocked.append(module_id)
        entries.append(ModuleLifecycleEntry(
            id=module_id,
            name=descriptor.name,
            enabled=enabled[module_id].enabled,
            explicitly_disabled=module_id in disabled,
            dependencies=list(descriptor.dependencies),
            blocked_by=blocked,
            tools=list(descriptor.tools),
            scheduled_jobs=list(getattr(descriptor, "scheduled_jobs", ())),
            navigation=[dict(item) for item in descriptor.navigation],
        ))
    return ModuleLifecycleRead(configuration_revision=revision, persisted=persisted, modules=entries)


async def save_module_lifecycle(
    session: AsyncSession, update: ModuleLifecycleUpdate,
) -> ModuleLifecycleRead:
    """Persist one requested module state with a CAS revision and dependency-aware enable validation."""
    descriptor = MODULES.get(update.module_id)
    if descriptor is None:
        raise HTTPException(status_code=404, detail="Unknown module")
    if not descriptor.enabled:
        raise HTTPException(status_code=409, detail="This module is not available in this build")
    await session.execute(
        insert(ModuleLifecycleRecord).values(owner_id=OWNER_ID)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(select(ModuleLifecycleRecord).where(
        ModuleLifecycleRecord.owner_id == OWNER_ID,
    ).with_for_update())
    if row is None:
        raise RuntimeError("Module lifecycle singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="Module settings changed; reload before saving")
    disabled = {value for value in row.disabled_modules if isinstance(value, str)}
    if update.enabled:
        disabled.discard(update.module_id)
        resulting = effective_modules(disabled, MODULES)
        blocked = [dependency for dependency in descriptor.dependencies
                   if dependency not in resulting or not resulting[dependency].enabled]
        if blocked:
            raise HTTPException(status_code=409, detail=f"Enable dependencies first: {', '.join(blocked)}")
    else:
        disabled.add(update.module_id)
    row.disabled_modules = sorted(disabled)
    row.configuration_revision += 1
    await session.flush()
    return await read_module_lifecycle(session)


async def read_retention_settings(session: AsyncSession) -> RetentionSettingsRead:
    """Return configured agent trace retention with the immutable raw-source/history policy."""
    row = await session.get(RetentionSettingsRecord, OWNER_ID)
    if row is None:
        return RetentionSettingsRead(configuration_revision=1, persisted=False, agent_trace_days=90)
    return RetentionSettingsRead(
        configuration_revision=row.configuration_revision, persisted=True, agent_trace_days=row.agent_trace_days,
    )


async def save_retention_settings(
    session: AsyncSession, update: RetentionSettingsUpdate,
) -> RetentionSettingsRead:
    """Save owner trace retention using a locked singleton and revision fence."""
    await session.execute(
        insert(RetentionSettingsRecord).values(owner_id=OWNER_ID)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(select(RetentionSettingsRecord).where(
        RetentionSettingsRecord.owner_id == OWNER_ID,
    ).with_for_update())
    if row is None:
        raise RuntimeError("Retention settings singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="Retention settings changed; reload before saving")
    row.agent_trace_days = update.agent_trace_days
    row.configuration_revision += 1
    await session.flush()
    return await read_retention_settings(session)


async def _require_owner_for_module(request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> AuthSession:
    """Complete normal owner or owner-write authorization before evaluating module availability."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return await require_owner(request, session)
    origin = request.headers.get("origin")
    csrf = request.headers.get("x-csrf-token")
    return await require_owner_write(request, session, origin, csrf)


def module_dependency(module_id: str) -> Callable[..., Awaitable[None]]:
    """Build a router dependency that checks owner authorization before the persisted module gate."""
    async def require_enabled_module(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
        _owner: Annotated[AuthSession, Depends(_require_owner_for_module)],
    ) -> None:
        """Refresh this process's tool descriptor state and deny only after owner/CSRF validation."""
        value = await read_module_lifecycle(session)
        disabled = {item.id for item in value.modules if item.explicitly_disabled}
        current = effective_modules(disabled, MODULES)
        request.app.state.modules = current
        tool_registry = getattr(request.app.state, "tool_registry", None)
        if tool_registry is not None:
            tool_registry.set_module_registry(current)
        # Destructive privacy cleanup and owner cancellation/revocation paths remain callable
        # so disabled modules cannot strand canonical records or live security authority.
        route = request.scope.get("route")
        route_path = getattr(route, "path", "")
        if (request.method, route_path) in _ALWAYS_AVAILABLE_ROUTES:
            return
        descriptor = current.get(module_id)
        if descriptor is None or not descriptor.enabled:
            raise HTTPException(status_code=404, detail="Module is disabled or unavailable")
    return require_enabled_module


def sync_application_modules(app: FastAPI, state: ModuleLifecycleRead) -> None:
    """Apply the persisted effective descriptor set to this API process and its native registry."""
    disabled = {item.id for item in state.modules if item.explicitly_disabled}
    current = effective_modules(disabled, MODULES)
    app.state.modules = current
    tool_registry = getattr(app.state, "tool_registry", None)
    if tool_registry is not None:
        tool_registry.set_module_registry(current)
