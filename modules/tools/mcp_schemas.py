"""Strict bounded HTTP and detached DTO contracts for persisted MCP management."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from core.mcp_endpoint import normalize_mcp_url


class McpTransport(str, Enum):  # str+Enum kept: StrEnum changes str()/format() behavior
    """Supported server transport names; stdio authority is an admin deployment profile only."""
    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"


class McpRisk(str, Enum):  # str+Enum kept: StrEnum changes str()/format() behavior
    """Explicit owner review classification for a selected immutable capability."""
    READ_ONLY = "READ_ONLY"
    INTERNAL_WRITE = "INTERNAL_WRITE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


class McpStrictModel(BaseModel):
    """Reject unknown fields and attribute rebinding; nested JSON remains mutable and projections copy it."""
    model_config = ConfigDict(extra="forbid", frozen=True)


class CredentialUpdate(McpStrictModel):
    """Write-only credential operation; retain/remove carry no value, replace requires one."""
    action: str
    value: SecretStr | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> "CredentialUpdate":
        """Require a secret only for replacement, preventing ambiguous keep/remove writes."""
        if self.action not in {"retain", "replace", "remove"}:
            raise ValueError("Unsupported credential operation")
        if (self.action == "replace") != (self.value is not None):
            raise ValueError("Credential value is required only for replace")
        if self.value is not None and not self.value.get_secret_value():
            raise ValueError("Credential cannot be empty")
        return self


class ConnectionDraft(McpStrictModel):
    """Bounded HTTP endpoint or admin profile selector; it contains no actor, launch strings, or approval authority."""
    name: str = Field(min_length=1, max_length=120)
    transport: McpTransport
    endpoint: str | None = Field(default=None, max_length=2048)
    deployment_profile_id: str | None = Field(default=None, min_length=1, max_length=80)
    auth_method: str = Field(default="none", pattern="^(none|bearer)$")
    credential_update: CredentialUpdate = Field(default_factory=lambda: CredentialUpdate(action="retain"))
    timeout_seconds: int = Field(default=30, ge=1, le=60)

    @model_validator(mode="after")
    def validate_target(self) -> "ConnectionDraft":
        """Enforce one target, canonical HTTP URLs, and stdio's credential-free profile-only contract."""

        if (self.endpoint is None) == (self.deployment_profile_id is None):
            raise ValueError("Specify exactly one endpoint or deployment profile")
        if (self.transport == McpTransport.STREAMABLE_HTTP) != (self.endpoint is not None):
            raise ValueError("HTTP requires endpoint and stdio requires deployment profile")
        if self.transport == McpTransport.STDIO and (
            self.auth_method != "none" or self.credential_update.action == "replace"
        ):
            raise ValueError("stdio profiles cannot use bearer authentication or supplied credentials")
        if self.endpoint is not None:
            scheme, _host, _port, _path = normalize_mcp_url(self.endpoint)
            if scheme == "http" and self.auth_method != "none":
                raise ValueError("Plain HTTP MCP endpoints cannot use bearer authentication")
        if self.auth_method == "none" and self.credential_update.action == "replace":
            raise ValueError("Unauthenticated connections cannot set a bearer credential")
        if self.auth_method == "bearer" and self.credential_update.action == "remove":
            raise ValueError("Bearer authentication requires a credential")
        return self


class ConnectionSave(McpStrictModel):
    """Optimistic connection edit request tied to the expected durable revision."""
    expected_revision: int = Field(ge=0)
    draft: ConnectionDraft


class ConnectionRead(McpStrictModel):
    """Credential-free snapshot exposing a stdio review hash but no manifest launch inputs."""
    id: UUID
    name: str
    transport: McpTransport
    endpoint: str | None
    deployment_profile_id: str | None
    deployment_profile_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    revision: int
    enabled: bool
    auth_method: str
    credential_configured: bool
    timeout_seconds: int
    health: str | None
    error_code: str | None
    updated_at: datetime


class CapabilityDescriptor(McpStrictModel):
    """Server-returned descriptor after bounds and JSON shape validation; never caller authority."""
    kind: str
    remote_key: str = Field(min_length=1, max_length=2048)
    descriptor: dict[str, Any]
    descriptor_hash: str = Field(pattern="^[0-9a-f]{64}$")

    @field_validator("descriptor")
    @classmethod
    def enforce_descriptor_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject oversized descriptor JSON before storing immutable discovery content."""
        import json
        if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 65_536:
            raise ValueError("Descriptor exceeds 64 KiB")
        return value


class DiscoveryPersist(McpStrictModel):
    """Internal server-owned discovery payload carrying the connection's captured profile hash."""
    connection_revision: int = Field(ge=1)
    deployment_profile_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    protocol: str = Field(min_length=1, max_length=40)
    server_info: dict[str, Any] = Field(default_factory=dict)
    schema_set_hash: str = Field(pattern="^[0-9a-f]{64}$")
    capabilities: tuple[CapabilityDescriptor, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_aggregate(self) -> "DiscoveryPersist":
        """Require unique capability keys and bound the complete snapshot before it is committed atomically."""
        import json
        keys = [(item.kind, item.remote_key) for item in self.capabilities]
        total = len(json.dumps([item.model_dump(mode="json") for item in self.capabilities], separators=(",", ":")).encode())
        if len(set(keys)) != len(keys) or total > 1_000_000:
            raise ValueError("Discovery descriptors must be unique and fit within 1 MiB")
        if len(json.dumps(self.server_info, separators=(",", ":")).encode()) > 8_192:
            raise ValueError("Server information exceeds its size bound")
        return self


class CapabilityRead(McpStrictModel):
    """Detached descriptor projection; nested JSON is a copied payload, not a deeply immutable authority object."""
    id: UUID
    kind: str
    remote_key: str
    descriptor: dict[str, Any]
    descriptor_hash: str


class DiscoveryRead(McpStrictModel):
    """Detached discovery snapshot exposing its deployment review identity and descriptor data."""
    id: UUID
    connection_id: UUID
    connection_revision: int
    deployment_profile_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    protocol: str
    schema_set_hash: str
    capabilities: tuple[CapabilityRead, ...]
    created_at: datetime


class GrantChoice(McpStrictModel):
    """Exact capability selection and scoped owner review; wildcards are impossible."""
    capability_id: UUID
    descriptor_hash: str = Field(pattern="^[0-9a-f]{64}$")
    purpose: str
    risk: McpRisk
    source_ids: tuple[UUID, ...] = Field(max_length=100)
    destinations: tuple[str, ...] = Field(max_length=8)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        """Preserve no-expiry grants; supplied expiries must be future timezone-aware values normalized to UTC."""
        from datetime import UTC
        from datetime import datetime as dt
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Grant expiry must include a timezone")
        normalized = value.astimezone(UTC)
        if normalized <= dt.now(UTC):
            raise ValueError("Grant expiry must be in the future")
        return normalized

    @model_validator(mode="after")
    def validate_choice(self) -> "GrantChoice":
        """Require explicit destination and limit use of collection grants to read-only semantics."""
        if self.purpose not in {"chat", "collection"} or not self.destinations:
            raise ValueError("Grant purpose and destinations must be explicit")
        if self.purpose == "collection" and self.risk != McpRisk.READ_ONLY:
            raise ValueError("Collection grants must be reviewed as read-only")
        if len(set(self.source_ids)) != len(self.source_ids) or len(set(self.destinations)) != len(self.destinations):
            raise ValueError("Grant scopes must not contain duplicates")
        if any(not value or len(value) > 255 for value in self.destinations):
            raise ValueError("Grant destinations must be nonempty strings of at most 255 characters")
        if sum(len(value.encode("utf-8")) for value in self.destinations) > 1_024:
            raise ValueError("Grant destination scope exceeds its aggregate size limit")
        return self


class GrantSelection(McpStrictModel):
    """Atomic replacement request anchored to one current connection and discovery revision."""
    expected_connection_revision: int = Field(ge=1)
    discovery_id: UUID
    selections: tuple[GrantChoice, ...] = Field(max_length=200)


class GrantRead(McpStrictModel):
    """Detached grant scope and copied profile identity without launch or authentication material."""
    id: UUID
    connection_id: UUID
    capability_id: UUID
    descriptor_hash: str
    reviewed_connection_revision: int
    reviewed_profile_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    grant_revision: int
    purpose: str
    risk: McpRisk
    source_ids: tuple[UUID, ...]
    destinations: tuple[str, ...]
    expires_at: datetime | None
    revoked_at: datetime | None


class ExecutionFence(McpStrictModel):
    """Server-derived detached pre-dispatch authority including the exact profile hash, never launch material."""
    connection_id: UUID
    connection_revision: int
    deployment_profile_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    grant_id: UUID
    grant_revision: int
    discovery_id: UUID
    descriptor_hash: str
    remote_capability_key: str
    purpose: str
    source_ids: tuple[UUID, ...]
    destination_id: str
    timeout_seconds: int
    limits: dict[str, int]


class InboundBinding(McpStrictModel):
    """Exact native registered tool contract authorized for one inbound client."""
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=40)
    schema_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")


class InboundClientCreate(McpStrictModel):
    """Create a separately scoped external identity with explicit audience, tools, sources, and expiry."""
    name: str = Field(min_length=1, max_length=120)
    audience: str = Field(min_length=1, max_length=255)
    tool_bindings: tuple[InboundBinding, ...] = Field(min_length=1, max_length=50)
    source_ids: tuple[UUID, ...] = Field(max_length=100)
    capabilities: tuple[str, ...] = Field(default=(), max_length=20)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_bindings(self) -> "InboundClientCreate":
        """Require bounded nonempty scope labels, a future aware expiry, and unique exact tool contracts."""
        from datetime import UTC
        from datetime import datetime as dt
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Inbound client expiry must include a timezone")
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))
        if self.expires_at <= dt.now(UTC) or len({(b.name, b.version, b.schema_fingerprint) for b in self.tool_bindings}) != len(self.tool_bindings):
            raise ValueError("Inbound client needs unique bindings and a future expiry")
        if len(set(self.source_ids)) != len(self.source_ids) or len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Inbound client scopes must be unique")
        if any(not value or len(value) > 80 for value in self.capabilities):
            raise ValueError("Inbound capability labels must be nonempty strings of at most 80 characters")
        if sum(len(value.encode("utf-8")) for value in self.capabilities) > 1_024:
            raise ValueError("Inbound capability scope exceeds its aggregate size limit")
        return self


class InboundClientRead(McpStrictModel):
    """Inbound client metadata with no raw bearer token."""
    id: UUID
    name: str
    token_prefix: str
    audience: str
    bindings: tuple[InboundBinding, ...]
    source_ids: tuple[UUID, ...]
    capabilities: tuple[str, ...]
    expires_at: datetime
    revoked_at: datetime | None
    revision: int


class InboundClientIssued(McpStrictModel):
    """One-time token issuance response; clients are never able to retrieve this token later."""
    client: InboundClientRead
    token: SecretStr


class InboundPrincipal(McpStrictModel):
    """Verified detached inbound identity used to derive a non-owner execution principal."""
    client_id: UUID
    workspace_id: UUID
    owner_id: int
    audience: str
    revision: int = Field(ge=1)
    bindings: tuple[InboundBinding, ...]
    source_ids: tuple[UUID, ...]
    capabilities: tuple[str, ...]
    destination_id: str
