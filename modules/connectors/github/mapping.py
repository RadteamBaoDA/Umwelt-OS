"""Deterministic canonical mapping of current GitHub document versions into knowledge.

Runs inside the ready-version transaction (source and document fences held by the
caller). Everything goes through the Documents, Entities, Relationships and Timeline
public APIs; this module never touches another module's tables.
"""

import logging
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from modules.knowledge.documents import public as documents
from modules.knowledge.documents.public import ReadyVersionRef
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships import public as relationships
from modules.sources import public as sources
from modules.timeline import public as timeline

logger = logging.getLogger(__name__)

# Bump when the mapping shape changes so identities of earlier mappings stay distinct.
MAPPING_VERSION = "github-map-v1"
_RECORD_TYPES = frozenset({"issue", "pull", "commit", "release"})


def _repository_name(configuration: dict[str, object], canonical_url: str | None) -> str | None:
    """Return the stable ``owner/repo`` name from source configuration, else from a record URL.

    The configuration is order-independent (unlike whichever record URL is processed last);
    the URL is only a fallback for sources whose configuration lacks the pair.
    """
    owner, repo = configuration.get("github_owner"), configuration.get("github_repository")
    if isinstance(owner, str) and isinstance(repo, str) and owner and repo:
        return f"{owner}/{repo}"[:300]
    if not canonical_url:
        return None
    parts = [part for part in urlsplit(canonical_url).path.split("/") if part]
    return f"{parts[0]}/{parts[1]}"[:300] if len(parts) >= 2 else None


def _display_title(record_type: str, title: str, excerpt: str) -> str:
    """Build a readable event title; commits are titled by the first message line, not the bare SHA."""
    if record_type != "commit":
        return title
    body = [line.strip() for line in excerpt.splitlines()[1:] if line.strip()]
    return f"Commit {title[:7]}: {body[0]}" if body else f"Commit {title[:7]}"


async def map_github_version(session: AsyncSession, ready: ReadyVersionRef) -> bool:
    """Map one current GitHub version to its repository/item entities, relationship and event.

    Idempotent: entity lookup uses a deterministic extraction key, memberships and
    relationship evidence are insert-or-keep, and the event is keyed by provider
    record identity so a re-delivered or edited record updates, never duplicates.
    Callers pass only the *current* ready version, so a stale version can never
    overwrite newer state. Deletion needs no code here: all rows hang off document
    evidence that the common document/source deletion workflow already removes.
    Returns False when the source is not a github source or the version is not mappable.
    """
    source = await sources.get_connector_source(session, ready.source_id)
    if source is None or source.provider != "github" or source.generation != ready.source_generation:
        return False
    try:
        # The savepoint rolls back every partial flush (entities, memberships, derived names)
        # if a later step fails, so a failed record never leaves an eventless orphan behind.
        async with session.begin_nested():
            return await _map_version(session, ready, source.configuration)
    except (ValueError, LookupError) as exc:
        # Visible but content-free: only exception class and opaque IDs. The ready-version
        # recovery pass re-runs the idempotent mapper, so this is retried, not permanent.
        logger.warning(
            "github mapping failed for version %s (%s); recovery will retry",
            ready.document_version_id, type(exc).__name__,
        )
        return False


async def _map_version(session: AsyncSession, ready: ReadyVersionRef, configuration: dict[str, object]) -> bool:
    """Do the mapping work for one version inside the caller's savepoint; False means not mappable.

    Only the first (title) chunk and bounded snapshot metadata are used, never the full body,
    so oversized issue/PR/release bodies still produce entities and an event.
    """
    snapshot = (await documents.read_provider_snapshots(session, [ready.document_version_id]))[0]
    record_type = (snapshot.provider_metadata.source_fields if snapshot.provider_metadata else {}).get("record_type")
    if record_type not in _RECORD_TYPES:
        return False
    chunk_id = await documents.get_first_chunk_id(session, ready.document_version_id)
    if chunk_id is None:
        return False
    refs = await documents.read_extraction_evidence_refs(
        session, document_id=ready.document_id, document_version_id=ready.document_version_id,
        source_id=ready.source_id, source_generation=ready.source_generation, chunk_ids=[chunk_id],
    )
    if refs is None:
        return False
    evidence_ref = refs[0]
    identity = f"{MAPPING_VERSION}:{ready.source_id}"

    async def ensure(candidate_key: str, entity_type: str, name: str | None) -> tuple[UUID, UUID]:
        """Find or create a keyed entity, bind this chunk as support and publish its derived name."""
        entity_id = await entities.find_extraction_entity(
            session, extraction_identity=identity, candidate_key=candidate_key,
        ) or await entities.create_extracted_entity(session, entity_type)
        membership_id = await entities.record_extraction_membership(
            session, entity_id=entity_id, evidence_ref=evidence_ref,
            source_generation=ready.source_generation, extraction_identity=identity,
            candidate_key=candidate_key,
            match_fingerprint=sha256(f"{identity}:{candidate_key}".encode()).hexdigest(),
            observed_at=snapshot.observed_at, confidence=1.0,
        )
        if name:
            await entities.publish_derived_field(
                session, entity_id=entity_id, membership_id=membership_id,
                field_name="name", value=name,
            )
        return entity_id, membership_id

    # Repository identity is the source (one source = one numeric repository id).
    repository_id, repository_membership = await ensure(
        "repository", "repository", _repository_name(configuration, snapshot.canonical_url),
    )
    title = _display_title(record_type, snapshot.title, snapshot.excerpt)
    item_id, item_membership = await ensure(
        "item:" + sha256(snapshot.provider_id.encode()).hexdigest(), "event_subject", title,
    )
    await relationships.publish_extracted_relationship(
        session, source_entity_id=item_id, target_entity_id=repository_id,
        relationship_type="part_of", document_version_id=ready.document_version_id,
        chunk_id=chunk_id, source_membership_id=item_membership,
        target_membership_id=repository_membership, confidence=1.0,
    )
    await timeline.publish_provider_event(
        session, source_id=ready.source_id, source_generation=ready.source_generation,
        document_id=ready.document_id, document_version_id=ready.document_version_id,
        chunk_ids=[chunk_id], extraction_identity=identity, record_key=snapshot.provider_id,
        event_type=f"github_{record_type}", title=title, summary=snapshot.excerpt[:1000] or None,
        started_at=snapshot.observed_at, observed_at=snapshot.observed_at,
        metadata={
            "external_id": snapshot.provider_id, "canonical_url": snapshot.canonical_url,
            "repository_entity_id": str(repository_id),
        },
        participants=[(repository_id, "repository"), (item_id, "subject")],
    )
    return True
