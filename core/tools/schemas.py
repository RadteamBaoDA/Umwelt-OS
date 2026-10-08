"""Schemas and data models for tool definitions, invocation results, risk classifications, and approval grants."""

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, computed_field, field_validator

from core.workspaces.schemas import InternalJobScope, WorkspaceContext


class ToolRisk(str, Enum):  # str+Enum kept: StrEnum changes str()/format() behavior
    """Classification of tool execution risk controlling automatic execution and approval requirements."""

    READ_ONLY = "READ_ONLY"
    INTERNAL_WRITE = "INTERNAL_WRITE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"

    @classmethod
    def from_str(cls, value: str) -> "ToolRisk":
        """Normalize a case-insensitive risk name to a canonical ToolRisk enum member.

        Args:
            value: Case-insensitive risk classification string (e.g. 'read_only' or 'READ_ONLY').

        Returns:
            Normalized ToolRisk enum member.

        Raises:
            ValueError: If the risk string does not match any recognized classification.
        """
        normalized = value.strip().upper()
        try:
            return cls[normalized]
        except KeyError:
            # Also try matching values directly
            for member in cls:
                if member.value == normalized:
                    return member
            raise ValueError(f"Unknown tool risk level: {value}")


class ToolDestination(str, Enum):  # str+Enum kept: StrEnum changes str()/format() behavior
    """Trusted privacy class for an output destination; only LOCAL permits local-only data."""

    LOCAL = "local"
    REMOTE = "remote"


class ToolOutputFence(BaseModel):
    """Carry immutable server-owned identity needed to revalidate one native result.

    The bounded invocation sink transports this DTO between registry dispatch and the final
    sender. It is internal authorization evidence, never part of user-facing tool data.
    """

    document_id: UUID
    document_version_id: UUID
    source_id: UUID
    source_generation: StrictInt = Field(ge=1)
    chunk_id: UUID | None = None

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


def compute_argument_hash(arguments: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 digest of normalized tool invocation arguments.

    Args:
        arguments: Key-value dictionary of tool arguments.

    Returns:
        Hex-encoded SHA-256 digest of canonical sorted JSON.
    """
    serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ToolApprovalGrant(BaseModel):
    """Untrusted approval-shaped record that cannot prove authorization without the T3 owner."""

    grant_id: str
    tool_name: str
    tool_version: str
    argument_hash: str
    actor: str
    approved: bool = False
    expires_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True, "extra": "forbid"}

    def is_expired(self, now: datetime | None = None) -> bool:
        """Check whether the approval grant has passed its expiration timestamp.

        Args:
            now: Optional current UTC datetime for testing; defaults to datetime.now(UTC).

        Returns:
            True if an expiration timestamp is set and is in the past; False otherwise.
        """
        if self.expires_at is None:
            return False
        current = now or datetime.now(UTC)
        return self.expires_at <= current

    def matches_args(self, arguments: dict[str, Any]) -> bool:
        """Verify whether the supplied arguments match the immutable hash recorded in this grant.

        Args:
            arguments: The tool arguments being invoked.

        Returns:
            True only when the required argument hash exactly matches canonical JSON arguments.
        """
        return self.argument_hash == compute_argument_hash(arguments)


class ToolDefinition(BaseModel):
    """Specification of a registered tool including its schema, risk level, timeout, and owning module."""

    name: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str = Field(default="1.0.0", min_length=1, max_length=40)
    description: str = Field(default="", max_length=2_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk: ToolRisk = ToolRisk.READ_ONLY
    confirmation_required: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    max_arguments_bytes: int = Field(default=64_000, ge=1, le=64_000)
    max_result_bytes: int = Field(default=256_000, ge=512, le=256_000)
    permissions: tuple[str, ...] = Field(default_factory=tuple)
    module: str = Field(min_length=1)

    model_config = {"frozen": True, "extra": "forbid"}

    @field_validator("risk", mode="before")
    @classmethod
    def normalize_risk(cls, value: Any) -> ToolRisk:
        """Convert string risk classifications to ToolRisk enum members.

        Args:
            value: Either a ToolRisk member or a string name.

        Returns:
            Validated ToolRisk enum member.
        """
        if isinstance(value, str):
            return ToolRisk.from_str(value)
        return cast("ToolRisk", value)  # validator input is already a ToolRisk member

    @field_validator("permissions", mode="before")
    @classmethod
    def freeze_permissions(cls, value: Any) -> tuple[str, ...]:
        """Normalize capability metadata to a bounded immutable tuple."""
        result = tuple(value)
        if len(result) > 32 or any(not isinstance(item, str) or not item for item in result):
            raise ValueError("Tool permissions must be nonempty strings within the supported bound")
        return result

    @property
    def timeout(self) -> float:
        """Alias for timeout_seconds to satisfy compact interface contracts."""
        return self.timeout_seconds

    @property
    def confirmation(self) -> bool:
        """Alias for confirmation_required to satisfy compact interface contracts."""
        return self.confirmation_required

    @computed_field(return_type=str)  # type: ignore[prop-decorator]  # pydantic computed_field over property
    @property
    def schema_fingerprint(self) -> str:
        """Return a stable digest binding name/version, schemas, permissions and execution bounds."""
        identity = {
            "name": self.name, "version": self.version,
            "input_schema": self.input_schema, "output_schema": self.output_schema,
            "module": self.module, "permissions": self.permissions, "risk": self.risk.value,
            "confirmation_required": self.confirmation_required,
            "timeout_seconds": self.timeout_seconds,
            "max_arguments_bytes": self.max_arguments_bytes,
            "max_result_bytes": self.max_result_bytes,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolResult(BaseModel):
    """Bounded dispatch envelope with sanitized error code and exact evidence references."""

    success: bool
    data: Any | None = None
    error: str | None = Field(default=None, max_length=256)
    execution_time_ms: float = Field(default=0.0, ge=0.0, le=120_000.0)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=100)
    error_code: str | None = Field(default=None, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Bound exact evidence identifiers and reject payload-bearing or malformed references."""
        if sum(len(value) for value in values) > 8_192 or any(
            not value or len(value) > 256 or re.fullmatch(r"[A-Za-z0-9._:/-]+", value) is None
            for value in values
        ):
            raise ValueError("Evidence references exceed the allowed identifier format or size")
        return values


class ToolExecutionPrincipal(BaseModel):
    """Server-derived actor and scope fence used for each tool dispatch."""

    actor_id: str
    scope: WorkspaceContext | InternalJobScope
    is_owner: bool = False
    allowed_tools: frozenset[str] = frozenset()
    source_ids: frozenset[str] = frozenset()
    owner_all_sources: bool = False
    destinations: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()

    model_config = {"frozen": True, "extra": "forbid"}

    @field_validator("scope", mode="before")
    @classmethod
    def _typed_scope(cls, value: object) -> object:
        """Reject dict/str input: pydantic would otherwise build a dataclass from a mapping."""
        if not isinstance(value, (WorkspaceContext, InternalJobScope)):
            raise ValueError("Typed workspace scope required")  # noqa: TRY004  # pydantic only wraps ValueError
        return value
