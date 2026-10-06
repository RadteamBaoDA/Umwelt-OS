from datetime import UTC, datetime, timedelta

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from redis.asyncio import Redis

from core.config import Settings
from core.model_gateway.cache import capability_alias_pattern, capability_key
from core.model_gateway.schemas import CapabilityResult, ModelMapping
from core.database import Base


class AISettingsRecord(Base):
    """Persist the owner-scoped AI gateway, model, and privacy configuration.

    The owner primary key and database checks enforce the single-user boundary;
    encrypted credential fields remain ciphertext at rest.
    """
    __tablename__ = "ai_settings"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_ai_settings_single_owner"),
        CheckConstraint("configuration_revision > 0", name="ck_ai_settings_revision_positive"),
        CheckConstraint("request_timeout_seconds BETWEEN 5 AND 180", name="ck_ai_settings_timeout"),
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    omniroute_base_url: Mapped[str | None] = mapped_column(Text)
    omniroute_api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    privacy: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
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

    The singleton owner key and checks constrain this record to supported settings.
    """
    __tablename__ = "owner_preferences"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_owner_preferences_single_owner"),
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
    """Persist only the owner's resumable onboarding step and explicit completion."""
    __tablename__ = "onboarding_state"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_onboarding_state_single_owner"),
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
    """Persist revisioned trace retention while raw-source and document history remain retained."""

    __tablename__ = "retention_settings"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_retention_settings_single_owner"),
        CheckConstraint("configuration_revision > 0", name="ck_retention_settings_revision"),
        CheckConstraint("agent_trace_days BETWEEN 1 AND 3650", name="ck_retention_settings_trace_days"),
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    agent_trace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="90")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ModuleLifecycleRecord(Base):
    """Persist only owner-requested disables; descriptor dependencies compute effective availability."""

    __tablename__ = "module_lifecycle_settings"
    __table_args__ = (
        CheckConstraint("owner_id = 1", name="ck_module_lifecycle_single_owner"),
        CheckConstraint("configuration_revision > 0", name="ck_module_lifecycle_revision"),
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    disabled_modules: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

ALIASES = ("reasoning-large", "reasoning-small", "fast", "embedding", "reranker", "vision", "local-private")
_MAPPINGS = "bbd:settings:model-mappings"


async def legacy_aliases(redis: Redis | None, settings: Settings) -> dict[str, ModelMapping]:
    """Load configured model aliases and overlay valid legacy Redis mappings.

    Invalid cached JSON is ignored; Redis is optional and returned mappings are
    limited to the supported aliases.
    """
    configured = {name: ModelMapping(model=model, destination="remote") for name, model in settings.omniroute_models.items() if name in ALIASES}
    if redis is not None:
        for alias, value in (await redis.hgetall(_MAPPINGS)).items():
            try:
                if alias in ALIASES:
                    configured[alias] = ModelMapping.model_validate_json(value)
            except ValueError:
                continue
    return configured


async def save_capability(redis: Redis, result: CapabilityResult) -> None:
    """Cache a capability result using its expiry as a Redis TTL.

    Remaining lifetime is clamped to at least one second, so an already-expired
    result can remain cache-visible for that final second.
    """
    ttl = max(1, int((datetime.fromisoformat(result.expires_at) - datetime.now(UTC)).total_seconds()))
    key = capability_key(result.alias, result.model, result.version, result.capability, result.gateway_identity)
    await redis.set(key, result.model_dump_json(), ex=ttl)


async def list_capabilities(redis: Redis, mappings: dict[str, ModelMapping], gateway_identity: str) -> list[CapabilityResult]:
    """Return TTL-managed cached capabilities matching aliases and gateway identity.

    Malformed entries and results for a different model version or gateway are
    omitted. Redis SCAN iterates incrementally for every alias; COUNT=100 is a
    work hint, not a maximum page size or total-result bound. Expiry is not
    compared here; this reader relies on the stored Redis TTL.
    """
    results: list[CapabilityResult] = []
    for alias, mapping in mappings.items():
        # Redis COUNT is a scan batch hint; it does not cap matches or total work.
        async for key in redis.scan_iter(match=capability_alias_pattern(alias), count=100):
            try:
                value = CapabilityResult.model_validate_json(await redis.get(key))
            except (ValueError, TypeError):
                continue
            if value.gateway_identity == gateway_identity and value.model == mapping.model and value.version == mapping.version:
                results.append(value)
    return results


def new_capability_result(alias: str, mapping: ModelMapping, capability: str, gateway_identity: str, result: str, configuration_revision: int = 0) -> CapabilityResult:
    """Build a capability record with a 24-hour validity window.

    The caller supplies the gateway identity and configuration revision so cached
    results can be scoped to the settings that produced them.
    """
    now = datetime.now(UTC)
    return CapabilityResult(alias=alias, model=mapping.model, version=mapping.version,
        gateway_identity=gateway_identity, configuration_revision=configuration_revision, capability=capability, result=result,
        checked_at=now.isoformat(), expires_at=(now + timedelta(hours=24)).isoformat())
