from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.model_gateway.schemas import ModelMapping, PrivacySettings


class OwnerPreferencesRead(BaseModel):
    """Expose persisted theme, locale, timezone, and configuration revision state."""
    configuration_revision: int = Field(ge=1)
    persisted: bool = False
    theme: Literal["light", "dark", "system"]
    locale: Literal["en-us", "vi-vi"]
    timezone: str


class OwnerPreferencesUpdate(BaseModel):
    """Validate a revision-fenced owner preference update."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    theme: Literal["light", "dark", "system"]
    locale: Literal["en-us", "vi-vi"]
    timezone: str = Field(min_length=1, max_length=100)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Accept only timezone identifiers resolvable by the IANA zone database."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Timezone must be a valid IANA timezone") from exc
        return value


class RetentionSettingsRead(BaseModel):
    """Expose the configured agent trace cutoff and immutable retained-data policies."""
    configuration_revision: int = Field(ge=1)
    persisted: bool = False
    agent_trace_days: int = Field(ge=1, le=3650)
    raw_source_retention: Literal["retain"] = "retain"
    document_history_retention: Literal["retain"] = "retain"


class RetentionSettingsUpdate(BaseModel):
    """Validate a revision-fenced trace retention change without permitting source/history deletion."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    agent_trace_days: int = Field(ge=1, le=3650)
    raw_source_retention: Literal["retain"] = "retain"
    document_history_retention: Literal["retain"] = "retain"


class ModuleLifecycleEntry(BaseModel):
    """Describe requested and dependency-derived module availability for owner Settings/navigation."""
    id: str
    name: str
    enabled: bool
    explicitly_disabled: bool
    dependencies: list[str]
    blocked_by: list[str]
    tools: list[str]
    scheduled_jobs: list[str]
    navigation: list[dict[str, str]]


class ModuleLifecycleRead(BaseModel):
    """Expose module availability and the revision for safe owner updates."""
    configuration_revision: int = Field(ge=1)
    persisted: bool = False
    modules: list[ModuleLifecycleEntry]


class ModuleLifecycleUpdate(BaseModel):
    """Validate an optimistic enable/disable request for one descriptor-owned module."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    module_id: str = Field(min_length=1, max_length=128)
    enabled: bool


class MaintenanceSummaryRead(BaseModel):
    """Expose the latest durable bounded-maintenance counts and next eligible timestamp."""
    completed_at: datetime | None = None
    agent_traces_redacted: int = Field(ge=0, default=0)
    temporary_data_deleted: int = Field(ge=0, default=0)
    next_eligible_at: datetime | None = None

__all__ = ["ModelMapping", "PrivacySettings", "OwnerPreferencesRead", "OwnerPreferencesUpdate",
           "RetentionSettingsRead", "RetentionSettingsUpdate", "ModuleLifecycleEntry", "ModuleLifecycleRead",
           "ModuleLifecycleUpdate", "MaintenanceSummaryRead"]
