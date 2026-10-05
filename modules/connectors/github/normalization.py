"""Map bounded GitHub API records into immutable ingestion provenance."""

from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any

from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents.schemas import ProviderRecordMetadata


def normalize_github_record(
    kind: str,
    raw: dict[str, Any],
    *,
    coverage: str,
    collected_at: datetime | None = None,
) -> IngestionRecord:
    """Normalize one issue, pull, commit, or release while binding stable IDs and source clocks.

    Field allowlists keep arbitrary API payloads and secrets out of document metadata. The selected
    field digest is a content version, not a claim that GitHub supplied a modification timestamp.
    """
    allowed = {"issue", "pull", "commit", "release"}
    if kind not in allowed:
        raise ValueError("Unsupported GitHub record type")
    identifier = raw.get("node_id") or raw.get("id")
    if kind == "commit":
        identifier = raw.get("sha")
    if not isinstance(identifier, (str, int)) or isinstance(identifier, bool) or not str(identifier):
        raise ValueError("GitHub record has no stable identifier")
    stable = str(identifier)[:256]
    commit = raw.get("commit")
    fields: dict[str, Any] = {key: raw.get(key) for key in (
        "node_id", "number", "title", "body", "html_url", "state", "draft", "merged",
        "tag_name", "name", "prerelease", "created_at", "updated_at", "published_at",
    ) if raw.get(key) is not None}
    if kind == "commit":
        commit_value = commit if isinstance(commit, dict) else {}
        author = commit_value.get("author") if isinstance(commit_value.get("author"), dict) else {}
        committer = commit_value.get("committer") if isinstance(commit_value.get("committer"), dict) else {}
        fields = {
            key: value for key, value in {
                "sha": raw.get("sha"), "html_url": raw.get("html_url"),
                "message": commit_value.get("message"),
                "author": {key: author.get(key) for key in ("name", "email", "date") if author.get(key) is not None},
                "committer": {key: committer.get(key) for key in ("name", "email", "date") if committer.get(key) is not None},
            }.items() if value is not None
        }
    canonical = raw.get("html_url")
    if not isinstance(canonical, str) or len(canonical) > 2048 or not canonical.startswith("https://github.com/"):
        raise ValueError("GitHub record URL is invalid")
    modified_value = raw.get("updated_at") if kind != "commit" else None
    commit_value = commit if isinstance(commit, dict) else {}
    author = commit_value.get("author") if isinstance(commit_value.get("author"), dict) else {}
    committer = commit_value.get("committer") if isinstance(commit_value.get("committer"), dict) else {}
    published_value = (
        committer.get("date") or author.get("date") if kind == "commit"
        else raw.get("published_at") or raw.get("created_at")
    )
    timestamp_value = modified_value or published_value
    if collected_at is not None and (collected_at.tzinfo is None or collected_at.utcoffset() is None):
        raise ValueError("GitHub collection time must include a timezone")
    collection_time = collected_at.astimezone(UTC) if collected_at is not None else datetime.now(UTC)
    observed = collection_time
    parsed_timestamp = None
    if isinstance(timestamp_value, str):
        try:
            parsed = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                parsed_timestamp = parsed.astimezone(UTC)
                observed = parsed_timestamp
        except ValueError:
            pass
    version_hash = sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    identity = f"github:{kind}:{stable}"
    title_value = str(raw.get("title") or raw.get("name") or raw.get("sha") or identity)
    body_value = str(raw.get("body") or (commit_value.get("message") if kind == "commit" else "") or "")
    content_truncated = len(title_value) > 500 or len(body_value) > 1_000_000
    typed = ProviderRecordMetadata(
        provider="github",
        identity=identity,
        provider_version=version_hash,
        timestamp_basis="provider_modified" if modified_value and parsed_timestamp else "provider_published" if published_value and parsed_timestamp else "collection",
        coverage=coverage,
        content_truncated=content_truncated,
        provider_modified_at=parsed_timestamp if modified_value and parsed_timestamp else None,
        source_fields={"record_type": kind, "node_id": stable, "html_url": canonical},
    )
    title = title_value[:500]
    body = body_value[:1_000_000]
    return IngestionRecord(
        provider_id=identity,
        content=(title + ("\n\n" + body if body else "")),
        observed_at=observed,
        collected_at=collection_time,
        version=version_hash,
        metadata={"title": title, "canonical_url": canonical, "content_type": f"github_{kind}", "provider_record": typed.model_dump(mode="json", exclude_none=True)},
    )
