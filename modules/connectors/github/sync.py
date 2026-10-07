"""Bounded GitHub cursor transitions and owner-side raw-page validation."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from modules.connectors.github.normalization import normalize_github_record
from modules.connectors.github.schemas import (
    MAX_GITHUB_CURSOR_BYTES,
    GitHubBindingFence,
    GitHubCursor,
    GitHubHintClaimProof,
    GitHubResource,
    GitHubResourceCursor,
    GitHubSegmentProof,
    GitHubSourceConfig,
)
from modules.ingestion.schemas import IngestionRecord

OVERLAP = timedelta(hours=24)
MAX_SWEEP_PAGES = 100
MAX_SWEEP_OBJECTS = 10_000


class GitHubValidatedSegment(BaseModel):
    """Carry owner-normalized records and the one durable cursor transition for an accepted page."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    records: tuple[IngestionRecord, ...] = Field(max_length=100)
    cursor_after: str | None = Field(default=None, max_length=MAX_GITHUB_CURSOR_BYTES)
    coverage: Literal["returned_snapshot", "truncated"]
    resource: GitHubResource
    sweep_revision: int = Field(ge=1)
    outcome: Literal["continued", "complete", "exhausted"]
    examined_count: int = Field(ge=0, le=100)
    hint_claim: GitHubHintClaimProof | None = None


def _canonical_json(value: object) -> str:
    """Serialize an owner-generated cursor or proof identity using stable compact UTF-8 JSON."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def encode_github_cursor(cursor: GitHubCursor) -> str:
    """Encode a detached GitHub cursor in canonical form and enforce its provider-specific byte ceiling."""
    encoded = _canonical_json(cursor.model_dump(mode="json"))
    if len(encoded.encode("utf-8")) > MAX_GITHUB_CURSOR_BYTES:
        raise ValueError("github_cursor_invalid")
    return encoded


def github_scope_digest(
    source_id: str,
    source_generation: int,
    connector_revision: int,
    repository_id: str,
    installation_id: str | None,
    app_id: str | None,
    resource_scope: tuple[GitHubResource, ...],
    history_days: int,
) -> str:
    """Hash the fixed numeric repository binding, source revisions, selection, horizon and sync algorithm."""
    value = {
        "algorithm": "github-sync-v1",
        "app_id": app_id,
        "connector_revision": connector_revision,
        "github_history_days": history_days,
        "installation_id": installation_id,
        "repository_id": repository_id,
        "resources": list(resource_scope),
        "source_generation": source_generation,
        "source_id": source_id,
    }
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _cursor_matches(cursor: GitHubCursor, fence: GitHubBindingFence) -> bool:
    """Compare every persisted source and verified identity field before using a cursor."""
    return (
        cursor.source_id == fence.source_id
        and cursor.source_generation == fence.source_generation
        and cursor.connector_revision == fence.connector_revision
        and cursor.repository_id == fence.repository_id
        and cursor.installation_id == fence.installation_id
        and cursor.app_id == fence.app_id
        and cursor.binding_revision == fence.binding_revision
        and cursor.scope_sha256 == fence.scope_sha256
        and tuple(item.resource for item in cursor.resources) == fence.resource_scope
    )


def _initial_cursor(fence: GitHubBindingFence, collected_at: datetime) -> GitHubCursor:
    """Start every enabled resource at the same bounded bootstrap floor and fixed upper instant."""
    upper = collected_at.astimezone(UTC)
    floor = upper - timedelta(days=fence.history_days)
    resources = tuple(
        GitHubResourceCursor(
            resource=resource,
            phase="bootstrap",
            sweep_revision=1,
            page=1,
            examined=0,
            floor=floor,
            upper=upper,
            completed_sweep_revision=0,
            last_outcome="pending",
        )
        for resource in fence.resource_scope
    )
    return GitHubCursor(
        kind="github-sync-v1",
        source_id=fence.source_id,
        source_generation=fence.source_generation,
        connector_revision=fence.connector_revision,
        repository_id=fence.repository_id,
        installation_id=fence.installation_id,
        app_id=fence.app_id,
        binding_revision=fence.binding_revision,
        scope_sha256=fence.scope_sha256,
        turn=0,
        resources=resources,
    )


def decode_github_cursor(value: str | None, fence: GitHubBindingFence, collected_at: datetime) -> GitHubCursor:
    """Decode only canonical, scope-matching state; initialize a first scan from server time when absent."""
    if value is None:
        return _initial_cursor(fence, collected_at)
    try:
        cursor = GitHubCursor.model_validate_json(value)
        if encode_github_cursor(cursor) != value or not _cursor_matches(cursor, fence):
            raise ValueError("cursor scope mismatch")
        return cursor
    except (TypeError, ValueError) as exc:
        raise ValueError("github_cursor_invalid") from exc


def _next_segment(cursor: GitHubCursor, collected_at: datetime) -> tuple[GitHubCursor, int, GitHubResourceCursor, int] | None:
    """Select the next enabled non-exhausted resource and open a bounded sweep only after prior acknowledgement."""
    count = len(cursor.resources)
    for offset in range(count):
        index = (cursor.turn + offset) % count
        state = cursor.resources[index]
        if state.phase == "exhausted":
            continue
        page = state.next_page if state.last_outcome == "continued" else state.page
        if state.last_outcome == "complete":
            floor = (
                state.completed_upper - OVERLAP
                if state.resource != "release" and state.completed_upper is not None
                else state.floor
            )
            phase = "reconcile" if state.resource == "release" else "incremental"
            state = state.model_copy(update={
                "phase": phase,
                "sweep_revision": state.sweep_revision + 1,
                "page": 1,
                "examined": 0,
                "floor": floor,
                "upper": collected_at.astimezone(UTC),
                "next_page": None,
                "last_outcome": "pending",
            })
            page = 1
        return cursor, index, state, page or 1
    return None


def github_segment_path(fence: GitHubBindingFence, resource: GitHubResource, state: GitHubResourceCursor, page: int) -> str:
    """Construct an allowlisted numeric repository endpoint and its fixed bounded page parameters."""
    repository_id = int(fence.repository_id)
    parameters: dict[str, str | int] = {"per_page": 100, "page": page}
    if resource == "issue":
        parameters.update({"state": "all", "sort": "updated", "direction": "desc", "since": state.floor.isoformat().replace("+00:00", "Z")})
        path = f"/repositories/{repository_id}/issues"
    elif resource == "pull":
        parameters.update({"state": "all", "sort": "updated", "direction": "desc"})
        path = f"/repositories/{repository_id}/pulls"
    elif resource == "commit":
        parameters.update({"since": state.floor.isoformat().replace("+00:00", "Z"), "until": state.upper.isoformat().replace("+00:00", "Z")})
        path = f"/repositories/{repository_id}/commits"
    else:
        path = f"/repositories/{repository_id}/releases"
    return f"{path}?{urlencode(parameters)}"


def validate_github_segment(
    fence: GitHubBindingFence,
    config: GitHubSourceConfig,
    cursor_before: str | None,
    proof: GitHubSegmentProof,
    *,
    collected_at: datetime,
) -> GitHubValidatedSegment:
    """Rebuild polling or targeted-resource transitions from raw proof under the current binding.

    Targeted and overflow reconciliation retain the leased polling cursor. Reconcile pages are
    normalized only for their selected resource and carry truncated coverage until the last page.
    """
    proof = GitHubSegmentProof.model_validate(proof.model_dump(mode="python"))
    if proof.fence != fence or proof.collected_at != collected_at.astimezone(UTC):
        raise ValueError("github_segment_fence_stale")
    if proof.hint_claim is not None:
        claim = proof.hint_claim
        if (
            claim.source_id != fence.source_id
            or claim.source_generation != fence.source_generation
            or claim.connector_revision != fence.connector_revision
            or claim.repository_id != fence.repository_id
            or claim.installation_id != fence.installation_id
            or claim.binding_revision != fence.binding_revision
            or claim.resource != proof.resource
            or claim.expires_at <= collected_at
            or claim.intent == "reconcile" and proof.page != claim.reconcile_page
        ):
            raise ValueError("github_hint_claim_stale")
        records: tuple[IngestionRecord, ...] = ()
        if claim.intent == "reconcile" and claim.locator_kind == "repository":
            if proof.target_outcome in {"forbidden", "not_found"}:
                if proof.has_next or proof.raw_items:
                    raise ValueError("github_reconcile_page_invalid")
                return GitHubValidatedSegment(
                    records=(), cursor_after=cursor_before, coverage="truncated",
                    resource=claim.resource, sweep_revision=claim.dirty_revision,
                    outcome="complete", examined_count=0, hint_claim=claim,
                )
            if proof.target_outcome != "found" or proof.has_next != (proof.next_page is not None):
                raise ValueError("github_reconcile_page_invalid")
            if proof.has_next and proof.next_page != claim.reconcile_page + 1:
                raise ValueError("github_reconcile_page_invalid")
            for raw in proof.raw_items:
                _validate_returned_repository_identity(raw, fence.repository_id)
            records = tuple(
                normalize_github_record(
                    claim.resource, raw, coverage="truncated", collected_at=proof.collected_at,
                )
                for raw in proof.raw_items
                if not (claim.resource == "issue" and "pull_request" in raw)
            )
            return GitHubValidatedSegment(
                records=records, cursor_after=cursor_before, coverage="truncated",
                resource=claim.resource, sweep_revision=claim.dirty_revision,
                outcome="continued" if proof.has_next else "complete",
                examined_count=len(proof.raw_items), hint_claim=claim,
            )
        if proof.target_outcome == "found" and claim.locator_kind == "repository" and claim.intent == "visibility_check":  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
            if len(proof.raw_items) != 1 or str(proof.raw_items[0].get("id")) != claim.locator:
                raise ValueError("github_targeted_identity_mismatch")
        if proof.target_outcome == "found" and proof.raw_items and claim.intent not in {"visibility_check", "visibility_lost"}:
            if len(proof.raw_items) != 1:
                raise ValueError("github_targeted_result_invalid")
            raw = proof.raw_items[0]
            _validate_returned_repository_identity(raw, fence.repository_id)
            if claim.locator_kind == "number":
                if str(raw.get("number")) != claim.locator:
                    raise ValueError("github_targeted_identity_mismatch")
                if claim.resource == "issue" and "pull_request" in raw:
                    raise ValueError("github_targeted_identity_mismatch")
            elif claim.locator_kind == "release_id":
                if str(raw.get("id")) != claim.locator:
                    raise ValueError("github_targeted_identity_mismatch")
            elif claim.locator_kind == "sha":
                if str(raw.get("sha", "")).casefold() != claim.locator.casefold():
                    raise ValueError("github_targeted_identity_mismatch")
            records = (normalize_github_record(
                claim.resource, raw, coverage="returned_snapshot", collected_at=proof.collected_at,
            ),)
        elif proof.target_outcome == "found" and claim.locator_kind == "ref" and proof.raw_items:
            raw = proof.raw_items[0]
            records = (normalize_github_record(
                "commit", raw, coverage="truncated", collected_at=proof.collected_at,
            ),)
        if proof.target_outcome not in {"found", "not_found", "forbidden", "partial"}:
            raise ValueError("github_targeted_outcome_missing")
        return GitHubValidatedSegment(
            records=records, cursor_after=cursor_before,
            coverage="truncated" if claim.intent in {"reconcile", "delete_candidate"} else "returned_snapshot",
            resource=claim.resource, sweep_revision=claim.dirty_revision,
            outcome="complete", examined_count=len(proof.raw_items),
            hint_claim=claim,
        )
    cursor = decode_github_cursor(cursor_before, fence, collected_at)
    selection = _next_segment(cursor, collected_at)
    if selection is None:
        raise ValueError("github_scope_exhausted")
    _, index, state, expected_page = selection
    if (
        proof.resource != state.resource
        or proof.page != expected_page
        or proof.sweep_revision != state.sweep_revision
        or proof.scan_floor != state.floor
        or proof.scan_upper != state.upper
    ):
        raise ValueError("github_segment_transition_stale")

    in_scope: list[dict[str, object]] = []
    pull_order: list[datetime] = []
    for item in proof.raw_items:
        timestamp = _resource_timestamp(state.resource, item)
        if state.resource == "pull":
            if timestamp is None:
                raise ValueError("github_pull_update_time_invalid")
            pull_order.append(timestamp)
        if state.resource != "release" and timestamp is not None and state.floor <= timestamp <= state.upper:
            if state.resource != "issue" or "pull_request" not in item:
                in_scope.append(item)
        elif state.resource == "release":
            in_scope.append(item)
    if state.resource == "pull" and any(left < right for left, right in zip(pull_order, pull_order[1:])):  # noqa: RUF007  # style-only rewrite skipped to avoid touching control flow
        raise ValueError("github_pull_page_order_invalid")

    stop_at_floor = bool(
        state.resource == "pull" and pull_order and pull_order[-1] < state.floor
    )
    examined = state.examined + len(proof.raw_items)
    if examined > MAX_SWEEP_OBJECTS:
        raise ValueError("github_sweep_examined_bound_invalid")
    at_ceiling = proof.page >= MAX_SWEEP_PAGES or examined >= MAX_SWEEP_OBJECTS
    exhausted = proof.has_next and at_ceiling and not stop_at_floor
    complete = stop_at_floor or not proof.has_next
    outcome: Literal["continued", "complete", "exhausted"] = (
        "exhausted" if exhausted else "complete" if complete else "continued"
    )
    coverage: Literal["returned_snapshot", "truncated"] = "truncated" if outcome != "complete" else "returned_snapshot"
    records = tuple(
        normalize_github_record(state.resource, raw, coverage=coverage, collected_at=proof.collected_at)
        for raw in in_scope
    )

    updated_state = state.model_copy(update={
        "phase": "exhausted" if exhausted else (
            "reconcile" if state.resource == "release" else "incremental"
        ) if complete else state.phase,
        "page": proof.page,
        "examined": examined,
        "completed_upper": state.upper if complete and state.resource != "release" else state.completed_upper,
        "completed_sweep_revision": state.sweep_revision if complete else state.completed_sweep_revision,
        "next_page": proof.next_page if outcome == "continued" else 101 if exhausted else None,
        "last_outcome": outcome,
    })
    resources = list(cursor.resources)
    resources[index] = updated_state
    advanced = cursor.model_copy(update={"turn": (index + 1) % len(resources), "resources": tuple(resources)})
    return GitHubValidatedSegment(
        records=records,
        cursor_after=encode_github_cursor(advanced),
        coverage=coverage,
        resource=state.resource,
        sweep_revision=state.sweep_revision,
        outcome=outcome,
        examined_count=len(proof.raw_items),
    )


def _validate_returned_repository_identity(item: dict[str, object], expected_id: str) -> None:
    """Reject embedded GitHub repository identity that differs from the numeric request target.

    Some REST resource shapes omit the embedded repository object; then the fixed numeric
    repository endpoint is the adapter's request authority and no mutable owner/name is used.
    """
    repository = item.get("repository")
    if not isinstance(repository, dict):
        base = item.get("base")
        repository = base.get("repo") if isinstance(base, dict) else None
    if isinstance(repository, dict) and repository.get("id") is not None:  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
        if str(repository["id"]) != expected_id:
            raise ValueError("github_targeted_repository_mismatch")


def _resource_timestamp(resource: GitHubResource, item: dict[str, object]) -> datetime | None:
    """Read the provider timestamp used only for supported issue, pull and commit sweep filtering."""
    value: object = None
    if resource in {"issue", "pull"}:
        value = item.get("updated_at")
    elif resource == "commit":
        commit = item.get("commit")
        committer = commit.get("committer") if isinstance(commit, dict) else None
        author = commit.get("author") if isinstance(commit, dict) else None
        value = committer.get("date") if isinstance(committer, dict) else None
        if value is None and isinstance(author, dict):
            value = author.get("date")
    if resource == "release":
        return None
    if value is None:
        raise ValueError("GitHub provider timestamp is missing")
    if not isinstance(value, str):
        raise ValueError("GitHub provider timestamp is invalid")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
    except ValueError as exc:
        raise ValueError("GitHub provider timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GitHub provider timestamp must include a timezone")
    return parsed.astimezone(UTC)
