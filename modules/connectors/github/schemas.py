"""Bound GitHub App source configuration and safe collection projections."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

GitHubResource = Literal["issue", "pull", "commit", "release"]
GITHUB_RESOURCES: tuple[GitHubResource, ...] = ("issue", "pull", "commit", "release")
MAX_GITHUB_CURSOR_BYTES = 4096
MAX_GITHUB_PAGE_BYTES = 2 * 1024 * 1024


def _bounded_json_counts(value: Any) -> tuple[int, int]:
    """Count only plain JSON nodes and reject non-JSON values, excessive nesting, and non-finite numbers."""
    nodes = 0
    maximum_depth = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        maximum_depth = max(maximum_depth, depth)
        if nodes > 50_000 or depth > 32:
            raise ValueError("GitHub response structure exceeds its bound")
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                raise ValueError("GitHub response object keys must be strings")
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif current is None or isinstance(current, (str, bool, int)) or isinstance(current, float) and current == current and abs(current) != float("inf"):  # noqa: PLR0124  # style-only rewrite skipped to avoid touching control flow
            continue
        else:
            raise ValueError("GitHub response contains a non-JSON value")
    return nodes, maximum_depth


class GitHubSourceConfig(BaseModel):
    """Represent one selected repository and its explicit read-only resource scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    github_owner: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    github_repository: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    include_issues: bool = False
    include_pulls: bool = False
    include_commits: bool = True
    include_releases: bool = True
    github_history_days: StrictInt = Field(default=90, ge=1, le=365)

    @model_validator(mode="after")
    def require_resource(self) -> "GitHubSourceConfig":
        """Reject empty read scopes and path-like or suffix-bearing repository names."""
        if not any((self.include_issues, self.include_pulls, self.include_commits, self.include_releases)):
            raise ValueError("At least one GitHub resource must be enabled")
        if self.github_repository in {".", ".."} or self.github_repository.casefold().endswith(".git"):
            raise ValueError("GitHub repository must be a single repository name")
        return self


def project_github_source_config(configuration: Mapping[str, object]) -> GitHubSourceConfig:
    """Project only GitHub-owned scope fields after the shared connector schema validates common settings."""
    return GitHubSourceConfig.model_validate({
        key: configuration[key]
        for key in GitHubSourceConfig.model_fields
        if key in configuration
    })


class GitHubBindingFence(BaseModel):
    """Bind one accepted page to the current source, resource scope, grant operation and installation identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    source_generation: StrictInt = Field(ge=1)
    connector_revision: StrictInt = Field(ge=1)
    grant_operation_id: UUID
    token_revision: StrictInt = Field(ge=1)
    repository_id: StrictStr = Field(pattern=r"^[1-9][0-9]{0,19}$")
    installation_id: StrictStr | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    app_id: StrictStr | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    binding_revision: StrictInt = Field(ge=1)
    resource_scope: tuple[GitHubResource, ...] = Field(min_length=1, max_length=4)
    history_days: StrictInt = Field(ge=1, le=365)
    scope_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("repository_id", "installation_id", "app_id")
    @classmethod
    def bounded_numeric_id(cls, value: str | None) -> str | None:
        """Reject zero and decimal identifiers wider than a positive signed provider ID."""
        if value is not None and int(value) > 2**63 - 1:
            raise ValueError("GitHub numeric identity exceeds the supported bound")
        return value

    @model_validator(mode="after")
    def validate_resource_order(self) -> "GitHubBindingFence":
        """Require a distinct resource tuple in the fixed issue, pull, commit, release order."""
        if not self.resource_scope or tuple(item for item in GITHUB_RESOURCES if item in self.resource_scope) != self.resource_scope:
            raise ValueError("GitHub resource scope must use distinct fixed order")
        return self


class GitHubResourceCursor(BaseModel):
    """Persist one resource's bounded scan window, provider page and completion proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    resource: GitHubResource
    phase: Literal["bootstrap", "incremental", "reconcile", "exhausted"]
    sweep_revision: StrictInt = Field(ge=1)
    page: StrictInt = Field(ge=1, le=100)
    examined: StrictInt = Field(ge=0, le=10_000)
    floor: datetime
    upper: datetime
    completed_upper: datetime | None = None
    completed_sweep_revision: StrictInt = Field(ge=0)
    next_page: StrictInt | None = Field(default=None, ge=2, le=101)
    last_outcome: Literal["pending", "continued", "complete", "exhausted"]

    @field_validator("floor", "upper", "completed_upper")
    @classmethod
    def normalize_cursor_time(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware cursor watermarks and store normalized UTC instants."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("GitHub cursor timestamps must include a timezone")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_window(self) -> "GitHubResourceCursor":
        """Require a forward fixed scan window and prohibit fabricated release time watermarks."""
        if (
            self.floor > self.upper
            or (self.resource == "release" and self.completed_upper is not None)
            or (self.completed_upper is not None and self.completed_upper > self.upper)
            or self.completed_sweep_revision > self.sweep_revision
            or (self.phase == "exhausted") != (self.last_outcome == "exhausted")
            or (self.phase == "bootstrap" and (self.sweep_revision != 1 or self.completed_sweep_revision != 0))
            or (self.phase == "incremental" and self.resource == "release")
            or (self.phase == "reconcile" and self.resource != "release")
            or (self.resource != "release" and self.completed_sweep_revision > 0 and self.completed_upper is None)
            or (self.last_outcome == "continued" and self.phase not in {"bootstrap", "incremental", "reconcile"})
            or (self.last_outcome == "complete" and self.phase not in {"incremental", "reconcile"})
            or (self.last_outcome == "complete" and self.completed_sweep_revision != self.sweep_revision)
            or (self.last_outcome == "exhausted" and (self.page != 100 or self.next_page != 101))
            or (self.last_outcome == "continued" and (self.next_page is None or self.page >= 100))
            or (self.last_outcome in {"pending", "complete"} and self.next_page is not None)
        ):
            raise ValueError("GitHub resource cursor window is invalid")
        if self.last_outcome == "continued" and self.next_page != self.page + 1:
            raise ValueError("GitHub continued cursor must retain its next page")
        if self.last_outcome not in {"continued", "exhausted"} and self.next_page is not None and self.last_outcome != "pending":
            raise ValueError("GitHub terminal cursor cannot retain a page continuation")
        if self.phase == "exhausted" and self.last_outcome != "exhausted":
            raise ValueError("GitHub exhausted phase must retain its incomplete outcome")
        return self


class GitHubCursor(BaseModel):
    """Store canonical per-resource progress bound to a source generation and verified GitHub scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["github-sync-v1"]
    source_id: UUID
    source_generation: StrictInt = Field(ge=1)
    connector_revision: StrictInt = Field(ge=1)
    repository_id: StrictStr = Field(pattern=r"^[1-9][0-9]{0,19}$")
    installation_id: StrictStr | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    app_id: StrictStr | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    binding_revision: StrictInt = Field(ge=1)
    scope_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    turn: StrictInt = Field(ge=0, le=3)
    resources: tuple[GitHubResourceCursor, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_order_and_size(self) -> "GitHubCursor":
        """Require one ordered cursor per selected resource and the GitHub-specific UTF-8 bound."""
        names = tuple(item.resource for item in self.resources)
        if names != tuple(item for item in GITHUB_RESOURCES if item in names) or self.turn >= len(names):
            raise ValueError("GitHub resource cursor order or turn is invalid")
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_GITHUB_CURSOR_BYTES:
            raise ValueError("GitHub cursor exceeds 4096 UTF-8 bytes")
        return self


class GitHubHintClaimProof(BaseModel):
    """Bind an optional targeted GET to one exact durable source hint claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    source_generation: StrictInt = Field(ge=1)
    connector_revision: StrictInt = Field(ge=1)
    repository_id: StrictStr = Field(pattern=r"^[1-9][0-9]{0,19}$")
    installation_id: StrictStr | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    binding_revision: StrictInt = Field(ge=1)
    hint_id: UUID
    dirty_revision: StrictInt = Field(ge=1)
    resource: GitHubResource
    locator_kind: Literal["number", "release_id", "sha", "ref", "repository", "installation"]
    locator: StrictStr = Field(min_length=1, max_length=256)
    intent: Literal["refresh", "delete_candidate", "visibility_lost", "visibility_check", "reconcile"]
    reconcile_page: StrictInt = Field(default=1, ge=1, le=101)
    lease_token: UUID
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def normalize_claim_expiry(cls, value: datetime) -> datetime:
        """Require an aware exact-claim deadline before network collection begins."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("GitHub hint claim expiry must be timezone-aware")
        return value.astimezone(UTC)


class GitHubSegmentProof(BaseModel):
    """Carry one server-collected raw provider page for owner validation inside ingestion acceptance."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    fence: GitHubBindingFence
    resource: GitHubResource
    page: StrictInt = Field(ge=1, le=100)
    sweep_revision: StrictInt = Field(ge=1)
    scan_floor: datetime
    scan_upper: datetime
    raw_items: tuple[dict[str, Any], ...] = Field(max_length=100)
    raw_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    transport_bytes: StrictInt = Field(ge=0, le=MAX_GITHUB_PAGE_BYTES)
    next_page: StrictInt | None = Field(default=None, ge=2, le=101)
    has_next: bool
    collected_at: datetime
    hint_claim: "GitHubHintClaimProof | None" = None
    target_outcome: Literal["found", "not_found", "forbidden", "partial"] | None = None

    @field_validator("scan_floor", "scan_upper", "collected_at")
    @classmethod
    def normalize_proof_time(cls, value: datetime) -> datetime:
        """Reject naive provider page clocks and normalize accepted instants to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("GitHub segment timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_raw_page(self) -> "GitHubSegmentProof":
        """Enforce the raw JSON tree, canonical digest, byte limit and exact next-page progression."""
        _bounded_json_counts(self.raw_items)
        encoded = json.dumps(self.raw_items, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_GITHUB_PAGE_BYTES or sha256(encoded).hexdigest() != self.raw_sha256:
            raise ValueError("GitHub raw page digest or size is invalid")
        if len(self.raw_items) > 100 or self.scan_floor > self.scan_upper:
            raise ValueError("GitHub raw page scope is invalid")
        if self.has_next != (self.next_page == self.page + 1):
            raise ValueError("GitHub next-page proof is invalid")
        return self


class GitHubGrantSnapshot(BaseModel):
    """Expose only nonsecret OAuth status, provider identity, and bounded expiry metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["ready", "refreshing", "reconciliation_required", "revoked"]
    github_user_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    repository_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    expires_at: datetime | None = None
    validated_at: datetime | None = None

    @field_validator("expires_at", "validated_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        """Normalize provider credential timestamps to UTC instants."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("GitHub grant timestamps must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None


class GitHubConnectionRead(BaseModel):
    """Project safe connection facts and current repository validation time."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    configured: bool
    connected: bool
    state: str
    repository_id: str | None = None
    validated_at: datetime | None = None
    error_code: str | None = None
