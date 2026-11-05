"""Dependency-free GitHub segment-proof contract carried by Ingestion's native collection request.

Ingestion owns these DTOs because ``NativeCollectionBatch.github_segment`` embeds them; the Connectors module
(the producer) imports them back through ``modules.ingestion.public`` and re-exports them from
``modules.connectors.github.schemas``. Keeping this file free of heavy imports lets ``modules.ingestion.schemas`` bind
the field type without importing the Connectors facade (which imports Ingestion), so no import cycle exists.
Pure validation only: no I/O, no side effects.
"""

import json
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
        elif (
            current is None
            or isinstance(current, (str, bool, int))
            or isinstance(current, float)
            and current == current
            and abs(current) != float("inf")
        ):
            continue
        else:
            raise ValueError("GitHub response contains a non-JSON value")
    return nodes, maximum_depth


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
        if (
            not self.resource_scope
            or tuple(item for item in GITHUB_RESOURCES if item in self.resource_scope)
            != self.resource_scope
        ):
            raise ValueError("GitHub resource scope must use distinct fixed order")
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
    intent: Literal[
        "refresh", "delete_candidate", "visibility_lost", "visibility_check", "reconcile"
    ]
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
        encoded = json.dumps(
            self.raw_items,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_GITHUB_PAGE_BYTES or sha256(encoded).hexdigest() != self.raw_sha256:
            raise ValueError("GitHub raw page digest or size is invalid")
        if len(self.raw_items) > 100 or self.scan_floor > self.scan_upper:
            raise ValueError("GitHub raw page scope is invalid")
        if self.has_next != (self.next_page == self.page + 1):
            raise ValueError("GitHub next-page proof is invalid")
        return self
