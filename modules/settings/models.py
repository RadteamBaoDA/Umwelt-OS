from uuid import UUID
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.config import Settings
from core.database import Base
from core.model_gateway.cache import capability_key, validate_capability_scope
from core.model_gateway.schemas import AIExecutionConfig, CapabilityResult, ModelMapping
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext


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
    omniroute_base_url: Mapped[str | None] = mapped_column(Text)
    omniroute_api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    privacy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    chat_alias: Mapped[str] = mapped_column(String(32), nullable=False, server_default="reasoning-large")
    brief_alias: Mapped[str] = mapped_column(String(32), nullable=False, server_default="reasoning-small")
    web_search_provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="none")
    web_search_endpoint: Mapped[str | None] = mapped_column(Text)
    web_search_api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
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


ALIASES = ("reasoning-large", "reasoning-small", "fast", "embedding", "reranker", "vision", "local-private")
_MAPPINGS = "bbd:settings:model-mappings"


async def legacy_aliases(redis: Redis | None, settings: Settings, *, scope: Scope) -> dict[str, ModelMapping]:
    """Return server defaults; overlay old Redis aliases only for already-proven bootstrap owner.

    Caller first admits active owned-default scope with actual settings feature flag. Redis
    singleton aliases never initialize a new workspace or grant remote-data consent.
    """
    if isinstance(scope, InternalJobScope):
        actor = scope.actor_user_id
    elif isinstance(scope, WorkspaceContext) and scope.role == "owner":
        actor = scope.user_id
    else:
        raise ValueError("An admitted owner scope is required")
    if actor != 1 and not settings.multi_workspace_enabled:
        raise ValueError("Multi-workspace execution is disabled")
    configured = {name: ModelMapping(model=model, destination="remote") for name, model in settings.omniroute_models.items() if name in ALIASES}
    if redis is not None and actor == 1:
        for alias, value in (await cast("Awaitable[dict[Any, Any]]", redis.hgetall(_MAPPINGS))).items():
            alias = alias.decode() if isinstance(alias, bytes) else alias
            try:
                if alias in ALIASES:
                    configured[alias] = ModelMapping.model_validate_json(value)
            except ValueError:
                continue
    return configured


async def save_capability(redis: Redis, result: CapabilityResult, *, config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Cache only exact scoped configuration evidence after caller's locked publication fence.

    Expired results are discarded; TTL max24h and exact namespace leave old workspace
    configuration entries unreachable without deleting any other workspace's cache.
    """
    validate_capability_scope(config, scope, multi_workspace_enabled)
    if (result.workspace_id != config.workspace_id or result.actor_user_id != config.actor_user_id
            or result.membership_revision != config.membership_revision
            or result.gateway_identity != config.gateway_identity
            or result.configuration_revision != config.configuration_revision):
        raise ValueError("Capability evidence does not match execution identity")
    mapping = config.aliases.get(result.alias)
    if mapping is None or mapping.model != result.model or mapping.version != result.version:
        raise ValueError("Capability evidence does not match configured model")
    ttl = int((datetime.fromisoformat(result.expires_at) - datetime.now(UTC)).total_seconds())
    if ttl <= 0:
        return
    key = capability_key(result.alias, result.model, result.version, result.capability, result.gateway_identity,
                         workspace_id=config.workspace_id, actor_user_id=config.actor_user_id)
    await redis.set(key, result.model_dump_json(), ex=min(ttl, 86400))


async def list_capabilities(redis: Redis, *, config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> list[CapabilityResult]:
    """Read at most7 aliases x6 capabilities by exact workspace/actor/config keys, never global SCAN."""
    validate_capability_scope(config, scope, multi_workspace_enabled)
    results: list[CapabilityResult] = []
    for alias, mapping in config.aliases.items():
        if alias not in ALIASES:
            continue
        for capability in ("chat", "streaming", "embeddings", "structured", "tools", "reranking"):
            key = capability_key(alias, mapping.model, mapping.version, capability, config.gateway_identity,
                                 workspace_id=config.workspace_id, actor_user_id=config.actor_user_id)
            try:
                value = CapabilityResult.model_validate_json(await redis.get(key))
                expiry = datetime.fromisoformat(value.expires_at)
                if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                    continue
            except (ValueError, TypeError):
                continue
            if (value.workspace_id == config.workspace_id and value.actor_user_id == config.actor_user_id
                    and value.membership_revision == config.membership_revision
                    and value.gateway_identity == config.gateway_identity
                    and value.configuration_revision == config.configuration_revision
                    and value.alias == alias and value.capability == capability
                    and value.model == mapping.model and value.version == mapping.version):
                results.append(value)
    return results


def new_capability_result(alias: str, mapping: ModelMapping, capability: str, result: str, *, config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> CapabilityResult:
    """Detach probe evidence for the exact workspace/actor/configuration with24h validity.

    Does not authorize storage: caller must reacquire matching identity/settings fence
    after provider I/O before save_capability. Draft probes cannot supply this config.
    """
    validate_capability_scope(config, scope, multi_workspace_enabled)
    now = datetime.now(UTC)
    return CapabilityResult(workspace_id=config.workspace_id, actor_user_id=config.actor_user_id,
        membership_revision=config.membership_revision, alias=alias, model=mapping.model, version=mapping.version,
        gateway_identity=config.gateway_identity, configuration_revision=config.configuration_revision,
        capability=capability, result=result, checked_at=now.isoformat(), expires_at=(now + timedelta(hours=24)).isoformat())
