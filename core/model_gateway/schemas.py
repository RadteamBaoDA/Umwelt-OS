from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

Capability = Literal["chat", "streaming", "embeddings", "structured", "tools", "reranking"]
ProviderKind = Literal["openai", "openrouter", "groq", "gemini", "anthropic", "ollama", "lmstudio", "custom"]
ModelType = Literal["llm", "embedding", "reranker", "vision", "asr", "tts"]
WebSearchProvider = Literal["none", "tavily", "brave", "searxng"]
# Alias slots that need a specific model type; every other alias takes an llm.
ALIAS_MODEL_TYPE = {"embedding": "embedding", "reranker": "reranker", "vision": "vision"}
LOCAL_PROVIDER_KINDS = frozenset({"ollama", "lmstudio"})
MAX_PROVIDERS = 8
DEFAULT_PROVIDER_ID = "default"
ProviderId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]


class RequestPolicy(BaseModel):
    """Immutable request policy bound to an explicitly admitted execution identity.

    Snapshot alone is not authorization: the client's mandatory before_send rechecks
    current account/session, workspace, configuration and resource policy each attempt.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_id: UUID
    actor_user_id: int = Field(gt=0)
    membership_revision: int = Field(gt=0)
    gateway_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    reasoning_allowed: bool = False
    embeddings_allowed: bool = False
    web_search_allowed: bool = False
    local_only: bool = False
    permitted_destinations: frozenset[str] = frozenset()
    reasoning_destinations: frozenset[str] = frozenset()
    embedding_destinations: frozenset[str] = frozenset()
    web_search_destinations: frozenset[str] = frozenset()
    configuration_revision: int = Field(ge=0)


class ModelMapping(BaseModel):
    """Validated configured model name, optional version, and known remote destination marker."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str = Field(default="", max_length=200)
    version: str | None = Field(default=None, max_length=200)
    destination: Literal["unknown", "remote"] = "unknown"
    provider_id: ProviderId | None = None  # None resolves to the "default" provider
    dimensions: int | None = Field(default=None, ge=1, le=4096)  # embedding slot only; set from the embeddings probe


class PrivacySettings(BaseModel):
    """Owner-controlled remote capability flags and destination allowlists."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    allow_remote_reasoning: bool = False
    allow_remote_embeddings: bool = False
    allow_remote_web_search: bool = False
    reasoning_destinations: list[str] = Field(default_factory=list, max_length=8)
    embedding_destinations: list[str] = Field(default_factory=list, max_length=8)
    web_search_destinations: list[str] = Field(default_factory=list, max_length=8)


class ProviderModel(BaseModel):
    """One model id exposed by a provider, tagged with what it is for."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    type: ModelType = "llm"


class ProviderUpdate(BaseModel):
    """One provider in a full-replace provider list; the key is write-only."""
    model_config = ConfigDict(extra="forbid")
    id: ProviderId
    kind: ProviderKind
    label: str = Field(default="", max_length=80)
    base_url: AnyHttpUrl | None = None
    credential_action: Literal["unchanged", "replaced", "removed"] = "unchanged"
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    models: list[ProviderModel] = Field(default_factory=list, max_length=500)


class ProviderRead(BaseModel):
    """Safe provider projection: credential presence only, never the key."""
    id: str
    kind: ProviderKind
    label: str
    base_url: str | None = None
    destination_id: str | None = None
    credential_configured: bool = False
    local: bool = False
    policy_denied: bool = False
    models: list[ProviderModel] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    """Resolved provider inside an execution snapshot; the plaintext key is excluded from repr/serialization."""
    model_config = ConfigDict(frozen=True)
    kind: ProviderKind
    label: str = ""
    base_url: str | None
    api_key: str = Field(default="", repr=False, exclude=True)
    destination_id: str | None
    credential_configured: bool = False
    local: bool = False
    policy_denied: bool = False
    models: tuple[ProviderModel, ...] = ()


class AISettingsUpdate(BaseModel):
    """Validated update payload with expected revision, provider credentials/actions, aliases, privacy, and timeout bounds."""
    model_config = ConfigDict(extra="forbid")
    # None keeps the stored providers; a list replaces them all.
    providers: list[ProviderUpdate] | None = None
    web_search_provider: WebSearchProvider = "none"
    web_search_endpoint: AnyHttpUrl | None = None
    web_search_credential_action: Literal["unchanged", "replaced", "removed"] = "unchanged"
    web_search_api_key: str | None = Field(default=None, max_length=4096, repr=False)
    chat_alias: Literal["reasoning-large", "reasoning-small", "fast"] = "reasoning-large"
    brief_alias: Literal["reasoning-large", "reasoning-small", "fast"] = "reasoning-small"
    aliases: dict[str, ModelMapping] = Field(default_factory=dict)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    request_timeout_seconds: int = Field(default=20, ge=5, le=180)
    expected_revision: int = Field(ge=1)


class ConnectionDraft(BaseModel):
    """Temporary endpoint and credential input for a gateway probe."""
    model_config = ConfigDict(extra="forbid")
    base_url: AnyHttpUrl
    api_key: str = Field(default="", max_length=4096, repr=False)
    provider_id: ProviderId | None = None  # empty api_key falls back to this stored provider's key
    kind: ProviderKind | None = None  # lets an unsaved local provider (no key) be tried


class DraftProbeRequest(BaseModel):
    """Temporary endpoint/model/capability input for an unpersisted connection probe."""
    model_config = ConfigDict(extra="forbid")
    base_url: AnyHttpUrl
    api_key: str = Field(default="", max_length=4096, repr=False)
    model: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, max_length=200)
    capability: Capability
    provider_id: ProviderId | None = None
    kind: ProviderKind | None = None


class ConnectionCheck(BaseModel):
    """Connection probe result and discovered model identifiers."""
    connected: bool
    model_ids: list[str]


class AIExecutionConfig(BaseModel):
    """Owner-scoped detached execution snapshot; plaintext secrets are excluded from repr/serialization.

    Workspace/actor/membership/access/config revisions and gateway identity bind cache and
    request policy. Frozen scalar configuration is preparation, never current authorization;
    use the fresh locked Settings check and caller's resource fence before each send/publish.
    """
    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    actor_user_id: int = Field(gt=0)
    membership_revision: int = Field(gt=0)
    access_configuration_revision: int = Field(gt=0)
    configuration_revision: int = Field(ge=1)
    gateway_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    endpoint_destination_id: str | None
    endpoint_policy_denied: bool = False
    endpoint_allowed_cidrs: tuple[str, ...] = ()
    allow_public_providers: bool = False
    gateway_base_url: str | None
    gateway_api_key: str = Field(repr=False, exclude=True)
    gateway_credential_configured: bool = False
    aliases: dict[str, ModelMapping]
    privacy: PrivacySettings
    chat_alias: str
    brief_alias: str
    request_timeout_seconds: int
    web_search_provider: str
    web_search_endpoint: str | None
    web_search_api_key: str = Field(repr=False, exclude=True)
    web_search_credential_configured: bool = False
    system_prompts: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


class CapabilityResult(BaseModel):
    """Capability evidence for exact workspace/actor/membership/config/model and expiry; no secrets."""
    workspace_id: UUID
    actor_user_id: int = Field(gt=0)
    membership_revision: int = Field(gt=0)
    alias: str
    model: str
    version: str | None = None
    gateway_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_revision: int = Field(ge=1)
    capability: Capability
    result: Literal["supported", "unsupported", "failed"]
    checked_at: str
    expires_at: str


class AISettingsRead(BaseModel):
    """Safe AI settings response exposing credential presence but not credential values."""
    configuration_revision: int
    endpoint_destination_id: str | None = None
    endpoint_policy_denied: bool = False
    providers: list[ProviderRead] = Field(default_factory=list)
    web_search_provider: WebSearchProvider
    web_search_endpoint: AnyHttpUrl | None = None
    web_search_destination_id: str | None = None
    web_search_credential_configured: bool
    chat_alias: Literal["reasoning-large", "reasoning-small", "fast"]
    brief_alias: Literal["reasoning-large", "reasoning-small", "fast"]
    aliases: dict[str, ModelMapping]
    capabilities: list[CapabilityResult]
    privacy: PrivacySettings
    request_timeout_seconds: int


class ProbeRequest(BaseModel):
    """Selects the capability to probe for a configured model mapping."""
    model_config = ConfigDict(extra="forbid")
    capability: Capability


class ModelSettingsRead(BaseModel):
    """Safe model configuration and capability response without returning credential material."""
    aliases: dict[str, ModelMapping]
    capabilities: list[CapabilityResult]
    credential_configured: bool
