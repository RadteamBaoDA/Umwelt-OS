"""Built-in data sources: one-toggle enable/disable of catalog presets that need no owner scope or key.

A built-in is a ``BUILTIN_PROVIDERS`` preset (fixed endpoint, no API key, adapter registered). The Source
row is created lazily on first enable (no seed migration) and reused afterwards; disabling only pauses it
so Source, terms and history survive. Enable is a resumable sequence of the same helpers the manual flow
uses (each commits itself), so a half-finished enable is finished by simply toggling again. The owner's
toggle is the terms acknowledgement; for review-class providers the single-user instance operator's toggle
additionally records the operator review evidence. Nothing here performs network I/O.
"""

import copy
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import is_instance_operator
from core.database import get_session
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.connectors import provider_terms, provisioning, registry, scheduler
from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.provider_options import (
    ALL_OPTION_NAMES,
    ISO_COUNTRY_PATTERN,
    MAX_COINS,
    MAX_JOB_KEYWORDS,
    MAX_VN_SYMBOLS,
    CoinId,
    CryptoPair,
    EonetCategory,
    EonetStatus,
    FxCurrency,
    GdacsAlert,
    GdacsType,
    GdeltCountry,
    GdeltLang,
    GdeltTimespan,
    HnList,
    IsoCountry,
    JobCity,
    JobKeyword,
    JobLevel,
    NvdSeverity,
    NwsArea,
    NwsSeverity,
    OecdArea,
    Region,
    SatnogsStatus,
    UsgsMagnitude,
    UsgsPeriod,
    VnSymbol,
    WbIndicator,
    WikiLanguage,
)
from modules.connectors.provider_specs import (
    BUILTIN_PROVIDERS,
    FREE_PROVIDER_SPECS,
    TERMS_INELIGIBLE,
    FreeProviderSpec,
    applicable_options,
    deployment_use_matches,
    get_provider_spec,
    get_realtime_tier,
    min_interval_minutes,
)
from modules.connectors.provisioning_routes import ActivationRequest, _owner_access, activate_native
from modules.connectors.public import (
    ConnectorConfig,
    DataShape,
    save_connector_configuration,
    serialize_source_configuration,
    source_data_shapes,
)
from modules.connectors.sections import MAX_SECTIONS, SECTION_CATALOG
from modules.settings.public import module_dependency
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceCreate, SourcePatch

router = APIRouter(prefix="/api/v1/connectors/builtins", tags=["connectors"],
                   dependencies=[Depends(module_dependency("connectors"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]

_USES_WITH_REVIEW = ("personal", "noncommercial", "commercial")


class SectionRead(BaseModel):
    """One selectable RSS section as the UI renders it (slug is the only value the owner sends back)."""
    slug: str
    label_en: str
    label_vi: str


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
    realtime_tier: Literal["realtime", "near_realtime"] = "near_realtime"
    data_shapes: list[DataShape] = []
    source_id: UUID | None
    enabled: bool
    # Owner options this built-in accepts (UI renders only these) and, for RSS presets, the server section catalog.
    applicable_options: list[str] = []
    sections: list[SectionRead] | None = None
    default_section: str | None = None
    # Hard floor for ``schedule_interval_minutes`` (the UI hides shorter choices; the API answers 422 below it).
    min_interval_minutes: int = 15


class BuiltinEnableRequest(BaseModel):
    """Owner input for the enable toggle: the deployment use declared with the terms acknowledgement."""
    model_config = ConfigDict(extra="forbid")
    declared_use: Literal["personal", "noncommercial", "commercial", "unknown"] = "personal"
    # Optional owner switches; None keeps the Source's current value (new Sources default to on).
    graph_enabled: bool | None = None
    rag_index_enabled: bool | None = None
    realtime_mode: Literal["realtime", "near_realtime"] | None = None
    # Wave A options: None keeps the saved value, else the spec default (see ``enable_builtin``).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule_interval_minutes: Literal[15, 30, 60, 360, 1440] | None = None
    max_items: StrictInt | None = Field(default=None, ge=1, le=100)
    include_keywords: tuple[str, ...] | None = Field(default=None, max_length=20)
    exclude_keywords: tuple[str, ...] | None = Field(default=None, max_length=20)
    max_age_days: StrictInt | None = Field(default=None, ge=1, le=365)
    # Wave B: RSS section slugs (catalog providers only; membership is checked against the server catalog).
    feed_sections: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=MAX_SECTIONS)
    # Wave D per-type options; the same bounded types as ``ConnectorConfig`` (422 on any other value), allowed
    # per provider by the registry (422 on the wrong provider).
    hn_list: HnList | None = None
    gdelt_query: str | None = Field(default=None, min_length=1, max_length=100)
    gdelt_timespan: GdeltTimespan | None = None
    gdelt_source_lang: GdeltLang | None = None
    gdelt_source_country: GdeltCountry | None = None
    usgs_min_magnitude: UsgsMagnitude | None = None
    usgs_period: UsgsPeriod | None = None
    min_magnitude: float | None = Field(default=None, ge=2.0, le=8.0)
    eonet_categories: tuple[EonetCategory, ...] | None = Field(default=None, min_length=1, max_length=13)
    eonet_status: EonetStatus | None = None
    eonet_days: StrictInt | None = Field(default=None, ge=1, le=30)
    gdacs_event_types: tuple[GdacsType, ...] | None = Field(default=None, min_length=1, max_length=6)
    gdacs_alert_levels: tuple[GdacsAlert, ...] | None = Field(default=None, min_length=1, max_length=3)
    nws_severity: tuple[NwsSeverity, ...] | None = Field(default=None, min_length=1, max_length=4)
    nws_area: NwsArea | None = None
    nvd_severity: tuple[NvdSeverity, ...] | None = Field(default=None, min_length=1, max_length=4)
    nvd_window_days: StrictInt | None = Field(default=None, ge=1, le=7)
    nvd_keyword: str | None = Field(default=None, min_length=1, max_length=100)
    kev_ransomware_only: StrictBool | None = None
    ioda_country: str | None = Field(default=None, pattern=ISO_COUNTRY_PATTERN)
    region: Region | None = None
    # Wave F per-type options (curated enums/bounded numbers; the registry allows each only on its own provider).
    wiki_language: WikiLanguage | None = None
    satnogs_status: SatnogsStatus | None = None
    disease_country: IsoCountry | None = None
    wb_country: IsoCountry | None = None
    wb_indicator: WbIndicator | None = None
    fx_base: FxCurrency | None = None
    fx_quotes: tuple[FxCurrency, ...] | None = Field(default=None, min_length=1, max_length=10)
    oecd_countries: tuple[OecdArea, ...] | None = Field(default=None, min_length=1, max_length=10)
    crypto_symbols: tuple[CryptoPair, ...] | None = Field(default=None, min_length=1, max_length=10)
    vn_symbols: tuple[VnSymbol, ...] | None = Field(default=None, min_length=1, max_length=MAX_VN_SYMBOLS)
    job_keywords: tuple[JobKeyword, ...] | None = Field(default=None, min_length=1, max_length=MAX_JOB_KEYWORDS)
    job_cities: tuple[JobCity, ...] | None = Field(default=None, min_length=1, max_length=3)
    job_levels: tuple[JobLevel, ...] | None = Field(default=None, min_length=1, max_length=5)
    job_min_salary_usd: StrictInt | None = Field(default=None, ge=0, le=20000)
    coin_ids: tuple[CoinId, ...] | None = Field(default=None, min_length=1, max_length=MAX_COINS)
    kp_min: float | None = Field(default=None, ge=0, le=9)
    advisory_min_level: StrictInt | None = Field(default=None, ge=1, le=4)


_DEFAULT_ENABLE = BuiltinEnableRequest()  # body is optional; the model is never mutated


def _spec(provider: str) -> FreeProviderSpec:
    """Return the built-in spec, or 404 for any provider outside ``BUILTIN_PROVIDERS`` (scoped/keyed stay manual)."""
    spec = get_provider_spec(provider)
    if spec is None or provider not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=404, detail="Built-in source not found")
    return spec


def _read(spec: FreeProviderSpec, source: ConnectorSource | None, active: bool) -> BuiltinSourceRead:
    """Project a spec plus its (optional) Source and activation flag into the owner-facing row.

    Adds the applicable option names and, for section-catalog presets, the catalog and its default slug so the
    UI never duplicates slug lists.
    """
    catalog = SECTION_CATALOG.get(spec.id)
    return BuiltinSourceRead(
        id=spec.id, label=spec.label, category=spec.category, eligibility=spec.eligibility,
        terms_url=spec.terms_url, terms_checked_on=spec.checked_on, attribution=spec.attribution,
        execution=spec.execution,
        realtime_tier=get_realtime_tier(spec.id, getattr(source, "configuration", None)),
        data_shapes=sorted(source_data_shapes(spec.id, PROVIDER_SOURCE_TYPES.get(spec.id, "api"))),
        source_id=source.id if source is not None else None, enabled=active,
        applicable_options=applicable_options(spec.id),
        sections=[SectionRead(slug=x.slug, label_en=x.label_en, label_vi=x.label_vi) for x in catalog.sections]
        if catalog else None,
        default_section=catalog.default if catalog else None,
        min_interval_minutes=min_interval_minutes(spec.id),
    )


def _is_enabled(source: ConnectorSource, row: object | None) -> bool:
    """A built-in is on only when its Source is active and the connector row is active and current."""
    return bool(
        row is not None and source.status == "active" and getattr(row, "state", None) == "active"
        and getattr(row, "desired_enabled", False) and getattr(row, "applied_revision", -1) == getattr(row, "desired_revision", -2)
        and getattr(row, "source_generation", -1) == source.generation
    )


@dataclass(frozen=True)
class _RowSnapshot:
    """Detached copy of the provisioning fields the toggle reads.

    ``session.rollback()`` expires every ORM row; reading an expired attribute triggers a synchronous
    lazy load, which raises MissingGreenlet under asyncio. The enable sequence rolls back between steps,
    so it never keeps the ORM row itself across a step, only this snapshot.
    """
    state: str
    desired_enabled: bool
    applied_revision: int
    desired_revision: int
    source_generation: int
    desired_configuration: dict[str, Any] | None


def _snapshot(row: Any) -> _RowSnapshot | None:
    """Copy a freshly loaded provisioning row into a ``_RowSnapshot`` (None stays None); read before any rollback."""
    if row is None:
        return None
    return _RowSnapshot(
        state=row.state, desired_enabled=row.desired_enabled, applied_revision=row.applied_revision,
        desired_revision=row.desired_revision, source_generation=row.source_generation,
        desired_configuration=copy.deepcopy(row.desired_configuration))


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

    Owner-only (members have no Source access). Creates no Source and takes no row locks; the
    access-fence reads use FOR SHARE locks that the route's read transaction releases. State is computed
    in two batched reads (all provider Sources, then their provisioning rows) instead of per built-in;
    per provider the oldest Source wins and ``enabled`` follows ``_state`` exactly.
    """
    gate = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, _owner)
    specs = [spec for spec in FREE_PROVIDER_SPECS if spec.id in BUILTIN_PROVIDERS]
    oldest: dict[str, ConnectorSource] = {}
    for source in await sources.list_provider_connector_sources(
            session, frozenset(spec.id for spec in specs), scope=_owner, multi_workspace_enabled=gate):
        if source.provider is not None:
            oldest.setdefault(source.provider, source)  # rows arrive oldest first
    rows = await provisioning.activation_statuses(
        session, frozenset(source.id for source in oldest.values()), scope=_owner, multi_workspace_enabled=gate)
    return [
        _read(spec, oldest[spec.id], _is_enabled(oldest[spec.id], rows.get(oldest[spec.id].id)))
        if spec.id in oldest else _read(spec, None, False)
        for spec in specs
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

    Wave D per-type options (``hn_list``, ``gdelt_*``, ``usgs_*``, ``eonet_*``, ``gdacs_*``, ``nws_*``, ``nvd_*``,
    ``min_magnitude``, ``kev_ransomware_only``, ``ioda_country``, ``region``) and wave F options (``wiki_language``,
    ``satnogs_status``, ``disease_country``, ``wb_*``, ``fx_*``, ``oecd_countries``, ``crypto_symbols``, ``coin_ids``,
    ``vn_symbols``, ``job_*``, ``kp_min``, ``advisory_min_level``) follow the same precedence and are accepted only by their own provider
    (422 otherwise; an enum value outside a field's curated list is rejected at request parsing, also 422).

    Settings precedence per field (interval, ``realtime_mode``, ``max_items``, keywords, ``max_age_days``,
    ``feed_sections``):
    request value, else the Source's saved configuration, else the spec default (interval) or unset.
    Filter fields for a provider outside ``INCREMENTAL_PROVIDERS`` and ``feed_sections`` for a provider without a
    section catalog (or with a slug outside it) fail validation (422). ``name`` only
    names a newly created Source; it is ignored when an existing Source is reused. An interval below the provider's
    hard floor (``min_interval_minutes``) fails with 422 ``schedule_interval_below_minimum``; the default is
    ``max(spec default, floor)``.
    """
    spec = _spec(provider)
    scope, gate = _owner, request.app.state.settings.multi_workspace_enabled
    use = payload.declared_use
    # Read before the first rollback: later rollbacks expire the auth ORM row, and reading it then raises MissingGreenlet.
    reviewer_user_id = request.state.auth_session.owner_id
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
    if found is not None:
        # rollback expires the ORM row; reload explicitly, since lazy attribute IO is illegal under asyncio.
        await session.refresh(found)
    if found is None:
        found = await sources.create_source(
            session, SourceCreate(
                type=PROVIDER_SOURCE_TYPES[provider], name=payload.name or spec.label, provider=provider,
                graph_enabled=bool(payload.graph_enabled),
                rag_index_enabled=(
                    payload.rag_index_enabled
                    if payload.rag_index_enabled is not None
                    else not spec.skip_embedding
                )),
            scope=scope, multi_workspace_enabled=gate, expected_access_fence=fence)
    else:
        # Re-enabling an existing Source: un-pause it and apply any explicitly sent switches in one patch.
        patch: dict[str, Any] = {"status": "active"} if found.status == "paused" else {}
        for flag in ("graph_enabled", "rag_index_enabled"):
            wanted = getattr(payload, flag)
            if wanted is not None and wanted != getattr(found, flag):
                patch[flag] = wanted
        if patch:
            await sources.update_source(
                session, found, SourcePatch(**patch), scope=scope, multi_workspace_enabled=gate,
                expected_access_fence=fence)
    source = await sources.get_connector_source(session, found.id, scope=scope, multi_workspace_enabled=gate)
    if source is None or source.status != "active":
        raise HTTPException(status_code=409, detail="Source is inactive; it cannot be enabled")

    # 3. Desired settings: only rewritten when different, because every save bumps the revision and
    # demotes an active connector. A paused->active Source has a new generation, so it must be re-saved.
    saved_config = source.configuration

    def pick(field: str, default: object = None) -> object:
        """Request value, else the owner's saved value, else ``default`` (so Resume keeps settings)."""
        value = getattr(payload, field)
        if value is None and field in payload.model_fields_set:
            return default  # explicit null clears the saved value (back to default); an absent field keeps it
        return value if value is not None else saved_config.get(field, default)

    # Only non-None filter values are passed so default sources serialise exactly as before.
    options = {
        field: value for field in (
            "max_items", "include_keywords", "exclude_keywords", "max_age_days", "feed_sections",
            *sorted(ALL_OPTION_NAMES - {"max_items"}),
        )
        if (value := pick(field)) is not None
    }
    floor = min_interval_minutes(provider)
    interval = pick("schedule_interval_minutes", max(spec.default_interval_minutes, floor))
    if isinstance(interval, int) and interval < floor:
        raise HTTPException(status_code=422, detail=f"schedule_interval_below_minimum: minimum {floor} minutes")
    try:
        config = ConnectorConfig(
            timeout_seconds=30, history_mode="returned_snapshot",
            schedule_interval_minutes=interval,
            realtime_mode=pick("realtime_mode"), **options)
        source_configuration = serialize_source_configuration(source, config)
        registry.validate(source.model_copy(update={"configuration": source_configuration}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Provider configuration is invalid") from exc
    desired = {**source_configuration, "auth_method": "none"}
    row = _snapshot(await provisioning.activation_status(session, source.id, scope=scope, multi_workspace_enabled=gate))
    if row is None or row.desired_configuration != desired or row.source_generation != source.generation:
        await session.rollback()
        fence = await _owner_access(session, request, scope, expected=fence)
        saved = await save_connector_configuration(
            session, source, row.desired_revision if row is not None else 0, source_configuration, desired,
            scope=scope, multi_workspace_enabled=gate)
        if saved is None:
            raise HTTPException(status_code=409, detail="Source or connector revision changed while saving")
        saved_source, saved_row = saved
        saved_row.state = "saved_not_active"
        await commit_with_replay(session, [
            make_source_change(saved_source.id, saved_source.generation, saved_source.status,
                               connector_state="saved_not_active", scope=scope),
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
            reviewer_user_id=reviewer_user_id,
            body=provider_terms.OperatorTermsReview(
                state="approved", allowed_use=use,
                evidence_ref=f"builtin-toggle:{spec.id}:{spec.terms_url}@{spec.checked_on}",
                reviewed_terms_version=spec.checked_on),
            instance_operator=True)
    await scheduler.clear_collection_block(
        session, source.id, terms_revision=terms.terms_revision, workspace_id=scope.workspace_id)
    await session.commit()

    # 6. Activate natively unless it is already on at the current revision.
    row = _snapshot(await provisioning.activation_status(session, source.id, scope=scope, multi_workspace_enabled=gate))
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
