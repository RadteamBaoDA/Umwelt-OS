from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

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

class TranslationSettingsRead(BaseModel):
    """Workspace translation choice; unrelated to the viewer's UI locale."""
    enabled: bool = False
    target_language: Literal["vi", "en"] = "vi"
    configuration_revision: int = Field(default=1, ge=1)


class TranslationSettingsUpdate(BaseModel):
    """Owner update must compare this revision under a row lock; the server picks the next one."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    target_language: Literal["vi", "en"]
    expected_revision: int = Field(ge=1)


class SystemPromptEntry(BaseModel):
    """One editable prompt: effective text, built-in default and whether it is customized."""
    key: str
    value: str
    default: str
    customized: bool


class SystemPromptsRead(BaseModel):
    """All editable system prompts with the revision used for optimistic updates."""
    configuration_revision: int = Field(ge=1)
    prompts: list[SystemPromptEntry]


class SystemPromptsUpdate(BaseModel):
    """Partial update; ``None`` or blank resets a key to its default."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    prompts: dict[str, str | None] = Field(min_length=1)


CredentialAction = Literal["unchanged", "replaced", "removed"]


class WebSearchEntryRead(BaseModel):
    """Expose one ordered search destination and credential presence, never its key."""
    provider: Literal["tavily", "brave", "searxng", "omniroute"]
    endpoint: str
    credential_configured: bool
    destination_id: str | None = None


class WebSearchRead(BaseModel):
    """Return ordered search destinations and their shared remote-search consent."""
    configuration_revision: int = Field(ge=1)
    providers: list[WebSearchEntryRead]
    allow_remote_web_search: bool


class WebSearchEntryUpdate(BaseModel):
    """Validate one search destination and write-only credential action."""
    model_config = ConfigDict(extra="forbid")
    provider: Literal["tavily", "brave", "searxng", "omniroute"]
    endpoint: AnyHttpUrl
    credential_action: CredentialAction = "unchanged"
    api_key: str | None = Field(default=None, max_length=4096, repr=False)


class WebSearchUpdate(BaseModel):
    """Replace up to four ordered search destinations under the shared revision fence."""
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    providers: list[WebSearchEntryUpdate] = Field(max_length=4)
    allow_remote_web_search: bool = False


class WebSearchTest(BaseModel):
    """Probe one draft; a blank key may reuse a saved key only for the same endpoint."""
    model_config = ConfigDict(extra="forbid")
    provider: Literal["tavily", "brave", "searxng", "omniroute"]
    endpoint: AnyHttpUrl
    api_key: str = Field(default="", max_length=4096, repr=False)


class ConnectionTestResult(BaseModel):
    """Outcome of a connection test; ``error`` is a short code, never a URL or secret."""
    ok: bool
    error: str | None = None
    detail: int | None = None  # result count (web search) or HTTP status (n8n)


class N8nRead(BaseModel):
    """n8n connection settings; secrets are never returned. ``*_source`` says where the effective value comes from."""
    configuration_revision: int = Field(ge=1)
    service_url: str
    api_key_configured: bool
    webhook_token_configured: bool
    service_url_source: Literal["database", "environment"]
    api_key_source: Literal["database", "environment", "none"]
    webhook_token_source: Literal["database", "environment", "none"]
    enabled: bool = True
    callback_base_url: str = ""


class N8nUpdate(BaseModel):
    """Full replace of the stored n8n settings; ``service_url`` None clears it (environment fallback)."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    service_url: AnyHttpUrl | None = None
    api_key_action: CredentialAction = "unchanged"
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    webhook_token_action: CredentialAction = "unchanged"
    webhook_token: str | None = Field(default=None, max_length=4096, repr=False)
    enabled: bool | None = None  # None leaves the stored kill switch unchanged


class N8nTest(BaseModel):
    """Optional draft; blank fields fall back to the effective (stored, then environment) value."""
    model_config = ConfigDict(extra="forbid")

    service_url: AnyHttpUrl | None = None
    api_key: str = Field(default="", max_length=4096, repr=False)


VizRouterKind = Literal["internal", "llm", "typesafe", "openrouter", "cloudflare"]
VizRouterMode = Literal["json_schema", "forced_tool"]
VizRouterApi = Literal["chat", "decisions"]  # openrouter only; rows saved without it are chat


class VizRouterRead(BaseModel):
    """Visualization router settings; the TypeSafe key is never returned, only whether one is stored."""
    configuration_revision: int = Field(ge=1)
    kind: VizRouterKind = "internal"
    provider_id: str | None = None
    model: str = ""
    mode: VizRouterMode = "json_schema"
    api: VizRouterApi = "chat"
    typesafe_key_configured: bool = False
    openrouter_key_configured: bool = False
    cloudflare_key_configured: bool = False
    account_id: str | None = None
    gateway_url: str | None = None
    allow_remote_visualization_routing: bool = False
    destination_id: str | None = None


class VizRouterUpdate(BaseModel):
    """Full replace of the visualization router settings under its own revision check.

    ``allow_remote_visualization_routing`` is consent bound to the destination chosen here: changing the
    provider or kind in a later save clears it.
    """
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    kind: VizRouterKind = "internal"
    provider_id: str | None = Field(default=None, max_length=32)
    model: str = Field(default="", max_length=200)
    mode: VizRouterMode = "json_schema"
    api: VizRouterApi = "chat"  # openrouter: "decisions" uses the Decisions API (alpha); ignored (stored chat) elsewhere
    typesafe_key_action: CredentialAction = "unchanged"
    typesafe_key: str | None = Field(default=None, max_length=4096, repr=False)
    # openrouter / cloudflare: the key action applies to the selected kind's own key
    api_key_action: CredentialAction = "unchanged"
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    account_id: str | None = Field(default=None, max_length=64)  # cloudflare
    gateway_url: str | None = Field(default=None, max_length=500)  # cloudflare optional AI Gateway base URL (https)
    allow_remote_visualization_routing: bool = False


class VizRouterTestResult(BaseModel):
    """Outcome of Test routing: ``mode`` is the mode that worked (LLM classifier only); ``error`` is a short code."""
    ok: bool
    category: str | None = None
    mode: VizRouterMode | None = None
    error: str | None = None


class VizDecisionModelsRequest(BaseModel):
    """Discovery input: an optional transient OpenRouter key; blank uses the saved key (never persisted here)."""
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, max_length=4096, repr=False)


class VizDecisionModel(BaseModel):
    """One discovered Decisions-API model."""
    id: str = Field(max_length=200)
    input_modalities: list[str] = Field(default_factory=list, max_length=8)


class VizDecisionModelsResult(BaseModel):
    """Bounded discovery outcome: ``models`` on success, a short ``error`` code (never URL/body/key) otherwise."""
    ok: bool
    models: list[VizDecisionModel] = Field(default_factory=list, max_length=200)
    error: str | None = None


__all__ = [
    "ConnectionTestResult",
    "MaintenanceSummaryRead",
    "ModelMapping",
    "ModuleLifecycleEntry",
    "ModuleLifecycleRead",
    "ModuleLifecycleUpdate",
    "N8nRead",
    "N8nTest",
    "N8nUpdate",
    "OwnerPreferencesRead",
    "OwnerPreferencesUpdate",
    "PrivacySettings",
    "RetentionSettingsRead",
    "RetentionSettingsUpdate",
    "SystemPromptEntry",
    "SystemPromptsRead",
    "SystemPromptsUpdate",
    "TranslationSettingsRead",
    "TranslationSettingsUpdate",
    "VizDecisionModel",
    "VizDecisionModelsRequest",
    "VizDecisionModelsResult",
    "VizRouterRead",
    "VizRouterTestResult",
    "VizRouterUpdate",
    "WebSearchRead",
    "WebSearchTest",
    "WebSearchUpdate",
]
