from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
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
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base
from core.model_gateway.cache import capability_key, validate_capability_scope
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
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    theme: Mapped[str] = mapped_column(String(8), nullable=False, server_default="system")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="en-us")
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, server_default="Asia/Ho_Chi_Minh")
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


class ModelTestResultRecord(Base):
    """Last Test-button outcome of one provider model, so the Models table survives reloads.

    Display-only evidence (never consulted by the gateway); capability evidence stays in Redis.
    """

    __tablename__ = "model_test_results"
    __table_args__ = (
        CheckConstraint("result IN ('supported', 'failed', 'unsupported')", name="ck_model_test_results_result"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_model_test_results_workspace", ondelete="RESTRICT"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


async def save_model_test_result(session: AsyncSession, *, workspace_id: UUID, provider_id: str, model_id: str, result: str) -> ModelTestResultRecord:
    """Upsert the latest test outcome for one model and stamp ``checked_at``; caller commits."""
    stmt = insert(ModelTestResultRecord).values(workspace_id=workspace_id, provider_id=provider_id, model_id=model_id, result=result)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["workspace_id", "provider_id", "model_id"],
        set_={"result": result, "checked_at": func.now()}))
    row = await session.scalar(select(ModelTestResultRecord).where(
        ModelTestResultRecord.workspace_id == workspace_id, ModelTestResultRecord.provider_id == provider_id,
        ModelTestResultRecord.model_id == model_id).execution_options(populate_existing=True))
    assert row is not None  # just upserted in this transaction
    return row


async def list_model_test_results(session: AsyncSession, *, workspace_id: UUID) -> list[ModelTestResultRecord]:
    """Return every stored model test outcome for the workspace (bounded by the 8-provider cap)."""
    return list((await session.scalars(select(ModelTestResultRecord).where(
        ModelTestResultRecord.workspace_id == workspace_id).limit(1000))).all())


async def save_capability(redis: Redis, result: CapabilityResult, *, config: AIExecutionConfig, scope: Scope,
                          multi_workspace_enabled: bool, configured_mapping: ModelMapping | None = None) -> None:
    """Cache only exact scoped configuration evidence after caller's locked publication fence.

    Expired results are discarded; TTL max24h and exact namespace leave old workspace
    configuration entries unreachable without deleting any other workspace's cache.
    Non-default roles require the caller's freshly verified configured mapping (e.g. the
    visualization router). A standard role may also be proven for a non-default model, but only when
    ``configured_mapping`` names a model listed on its provider in ``config.providers`` (chat
    composer overrides); anything else raises ValueError. Identity/model mismatches raise ValueError.
    """
    validate_capability_scope(config, scope, multi_workspace_enabled)
    if (result.workspace_id != config.workspace_id or result.actor_user_id != config.actor_user_id
            or result.membership_revision != config.membership_revision
            or result.gateway_identity != config.gateway_identity
            or result.configuration_revision != config.configuration_revision):
        raise ValueError("Capability evidence does not match execution identity")
    mapping = config.defaults.resolve(result.role) if result.role in MODEL_ROLES else configured_mapping
    if result.role in MODEL_ROLES and configured_mapping is not None:
        # Per-request model override: evidence is allowed only for a model the owner configured on that provider.
        provider = config.providers.get(configured_mapping.provider_id or "default")
        if provider is not None and any(m.id == configured_mapping.model for m in provider.models):
            mapping = configured_mapping
    if mapping is None or mapping.model != result.model or mapping.version != result.version:
        raise ValueError("Capability evidence does not match configured model")
    ttl = int((datetime.fromisoformat(result.expires_at) - datetime.now(UTC)).total_seconds())
    if ttl <= 0:
        return
    key = capability_key(result.role, result.model, result.version, result.capability, result.gateway_identity,
                         workspace_id=config.workspace_id, actor_user_id=config.actor_user_id)
    await redis.set(key, result.model_dump_json(), ex=min(ttl, 86400))


async def list_capabilities(redis: Redis, *, config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> list[CapabilityResult]:
    """Read at most 6 roles x 6 capabilities by exact workspace/actor/config keys, never global SCAN."""
    validate_capability_scope(config, scope, multi_workspace_enabled)
    results: list[CapabilityResult] = []
    for role in MODEL_ROLES:
        mapping = config.defaults.resolve(role)
        if mapping is None:
            continue
        for capability in ("chat", "streaming", "embeddings", "structured", "tools", "reranking"):
            key = capability_key(role, mapping.model, mapping.version, capability, config.gateway_identity,
                                 workspace_id=config.workspace_id, actor_user_id=config.actor_user_id)
            try:
                value = CapabilityResult.model_validate_json(await redis.get(key))
                expiry = datetime.fromisoformat(value.expires_at)
                if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                    continue
            except (ValueError, TypeError):
                continue
            except RedisError:
                return results  # cached evidence is optional; report none when Redis is down
            if (value.workspace_id == config.workspace_id and value.actor_user_id == config.actor_user_id
                    and value.membership_revision == config.membership_revision
                    and value.gateway_identity == config.gateway_identity
                    and value.configuration_revision == config.configuration_revision
                    and value.role == role and value.capability == capability
                    and value.model == mapping.model and value.version == mapping.version):
                results.append(value)
    return results


def new_capability_result(role: str, mapping: ModelMapping, capability: str, result: str, *, config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> CapabilityResult:
    """Detach probe evidence for the exact workspace/actor/configuration with24h validity.

    Does not authorize storage: caller must reacquire matching identity/settings fence
    after provider I/O before save_capability. Draft probes cannot supply this config.
    """
    validate_capability_scope(config, scope, multi_workspace_enabled)
    now = datetime.now(UTC)
    return CapabilityResult(workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision, role=role, model=mapping.model, version=mapping.version,
        gateway_identity=config.gateway_identity, configuration_revision=config.configuration_revision,
        capability=capability, result=result, checked_at=now.isoformat(), expires_at=(now + timedelta(hours=24)).isoformat())
