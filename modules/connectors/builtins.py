"""Built-in data sources: one-toggle enable/disable of catalog presets that need no owner scope or key.

A built-in is a ``BUILTIN_PROVIDERS`` preset (fixed endpoint, no API key, adapter registered). The Source
row is created lazily on first enable (no seed migration) and reused afterwards; disabling only pauses it
so Source, terms and history survive. Enable is a resumable sequence of the same helpers the manual flow
uses (each commits itself), so a half-finished enable is finished by simply toggling again. The owner's
toggle is the terms acknowledgement; for review-class providers the single-user instance operator's toggle
additionally records the operator review evidence. Nothing here performs network I/O.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import is_instance_operator
from core.database import get_session
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.connectors import provider_terms, provisioning, registry, scheduler
from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.provider_specs import (
    BUILTIN_PROVIDERS,
    FREE_PROVIDER_SPECS,
    TERMS_INELIGIBLE,
    FreeProviderSpec,
    deployment_use_matches,
    get_provider_spec,
)
from modules.connectors.provisioning_routes import ActivationRequest, _owner_access, activate_native
from modules.connectors.public import (
    ConnectorConfig,
    save_connector_configuration,
    serialize_source_configuration,
)
from modules.settings.public import module_dependency
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceCreate, SourcePatch

router = APIRouter(prefix="/api/v1/connectors/builtins", tags=["connectors"],
                   dependencies=[Depends(module_dependency("connectors"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]

_USES_WITH_REVIEW = ("personal", "noncommercial", "commercial")


class BuiltinSourceRead(BaseModel):
    """One built-in preset with the owner's current toggle state; ``source_id`` is None until first enabled."""
    id: str
    label: str
    category: str
    eligibility: str
    terms_url: str
    terms_checked_on: str
    attribution: str
    execution: str
    source_id: UUID | None
    enabled: bool


class BuiltinEnableRequest(BaseModel):
    """Owner input for the enable toggle: the deployment use declared with the terms acknowledgement."""
    model_config = ConfigDict(extra="forbid")
    declared_use: Literal["personal", "noncommercial", "commercial", "unknown"] = "personal"


_DEFAULT_ENABLE = BuiltinEnableRequest()  # body is optional; the model is never mutated


def _spec(provider: str) -> FreeProviderSpec:
    """Return the built-in spec, or 404 for any provider outside ``BUILTIN_PROVIDERS`` (scoped/keyed stay manual)."""
    spec = get_provider_spec(provider)
    if spec is None or provider not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=404, detail="Built-in source not found")
    return spec


def _read(spec: FreeProviderSpec, source: ConnectorSource | None, active: bool) -> BuiltinSourceRead:
    """Project a spec plus its (optional) Source and activation flag into the owner-facing row."""
    return BuiltinSourceRead(
        id=spec.id, label=spec.label, category=spec.category, eligibility=spec.eligibility,
        terms_url=spec.terms_url, terms_checked_on=spec.checked_on, attribution=spec.attribution,
        execution=spec.execution, source_id=source.id if source is not None else None, enabled=active,
    )


def _is_enabled(source: ConnectorSource, row: object | None) -> bool:
    """A built-in is on only when its Source is active and the connector row is active and current."""
    return bool(
        row is not None and source.status == "active" and getattr(row, "state", None) == "active"
        and getattr(row, "desired_enabled", False) and getattr(row, "applied_revision", -1) == getattr(row, "desired_revision", -2)
        and getattr(row, "source_generation", -1) == source.generation
    )


async def _oldest_source(session: AsyncSession, provider: str, *, scope: WorkspaceContext, gate: bool):  # type: ignore[no-untyped-def]
    """Oldest non-archived Source ORM row for the provider, or None."""
    rows = await sources.list_provider_sources(session, frozenset({provider}), scope=scope, multi_workspace_enabled=gate)
    return rows[0] if rows else None


async def _state(session: AsyncSession, spec: FreeProviderSpec, *, scope: WorkspaceContext, gate: bool) -> BuiltinSourceRead:
    """Read one built-in's toggle state from the current Source and connector row."""
    found = await _oldest_source(session, spec.id, scope=scope, gate=gate)
    if found is None:
        return _read(spec, None, False)
    source = await sources.get_connector_source(session, found.id, scope=scope, multi_workspace_enabled=gate)
    if source is None:
        return _read(spec, None, False)
    row = await provisioning.activation_status(session, source.id, scope=scope, multi_workspace_enabled=gate)
    return _read(spec, source, _is_enabled(source, row))


@router.get("", response_model=list[BuiltinSourceRead])
async def list_builtins(session: Session, request: Request, _owner: OwnerRead) -> list[BuiltinSourceRead]:
    """List every built-in preset with its toggle state for the owner workspace.

    Owner-only (members have no Source access). Read only: no locks, no Source creation.
    """
    gate = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, _owner)
    return [
        await _state(session, spec, scope=_owner, gate=gate)
        for spec in FREE_PROVIDER_SPECS if spec.id in BUILTIN_PROVIDERS
    ]


@router.post("/{provider}/enable", response_model=BuiltinSourceRead)
async def enable_builtin(
    provider: str, session: Session, request: Request, _owner: OwnerWrite,
    payload: Annotated[BuiltinEnableRequest, Body()] = _DEFAULT_ENABLE,
) -> BuiltinSourceRead:
    """Turn a built-in on: create/reuse its Source, save settings, acknowledge terms, review, activate.

    Idempotent and resumable: every step is skipped when already satisfied, so a repeat call after a
    failure continues where it stopped. 404 outside ``BUILTIN_PROVIDERS``; 409 ``provider_terms_ineligible``
    when the declared use cannot satisfy the catalog class; 403 ``operator_required`` for a review-class
    provider unless the caller is the bootstrap instance operator (single-user: the owner). Owner-write,
    CSRF and access-fence admission apply; the terms acknowledgement and operator evidence are written
    only here, from this explicit owner action.
    """
    spec = _spec(provider)
    scope, gate = _owner, request.app.state.settings.multi_workspace_enabled
    use = payload.declared_use
    review_class = spec.eligibility == "review"
    # Reject before creating anything so a refused toggle leaves no half-built Source behind.
    if review_class:
        if use not in _USES_WITH_REVIEW:
            raise HTTPException(status_code=409, detail=TERMS_INELIGIBLE)
        if not is_instance_operator(request.state.auth_session):
            raise HTTPException(status_code=403, detail="operator_required")
    elif not deployment_use_matches(spec.eligibility, use):
        raise HTTPException(status_code=409, detail=TERMS_INELIGIBLE)
    fence = await _owner_access(session, request, scope)

    # 1-2. Source: reuse the oldest one, un-pause it, else create it.
    found = await _oldest_source(session, provider, scope=scope, gate=gate)
    await session.rollback()
    if found is None:
        found = await sources.create_source(
            session, SourceCreate(type=PROVIDER_SOURCE_TYPES[provider], name=spec.label, provider=provider),
            scope=scope, multi_workspace_enabled=gate, expected_access_fence=fence)
    elif found.status == "paused":
        await sources.update_source(
            session, found, SourcePatch(status="active"), scope=scope, multi_workspace_enabled=gate,
            expected_access_fence=fence)
    source = await sources.get_connector_source(session, found.id, scope=scope, multi_workspace_enabled=gate)
    if source is None or source.status != "active":
        raise HTTPException(status_code=409, detail="Source is inactive; it cannot be enabled")

    # 3. Desired settings: only rewritten when different, because every save bumps the revision and
    # demotes an active connector. A paused->active Source has a new generation, so it must be re-saved.
    config = ConnectorConfig(
        timeout_seconds=30, history_mode="returned_snapshot",
        schedule_interval_minutes=spec.default_interval_minutes)
    try:
        source_configuration = serialize_source_configuration(source, config)
        registry.validate(source.model_copy(update={"configuration": source_configuration}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Provider configuration is invalid") from exc
    desired = {**source_configuration, "auth_method": "none"}
    row = await provisioning.activation_status(session, source.id, scope=scope, multi_workspace_enabled=gate)
    if row is None or row.desired_configuration != desired or row.source_generation != source.generation:
        await session.rollback()
        fence = await _owner_access(session, request, scope, expected=fence)
        saved = await save_connector_configuration(
            session, source, row.desired_revision if row is not None else 0, source_configuration, desired,
            scope=scope, multi_workspace_enabled=gate)
        if saved is None:
            raise HTTPException(status_code=409, detail="Source or connector revision changed while saving")
        saved_source, row = saved
        row.state = "saved_not_active"
        await commit_with_replay(session, [
            make_source_change(saved_source.id, saved_source.generation, saved_source.status,
                               connector_state=row.state, scope=scope),
        ], access_fence=fence, multi_workspace_enabled=gate, scope=scope)
        source = await sources.get_connector_source(session, source.id, scope=scope, multi_workspace_enabled=gate)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")

    # 4-5. Terms: the owner toggle is the acknowledgement; review-class adds operator evidence for this version.
    await session.rollback()
    terms = await provider_terms.acknowledge_terms(
        session, source.id, provider_terms.ProviderTermsAcknowledge(declared_use=use, terms_version=spec.checked_on),
        scope=scope, multi_workspace_enabled=gate)
    if review_class and not terms.eligible:
        terms = await provider_terms.record_operator_review(
            session, workspace_id=scope.workspace_id, source_id=source.id,
            reviewer_user_id=request.state.auth_session.owner_id,
            body=provider_terms.OperatorTermsReview(
                state="approved", allowed_use=use,
                evidence_ref=f"builtin-toggle:{spec.id}:{spec.terms_url}@{spec.checked_on}",
                reviewed_terms_version=spec.checked_on),
            instance_operator=True)
    await scheduler.clear_collection_block(
        session, source.id, terms_revision=terms.terms_revision, workspace_id=scope.workspace_id)
    await session.commit()

    # 6. Activate natively unless it is already on at the current revision.
    row = await provisioning.activation_status(session, source.id, scope=scope, multi_workspace_enabled=gate)
    if row is None:
        raise HTTPException(status_code=409, detail="No connector provisioning state exists")
    if not _is_enabled(source, row):
        await session.rollback()
        fence = await _owner_access(session, request, scope, expected=fence)
        await activate_native(
            session, request, scope, source, ActivationRequest(expected_revision=row.desired_revision),
            access_fence=fence)
    return await _state(session, spec, scope=scope, gate=gate)


@router.post("/{provider}/disable", response_model=BuiltinSourceRead)
async def disable_builtin(provider: str, session: Session, request: Request, _owner: OwnerWrite) -> BuiltinSourceRead:
    """Turn a built-in off by pausing its Source (same cleanup as ``deactivate_source``).

    Idempotent: a never-enabled or already-paused built-in just returns its state. The Source, terms
    acknowledgement and collected history are kept, so a later enable reuses them.
    """
    spec = _spec(provider)
    scope, gate = _owner, request.app.state.settings.multi_workspace_enabled
    fence = await _owner_access(session, request, scope)
    found = await _oldest_source(session, provider, scope=scope, gate=gate)
    if found is None or found.status == "paused":
        return await _state(session, spec, scope=scope, gate=gate)
    source = await sources.pause_source_for_connector(session, found.id, scope=scope, multi_workspace_enabled=gate)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    row = await provisioning.activation_status(session, source.id, scope=scope, multi_workspace_enabled=gate)
    state = row.state if row is not None else None
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status, connector_state=state, scope=scope),
    ], access_fence=fence, multi_workspace_enabled=gate, scope=scope)
    return await _state(session, spec, scope=scope, gate=gate)
