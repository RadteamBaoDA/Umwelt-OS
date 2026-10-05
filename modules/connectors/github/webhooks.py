"""Verify GitHub webhook bytes and project supported events into bounded hints."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

MAX_GITHUB_WEBHOOK_BYTES = 256 * 1024
_SIGNATURE = re.compile(r"^sha256=[0-9a-f]{64}$")
_POSITIVE_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_REFRESH_ACTIONS = {
    "assigned", "auto_merge_disabled", "auto_merge_enabled", "closed", "converted_to_draft",
    "demilestoned", "dequeued", "edited", "enqueued", "labeled", "locked", "milestoned",
    "opened", "ready_for_review", "reopened", "review_request_removed", "review_requested",
    "synchronize", "unassigned", "unlabeled", "unlocked", "transferred", "pinned", "unpinned",
}


class GitHubTargetHint(BaseModel):
    """Describe one server-derived invalidation target without carrying provider content."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    resource: Literal["issue", "pull", "commit", "release"]
    locator_kind: Literal["number", "release_id", "sha", "ref", "repository", "installation"]
    locator: StrictStr = Field(min_length=1, max_length=256)
    intent: Literal["refresh", "delete_candidate", "visibility_lost", "visibility_check", "reconcile"]


class VerifiedGitHubDelivery(BaseModel):
    """Represent verified transport metadata and at most 100 bounded event targets."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    receiver_revision: StrictStr = Field(min_length=1, max_length=64)
    delivery_id: StrictStr = Field(min_length=1, max_length=128)
    raw_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    event: StrictStr = Field(min_length=1, max_length=64)
    action: StrictStr | None = Field(default=None, max_length=64)
    app_id: StrictStr
    installation_id: StrictStr | None = None
    repository_id: StrictStr | None = None
    received_at: datetime
    targets: tuple[GitHubTargetHint, ...] = Field(max_length=100)
    disposition: Literal["received", "ignored", "ping"]

    @field_validator("app_id", "installation_id", "repository_id")
    @classmethod
    def validate_numeric_identity(cls, value: str | None) -> str | None:
        """Reject nondecimal, zero, and oversized provider IDs before binding lookup."""
        if value is not None and (_POSITIVE_ID.fullmatch(value) is None or int(value) > 2**63 - 1):
            raise ValueError("GitHub identity is invalid")
        return value

    @field_validator("received_at")
    @classmethod
    def normalize_receipt_time(cls, value: datetime) -> datetime:
        """Require and normalize the transport receipt timestamp to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("GitHub receipt timestamp must be timezone-aware")
        return value.astimezone(UTC)


class GitHubWebhookReceipt(BaseModel):
    """Expose a safe durable delivery acknowledgement without raw payload or source inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    delivery_id: str = Field(min_length=1, max_length=128)
    receipt_id: UUID
    disposition: Literal["received", "duplicate", "ignored", "ping"]
    matched_binding_count: int | None = Field(default=None, ge=0, le=100_000)
    accepted_at: datetime


def verify_github_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Verify the exact request bytes with GitHub's SHA-256 HMAC before JSON parsing."""
    if len(raw_body) > MAX_GITHUB_WEBHOOK_BYTES or not secret or signature is None or _SIGNATURE.fullmatch(signature) is None:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _json_shape(value: Any) -> None:
    """Reject non-JSON values, excessive depth, and oversized structural node counts."""
    nodes = 0
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > 50_000 or depth > 32:
            raise ValueError("Webhook JSON exceeds its structural bound")
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise ValueError("Webhook JSON keys must be strings")
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
        elif node is None or isinstance(node, (str, bool, int)):
            continue
        elif isinstance(node, float) and node == node and abs(node) != float("inf"):
            continue
        else:
            raise ValueError("Webhook JSON contains a non-JSON value")


def _provider_id(value: object) -> str | None:
    """Return one positive signed-64-bit provider ID as canonical decimal text."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return text if _POSITIVE_ID.fullmatch(text) and int(text) <= 2**63 - 1 else None


def _object(value: object) -> dict[str, Any] | None:
    """Narrow an untrusted JSON value to a plain string-keyed object."""
    return value if isinstance(value, dict) and all(isinstance(key, str) for key in value) else None


def parse_github_delivery(
    *,
    raw_body: bytes,
    receiver_revision: str,
    delivery_id: str,
    event: str,
    app_id: str,
    received_at: datetime | None = None,
) -> VerifiedGitHubDelivery:
    """Parse a signature-verified body into bounded hints; never treat payload data as current state.

    The caller must authenticate `raw_body` before calling this function. Supported object targets
    contain identifiers only; current repository access and exact content must be revalidated by
    the connector before collection, deletion, or visibility changes.
    """
    if len(raw_body) > MAX_GITHUB_WEBHOOK_BYTES:
        raise ValueError("Webhook body exceeds 256 KiB")
    payload = json.loads(raw_body)
    _json_shape(payload)
    body = _object(payload)
    if body is None:
        raise ValueError("Webhook body must be a JSON object")
    action = body.get("action")
    if action is not None and (not isinstance(action, str) or len(action) > 64):
        raise ValueError("Webhook action is invalid")

    installation = _object(body.get("installation"))
    repository = _object(body.get("repository"))
    installation_id = _provider_id(installation.get("id")) if installation else None
    repository_id = _provider_id(repository.get("id")) if repository else None
    payload_app_id = _provider_id(installation.get("app_id")) if installation else None
    if payload_app_id is not None and payload_app_id != _provider_id(app_id):
        raise ValueError("Webhook App identity does not match configuration")

    targets: list[GitHubTargetHint] = []
    disposition: Literal["received", "ignored", "ping"] = "received"
    if event == "ping":
        disposition = "ping"
    elif event in {"issues", "pull_request"}:
        item = _object(body.get("issue" if event == "issues" else "pull_request"))
        number = _provider_id(item.get("number")) if item else None
        if repository_id and number and action in _REFRESH_ACTIONS:
            targets.append(GitHubTargetHint(
                resource="issue" if event == "issues" else "pull",
                locator_kind="number", locator=number, intent="refresh",
            ))
        else:
            disposition = "ignored"
    elif event == "release":
        release = _object(body.get("release"))
        release_id = _provider_id(release.get("id")) if release else None
        if repository_id and release_id and action in {"created", "edited", "published", "unpublished", "prereleased", "released"}:
            targets.append(GitHubTargetHint(resource="release", locator_kind="release_id", locator=release_id, intent="refresh"))
        elif repository_id and release_id and action == "deleted":
            targets.append(GitHubTargetHint(resource="release", locator_kind="release_id", locator=release_id, intent="delete_candidate"))
        else:
            disposition = "ignored"
    elif event == "push":
        ref = body.get("ref")
        if repository_id and isinstance(ref, str) and 1 <= len(ref.encode("utf-8")) <= 256 and ref.startswith(("refs/heads/", "refs/tags/")):
            targets.append(GitHubTargetHint(resource="commit", locator_kind="ref", locator=ref, intent="reconcile"))
        else:
            disposition = "ignored"
    elif event == "repository":
        if repository_id and action in {"publicized", "privatized", "edited", "renamed", "transferred"}:
            targets.append(GitHubTargetHint(resource="issue", locator_kind="repository", locator=repository_id, intent="visibility_check"))
        else:
            disposition = "ignored"
    elif event == "public":
        if repository_id:
            targets.append(GitHubTargetHint(resource="issue", locator_kind="repository", locator=repository_id, intent="visibility_check"))
        else:
            disposition = "ignored"
    elif event == "installation":
        if installation_id and action in {"suspend", "deleted"}:
            targets.append(GitHubTargetHint(resource="issue", locator_kind="installation", locator=installation_id, intent="visibility_lost"))
        elif installation_id and action == "unsuspend":
            targets.append(GitHubTargetHint(resource="issue", locator_kind="installation", locator=installation_id, intent="visibility_check"))
        else:
            disposition = "ignored"
    elif event == "installation_repositories":
        removed = body.get("repositories_removed")
        if installation_id and isinstance(removed, list) and len(removed) <= 100:
            for value in removed:
                item = _object(value)
                repository_key = _provider_id(item.get("id")) if item else None
                if repository_key is None:
                    raise ValueError("Removed repository identity is invalid")
                targets.append(GitHubTargetHint(resource="issue", locator_kind="repository", locator=repository_key, intent="visibility_lost"))
        elif installation_id and action in {"added", "removed"}:
            targets.append(GitHubTargetHint(resource="issue", locator_kind="installation", locator=installation_id, intent="visibility_check"))
        else:
            disposition = "ignored"
    else:
        disposition = "ignored"

    if len(targets) > 100:
        raise ValueError("Webhook target count exceeds 100")
    if targets and installation_id is None:
        targets.clear()
        disposition = "ignored"
    return VerifiedGitHubDelivery(
        receiver_revision=receiver_revision, delivery_id=delivery_id,
        raw_sha256=hashlib.sha256(raw_body).hexdigest(), event=event, action=action,
        app_id=_provider_id(app_id) or "", installation_id=installation_id,
        repository_id=repository_id, received_at=received_at or datetime.now(UTC),
        targets=tuple(targets), disposition=disposition,
    )
