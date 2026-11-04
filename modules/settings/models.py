import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base
from core.model_gateway.cache import validate_capability_scope
from core.model_gateway.schemas import (
    MODEL_ROLES,
    AIExecutionConfig,
    CapabilityResult,
    ModelMapping,
)
from core.workspaces.schemas import Scope


class AISettingsRecord(Base):
    """Persist the owner-scoped AI gateway, model, and privacy configuration.

    The owned-default workspace constraint scopes configuration;
    encrypted credential fields remain ciphertext at rest.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "ai_settings"
    __table_args__ = (
        CheckConstraint("configuration_revision > 0", name="ck_ai_settings_revision_positive"),
        CheckConstraint("request_timeout_seconds BETWEEN 5 AND 180", name="ck_ai_settings_timeout"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_ai_settings_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_ai_settings_principal", ondelete="RESTRICT"),
        Index("ix_w2_ai_settings_scope", 'workspace_id', 'owner_id'),
        Index("ix_w2_ai_settings_work", 'workspace_id', 'created_at', 'owner_id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # {role: ModelMapping json} for chat, brief, background, embedding, reranker, vision (see r29_model_roles).
    defaults: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    privacy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    system_prompts: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # {provider_id: {kind, label, base_url, key_ciphertext, local, models: [{id, type}]}}.
    providers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # {service_url, api_key_ciphertext, webhook_token_ciphertext}; empty means "use the N8N_* environment".
    n8n: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Opt-in visualization router; empty = internal router (see r24_viz_router_settings).
    viz_router: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    web_search_provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="none")
    web_search_endpoint: Mapped[str | None] = mapped_column(Text)
    web_search_api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    # Ordered secondary destinations hold only encrypted credential material.
    web_search_fallbacks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class OwnerPreferencesRecord(Base):
    """Persist the owner's revisioned theme, locale, and timezone preferences.

    The account key and checks constrain this record to supported settings.

    Account or Source lineage replaces the legacy bootstrap-only owner restriction.
    """
    __tablename__ = "owner_preferences"
    __table_args__ = (
        CheckConstraint("configuration_revision > 0", name="ck_owner_preferences_revision_positive"),
        CheckConstraint("theme IN ('light', 'dark', 'system')", name="ck_owner_preferences_theme"),
        CheckConstraint("locale IN ('en-us', 'vi-vi')", name="ck_owner_preferences_locale"),
        CheckConstraint("accent IS NULL OR accent IN ('ink', 'moss', 'sea', 'earth', 'plum')", name="ck_owner_preferences_accent"),
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    theme: Mapped[str] = mapped_column(String(8), nullable=False, server_default="system")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en-us")
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, server_default="Asia/Ho_Chi_Minh")
    # Optional greeting name and accent palette; NULL means unset / default ink.
    display_name: Mapped[str | None] = mapped_column(String(80))
    accent: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class OnboardingStateRecord(Base):
    """Persist only the owner's resumable onboarding step and explicit completion.

    Account or Source lineage replaces the legacy bootstrap-only owner restriction.
    """
    __tablename__ = "onboarding_state"
    __table_args__ = (
        CheckConstraint("configuration_revision > 0", name="ck_onboarding_state_revision_positive"),
        CheckConstraint("current_step IN ('ai_privacy', 'capability', 'sources', 'sample_or_import', 'indexing', 'complete')", name="ck_onboarding_state_step"),
        CheckConstraint("data_choice IS NULL OR data_choice IN ('sample', 'personal_import')", name="ck_onboarding_state_data_choice"),
        CheckConstraint("(current_step = 'complete') = (completed_at IS NOT NULL)", name="ck_onboarding_state_completion"),
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    current_step: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ai_privacy")
    data_choice: Mapped[str | None] = mapped_column(String(24))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class RetentionSettingsRecord(Base):
    """Persist revisioned trace retention while raw-source and document history remain retained.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "retention_settings"
    __table_args__ = (
        CheckConstraint("configuration_revision > 0", name="ck_retention_settings_revision"),
        CheckConstraint("agent_trace_days BETWEEN 1 AND 3650", name="ck_retention_settings_trace_days"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_retention_settings_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_retention_settings_principal", ondelete="RESTRICT"),
        Index("ix_w2_retention_settings_scope", 'workspace_id', 'owner_id'),
        Index("ix_w2_retention_settings_work", 'workspace_id', 'created_at', 'owner_id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    agent_trace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="90")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class ModuleLifecycleRecord(Base):
    """Persist only owner-requested disables; descriptor dependencies compute effective availability.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "module_lifecycle_settings"
    __table_args__ = (
        CheckConstraint("configuration_revision > 0", name="ck_module_lifecycle_revision"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_module_lifecycle_settings_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_module_lifecycle_settings_principal", ondelete="RESTRICT"),
        Index("ix_w2_module_lifecycle_settings_scope", 'workspace_id', 'owner_id'),
        Index("ix_w2_module_lifecycle_settings_work", 'workspace_id', 'created_at', 'owner_id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    disabled_modules: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class TranslationSettingsRecord(Base):
    """Persist one workspace's translation choice; unrelated to any viewer's UI locale.

    Absent row means disabled/vi/revision 1. Revision changes invalidate every cache fingerprint.
    """

    __tablename__ = "translation_settings"
    __table_args__ = (
        CheckConstraint("configuration_revision > 0", name="ck_translation_settings_revision"),
        CheckConstraint("target_language IN ('vi', 'en')", name="ck_translation_settings_target"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_translation_settings_workspace", ondelete="RESTRICT"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    target_language: Mapped[str] = mapped_column(String(2), nullable=False, server_default="vi")
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("owner.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


_LOG = logging.getLogger(__name__)
_DEFAULT_PROVIDER = "default"
# A repeat of the same outcome only refreshes ``checked_at`` this often, so every chat turn is not a DB write.
_EVIDENCE_REFRESH = "1 hour"


class ModelTestResultRecord(Base):
    """Single persistent source of model capability evidence: last outcome per provider model and capability.

    Written by the Settings Test routes and by real gateway calls (chat/stream/tools/structured/embeddings outcomes);
    read by the Models table and, for ``unsupported`` tools/structured evidence only, by the gateway. There is no
    expiry: ``model_identity`` (credential/endpoint/default fingerprint) invalidates evidence when it changes.
    Rows with a NULL identity (pre-existing UI test summaries) are display-only and never gate a request.
    """

    __tablename__ = "model_test_results"
    __table_args__ = (
        CheckConstraint("result IN ('supported', 'failed', 'unsupported')", name="ck_model_test_results_result"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_model_test_results_workspace", ondelete="RESTRICT"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    capability: Mapped[str] = mapped_column(String(16), primary_key=True, server_default="chat")
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    model_identity: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


async def save_model_test_result(
    session: AsyncSession, *, workspace_id: UUID, provider_id: str, model_id: str, result: str,
    capability: str = "chat", model_identity: str | None = None, throttle: bool = False,
) -> ModelTestResultRecord | None:
    """Upsert the latest outcome for one model capability and stamp ``checked_at``; caller commits.

    ``throttle`` (real-call recording) skips the write when the stored outcome and identity are identical and
    were checked within the last hour; the stored row is still returned. Explicit Test saves always write.
    """
    table = ModelTestResultRecord
    stmt = insert(table).values(
        workspace_id=workspace_id, provider_id=provider_id, model_id=model_id,
        capability=capability, result=result, model_identity=model_identity)
    guard = or_(table.result != result, table.model_identity.is_distinct_from(model_identity),
                table.checked_at < func.now() - text(f"interval '{_EVIDENCE_REFRESH}'")) if throttle else None
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["workspace_id", "provider_id", "model_id", "capability"],
        set_={"result": result, "model_identity": model_identity, "checked_at": func.now()}, where=guard))
    return await session.scalar(select(table).where(
        table.workspace_id == workspace_id, table.provider_id == provider_id,
        table.model_id == model_id, table.capability == capability,
    ).execution_options(populate_existing=True))


async def list_model_test_results(session: AsyncSession, *, workspace_id: UUID) -> list[ModelTestResultRecord]:
    """Return every stored capability outcome for the workspace (bounded by providers x models x capabilities)."""
    return list((await session.scalars(select(ModelTestResultRecord).where(
        ModelTestResultRecord.workspace_id == workspace_id).limit(5000))).all())


class PostgresCapabilityEvidence:
    """``CapabilityEvidenceStore`` backed by ``model_test_results`` for one workspace and model identity.

    Each call uses its own short session, never the caller's: evidence I/O must not hold or commit the request
    transaction around provider egress. Reads only trust rows whose stored identity equals the current one, so a
    credential/endpoint/default change silently invalidates old evidence (no TTL).
    """

    def __init__(self, factory: Callable[[], AbstractAsyncContextManager[AsyncSession]], *, workspace_id: UUID, model_identity: str) -> None:
        """Bind the session factory and the immutable workspace/identity namespace of this evidence."""
        self._factory = factory
        self._workspace_id = workspace_id
        self._identity = model_identity

    async def lookup(self, role: str, mapping: ModelMapping, capability: str) -> str | None:
        """Return the stored result for this model capability under the current identity, else None."""
        async with self._factory() as session:
            return await session.scalar(select(ModelTestResultRecord.result).where(
                ModelTestResultRecord.workspace_id == self._workspace_id,
                ModelTestResultRecord.provider_id == (mapping.provider_id or _DEFAULT_PROVIDER),
                ModelTestResultRecord.model_id == mapping.model,
                ModelTestResultRecord.capability == capability,
                ModelTestResultRecord.model_identity == self._identity))

    async def record(self, role: str, mapping: ModelMapping, capability: str,
                     result: Literal["supported", "unsupported", "failed"]) -> None:
        """Upsert (throttled) the real-call outcome and commit it in an isolated transaction."""
        async with self._factory() as session:
            await save_model_test_result(
                session, workspace_id=self._workspace_id, provider_id=mapping.provider_id or _DEFAULT_PROVIDER,
                model_id=mapping.model, result=result, capability=capability, model_identity=self._identity, throttle=True)
            await session.commit()


def capability_evidence(factory: Callable[[], AbstractAsyncContextManager[AsyncSession]], config: AIExecutionConfig) -> PostgresCapabilityEvidence:
    """Build the gateway evidence store for an execution config (workspace and model identity come from it).

    ``factory`` is the caller's ``async_sessionmaker``; the store opens its own short sessions from it.
    """
    return PostgresCapabilityEvidence(factory, workspace_id=config.workspace_id, model_identity=config.model_identity)


async def save_capability(session: AsyncSession, result: CapabilityResult, *, config: AIExecutionConfig, scope: Scope,
                          multi_workspace_enabled: bool, configured_mapping: ModelMapping | None = None) -> None:
    """Persist exact scoped probe evidence after the caller's post-I/O configuration fence; caller commits.

    Evidence is bound to ``model_identity`` (model-relevant config only), not the settings row revision:
    unrelated saves (web search, prompts, owner prefs) must not discard verification. There is no TTL.
    Non-default roles require the caller's freshly verified configured mapping (e.g. the
    visualization router). A standard role may also be proven for a non-default model, but only when
    ``configured_mapping`` names a model listed on its provider in ``config.providers`` (chat
    composer overrides); anything else raises ValueError. Identity/model mismatches raise ValueError.
    """
    validate_capability_scope(config, scope, multi_workspace_enabled)
    if (result.workspace_id != config.workspace_id or result.actor_user_id != config.actor_user_id
            or result.membership_revision != config.membership_revision
            or result.model_identity != config.model_identity):
        raise ValueError("Capability evidence does not match execution identity")
    mapping = config.defaults.resolve(result.role) if result.role in MODEL_ROLES else configured_mapping
    if result.role in MODEL_ROLES and configured_mapping is not None:
        # Per-request model override: evidence is allowed only for a model the owner configured on that provider.
        provider = config.providers.get(configured_mapping.provider_id or _DEFAULT_PROVIDER)
        if provider is not None and any(m.id == configured_mapping.model for m in provider.models):
            mapping = configured_mapping
    if mapping is None or mapping.model != result.model or mapping.version != result.version:
        raise ValueError("Capability evidence does not match configured model")
    await save_model_test_result(
        session, workspace_id=config.workspace_id, provider_id=mapping.provider_id or _DEFAULT_PROVIDER,
        model_id=mapping.model, result=result.result, capability=result.capability, model_identity=config.model_identity)


async def list_capabilities(session: AsyncSession, *, config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> list[CapabilityResult]:
    """Report stored evidence for each configured role default under the current model identity (one query)."""
    validate_capability_scope(config, scope, multi_workspace_enabled)
    rows = await list_model_test_results(session, workspace_id=config.workspace_id)
    by_key = {(r.provider_id, r.model_id, r.capability): r for r in rows if r.model_identity == config.model_identity}
    results: list[CapabilityResult] = []
    for role in MODEL_ROLES:
        mapping = config.defaults.resolve(role)
        if mapping is None:
            continue
        for capability in ("chat", "streaming", "embeddings", "structured", "tools", "reranking"):
            row = by_key.get((mapping.provider_id or _DEFAULT_PROVIDER, mapping.model, capability))
            if row is None or row.result not in {"supported", "unsupported", "failed"}:
                continue
            results.append(CapabilityResult(
                workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
                membership_revision=config.membership_revision, role=role, model=mapping.model, version=mapping.version,
                model_identity=config.model_identity, configuration_revision=config.configuration_revision,
                capability=capability, result=row.result,
                checked_at=row.checked_at.isoformat()))
    return results


def new_capability_result(role: str, mapping: ModelMapping, capability: str, result: str, *, config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> CapabilityResult:
    """Detach probe evidence for the exact workspace/actor/configuration (no expiry).

    Does not authorize storage: caller must reacquire matching identity/settings fence
    after provider I/O before save_capability. Draft probes cannot supply this config.
    """
    validate_capability_scope(config, scope, multi_workspace_enabled)
    return CapabilityResult(workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision, role=role, model=mapping.model, version=mapping.version,
        model_identity=config.model_identity, configuration_revision=config.configuration_revision,
        capability=capability, result=result, checked_at=datetime.now(UTC).isoformat())
