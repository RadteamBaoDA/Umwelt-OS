from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

Capability = Literal["chat", "streaming", "embeddings", "structured", "tools", "reranking"]


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


class PrivacySettings(BaseModel):
    """Owner-controlled remote capability flags and destination allowlists."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    allow_remote_reasoning: bool = False
    allow_remote_embeddings: bool = False
    allow_remote_web_search: bool = False
    reasoning_destinations: list[str] = Field(default_factory=list, max_length=8)
    embedding_destinations: list[str] = Field(default_factory=list, max_length=8)
    web_search_destinations: list[str] = Field(default_factory=list, max_length=8)


class AISettingsUpdate(BaseModel):
    """Validated update payload with expected revision, provider credentials/actions, aliases, privacy, and timeout bounds."""
    model_config = ConfigDict(extra="forbid")
    omniroute_base_url: AnyHttpUrl | None = None
    omniroute_credential_action: Literal["unchanged", "replaced", "removed"] = "unchanged"
    omniroute_api_key: str | None = Field(default=None, max_length=4096, repr=False)
    web_search_provider: Literal["none", "tavily", "brave"] = "none"
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


class DraftProbeRequest(BaseModel):
    """Temporary endpoint/model/capability input for an unpersisted connection probe."""
    model_config = ConfigDict(extra="forbid")
    base_url: AnyHttpUrl
    api_key: str = Field(default="", max_length=4096, repr=False)
    model: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, max_length=200)
    capability: Capability


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
    omniroute_base_url: str | None
    omniroute_api_key: str = Field(repr=False, exclude=True)
    omniroute_credential_configured: bool = False
    aliases: dict[str, ModelMapping]
    privacy: PrivacySettings
    chat_alias: str
    brief_alias: str
    request_timeout_seconds: int
    web_search_provider: str
    web_search_endpoint: str | None
    web_search_api_key: str = Field(repr=False, exclude=True)
    web_search_credential_configured: bool = False


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
    omniroute_base_url: AnyHttpUrl | None = None
    endpoint_destination_id: str | None = None
    endpoint_policy_denied: bool = False
    omniroute_credential_configured: bool
    web_search_provider: Literal["none", "tavily", "brave"]
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
