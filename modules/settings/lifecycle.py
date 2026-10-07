"""Owner-facing persistence and request fences for retention and module availability."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account, require_account_write, require_owner, require_owner_write
from core.auth.schemas import AccountSessionRef
from core.database import get_session
from core.modules import effective_modules, register_modules
from core.workspaces.dependencies import (
    require_default_workspace_read, require_default_workspace_write,
    require_workspace_read, require_workspace_write,
)
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import Scope, WorkspaceContext
from modules.settings.public import require_settings_scope, scope_actor
from modules.settings.models import ModuleLifecycleRecord, RetentionSettingsRecord
from modules.settings.schemas import (
    ModuleLifecycleEntry,
    ModuleLifecycleRead,
    ModuleLifecycleUpdate,
    RetentionSettingsRead,
    RetentionSettingsUpdate,
)

MODULES = register_modules()
# Instance controls use bootstrap operator admission and static build capabilities;
# they cannot be disabled by one workspace's persisted settings.
_INSTANCE_MODULES = frozenset({"backup", "observability"})

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


async def get_disabled_modules(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> tuple[set[str], int, bool]:
    """Read scoped owner disables/revision, ignoring legacy instance controls before closure.

    Explicit configured admission remains mandatory; reads do not rewrite persisted IDs.
    """
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(select(ModuleLifecycleRecord).where(
        ModuleLifecycleRecord.workspace_id == scope.workspace_id, ModuleLifecycleRecord.owner_id == scope_actor(scope),
    ).execution_options(populate_existing=True))
    if row is None:
        return set(), 1, False
    values = row.disabled_modules if isinstance(row.disabled_modules, list) else []
    return {value for value in values if isinstance(value, str) and value not in _INSTANCE_MODULES}, row.configuration_revision, True


async def module_is_enabled(session: AsyncSession, module_id: str, *, scope: Scope, multi_workspace_enabled: bool) -> bool:
    """Read admitted owner workspace availability for one module; unknown IDs deny."""
    state = await read_module_lifecycle(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return next((item.enabled for item in state.modules if item.id == module_id), False)


async def read_module_lifecycle(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> ModuleLifecycleRead:
    """Return workspace-editable owner DTO with full dependency closure and actual configured gate.

    Instance descriptors are excluded from the response but retained as static build
    capabilities when resolving dependencies. No global registry or other workspace mutates.
    """
    disabled, revision, persisted = await get_disabled_modules(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    enabled = effective_modules(disabled, MODULES)
    entries = []
    for module_id, descriptor in MODULES.items():
        if module_id in _INSTANCE_MODULES:
            continue
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
    *, scope: Scope, multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> ModuleLifecycleRead:
    """Persist owner workspace module choice after auth/access locks with CAS and dependency checks.

    Caller supplies actual gate and exact HTTP session where applicable, then commits; no
    process registry updates occur. Instance toggles fail422 before settings mutation;
    unavailable dependencies fail409. A valid workspace save drops legacy instance IDs.
    """
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                                 locked=True, auth_sessions=auth_sessions)
    descriptor = MODULES.get(update.module_id)
    if descriptor is None:
        raise HTTPException(status_code=404, detail="Unknown module")
    if update.module_id in _INSTANCE_MODULES:
        raise HTTPException(status_code=422, detail="Instance modules cannot be changed through workspace settings")
    if not descriptor.enabled:
        raise HTTPException(status_code=409, detail="This module is not available in this build")
    await session.execute(
        insert(ModuleLifecycleRecord).values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(select(ModuleLifecycleRecord).where(
        ModuleLifecycleRecord.owner_id == scope_actor(scope), ModuleLifecycleRecord.workspace_id == scope.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise RuntimeError("Module lifecycle singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="Module settings changed; reload before saving")
    disabled = {value for value in row.disabled_modules if isinstance(value, str) and value not in _INSTANCE_MODULES}
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
    return await read_module_lifecycle(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def read_retention_settings(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> RetentionSettingsRead:
    """Return owner-only scoped trace retention/default90d; raw-source/history always retained.

    Actual gate is mandatory; maintenance caller applies this DTO only to this workspace.
    """
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(select(RetentionSettingsRecord).where(
        RetentionSettingsRecord.owner_id == scope_actor(scope), RetentionSettingsRecord.workspace_id == scope.workspace_id,
    ).execution_options(populate_existing=True))
    if row is None:
        return RetentionSettingsRead(configuration_revision=1, persisted=False, agent_trace_days=90)
    return RetentionSettingsRead(
        configuration_revision=row.configuration_revision, persisted=True, agent_trace_days=row.agent_trace_days,
    )


async def save_retention_settings(
    session: AsyncSession, update: RetentionSettingsUpdate,
    *, scope: Scope, multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> RetentionSettingsRead:
    """Save workspace trace retention after auth/access locks with CAS; caller commits."""
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                                 locked=True, auth_sessions=auth_sessions)
    await session.execute(
        insert(RetentionSettingsRecord).values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(select(RetentionSettingsRecord).where(
        RetentionSettingsRecord.owner_id == scope_actor(scope), RetentionSettingsRecord.workspace_id == scope.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise RuntimeError("Retention settings singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="Retention settings changed; reload before saving")
    row.agent_trace_days = update.agent_trace_days
    row.configuration_revision += 1
    await session.flush()
    return await read_retention_settings(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _require_owner_for_module(request: Request, session: AsyncSession, *, module_id: str) -> WorkspaceContext | None:
    """Prepare explicit domain identity; retain operator authentication for instance modules.

    Historical name is retained without bootstrap authority for ordinary domains. Member
    reads still need domain grants. Writes retain actual account CSRF/backup admission.
    """
    if module_id in _INSTANCE_MODULES:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            await require_owner(request, session)
        else:
            await require_owner_write(request, session, request.headers.get("origin"), request.headers.get("x-csrf-token"))
        return None
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        auth = await require_account(request, session)
        if module_id in {"chat", "memory", "agents"}:
            return await require_default_workspace_read(request, auth, session, request.headers.get("X-Workspace-ID"))
        return await require_workspace_read(request, auth, session, request.headers.get("X-Workspace-ID"))
    auth = await require_account_write(request, session, request.headers.get("origin"), request.headers.get("x-csrf-token"))
    if module_id in {"chat", "memory", "agents"}:
        return await require_default_workspace_write(request, auth, session, request.headers.get("X-Workspace-ID"))
    return await require_workspace_write(request, auth, session, request.headers.get("X-Workspace-ID"))


def module_dependency(module_id: str) -> Callable[..., Awaitable[None]]:
    """Build a scoped request availability gate; domain owner still authorizes content/grants.

    Instance Backup/Observability retain operator auth and static build availability.
    Legacy workspace disables for them are ignored before dependency closure. No current-workspace registry or
    process tools mutation occurs. Cleanup/cancel/revoke routes bypass disable only after
    normal scope/authentication/CSRF admission.
    """
    async def require_enabled_module(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> None:
        """Check request availability excluding instance disables; expose no owner settings to members."""
        scope = await _require_owner_for_module(request, session, module_id=module_id)
        disabled: set[str] = set()
        if scope is not None:
            await read_access_fence(session, scope=scope,
                                    multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
            # Internal workspace-only projection grants no Settings DTO/secret access and
            # includes the owner's single lifecycle row even for a member resource route.
            row = await session.scalar(select(ModuleLifecycleRecord).where(
                ModuleLifecycleRecord.workspace_id == scope.workspace_id,
            ).execution_options(populate_existing=True))
            disabled = {value for value in row.disabled_modules
                        if isinstance(value, str) and value not in _INSTANCE_MODULES} if row else set()
        current = effective_modules(disabled, MODULES)
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


def sync_application_modules(app: FastAPI) -> None:
    """Initialize process build capabilities only; workspace lifecycle is a request/job gate.

    I-runtime must migrate startup call to this signature. No owner settings argument may
    replace this process registry; tools must apply explicit admitted scope per dispatch.
    """
    current = register_modules()
    app.state.modules = current
    tool_registry = getattr(app.state, "tool_registry", None)
    if tool_registry is not None:
        tool_registry.set_module_registry(current)
