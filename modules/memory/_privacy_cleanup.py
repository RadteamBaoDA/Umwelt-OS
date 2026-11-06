"""Privacy advisory locks, export-privacy reads and document/Source copied-evidence cleanup sweeps."""

import binascii
import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, UnavailableError
from core.pagination import b64url_decode, b64url_encode
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.memory._common import (
    _DOCUMENT_MEMORY_CLEANUP_REASON,
    _DOCUMENT_MEMORY_UNRESOLVED_REASON,
    DocumentMemoryCleanupProgress,
    SourceCopiedEvidenceScope,
    SourceMemoryCleanupProgress,
    _admit,
    _owned,
)
from modules.memory.models import Memory, MemoryCandidate, MemoryPrivacyRecord
from modules.memory.schemas import MemoryExportPrivacy

if TYPE_CHECKING:
    from modules.knowledge.documents.public import DocumentCleanupEvidenceScope
    from modules.sources.public import SourceFence

logger = logging.getLogger(__name__)

_PRIVACY_LOCK_NAMESPACE = 1297109577
# Cleanup stages run on the worker's job slot: a contended row lock must fail fast so the stage's
# own retry path reschedules it instead of the slot waiting on someone else's transaction. The cap
# lasts for the rest of the caller transaction, so it also bounds the later materialization and
# brief stage locks of the same cleanup pass.
CLEANUP_LOCK_TIMEOUT_MS = 5000


async def bound_cleanup_lock_waits(session: AsyncSession) -> None:
    """Cap lock waits for the rest of the caller transaction so a cleanup stage retries, not stalls."""
    await session.execute(text(f"SET LOCAL lock_timeout = {CLEANUP_LOCK_TIMEOUT_MS}"))


def privacy_lock_key(workspace_id: UUID | str) -> tuple[int, int]:
    """Derive the stable two-int advisory key of one workspace's privacy fence.

    The first int is the fixed namespace; the second is the first four bytes of
    ``sha256("privacy:<workspace uuid>")`` as a signed 32-bit integer (Postgres ``int4``). The
    derivation is pure and process-independent so every API/worker process agrees on the key.
    A 32-bit collision between two workspaces only adds harmless extra contention, never a
    missed exclusion.
    """
    digest = hashlib.sha256(f"privacy:{UUID(str(workspace_id))}".encode()).digest()
    return _PRIVACY_LOCK_NAMESPACE, int.from_bytes(digest[:4], "big", signed=True)


def _note_privacy_lock(session: AsyncSession, key: tuple[int, int], *, shared: bool) -> None:
    """Warn when a transaction asks for the exclusive form after already holding the shared form.

    Shared-then-exclusive on one key is a lock upgrade: two such transactions deadlock (Postgres
    aborts one). Exclusive-then-shared is safe. The record is scoped to the current database
    transaction object so a commit/rollback (which releases advisory xact locks) resets it.
    """
    sync_session = getattr(session, "sync_session", None)
    transaction = sync_session.get_transaction() if sync_session is not None else None
    held = (
        session.info.get("_privacy_lock_modes")
        if isinstance(getattr(session, "info", None), dict)
        else None
    )
    if held is None or held.get("transaction") is not transaction:
        held = {"transaction": transaction, "modes": {}}
        if isinstance(getattr(session, "info", None), dict):
            session.info["_privacy_lock_modes"] = held
    previous = held["modes"].get(key)
    if previous == "shared" and not shared:
        logger.warning("privacy_lock_upgrade_shared_to_exclusive", extra={"key": key})
    if previous != "exclusive":
        held["modes"][key] = "shared" if shared else "exclusive"


async def lock_export_privacy(
    session: AsyncSession,
    workspace_id: UUID | str,
    *,
    shared: bool = False,
) -> None:
    """Serialize Chat consent fences with Memory privacy writes for one workspace.

    ``shared=False`` (default, a privacy WRITER or eraser) takes ``pg_advisory_xact_lock`` on the
    workspace key; ``shared=True`` (a READER that only needs a consistent consent read or a
    publication fence) takes ``pg_advisory_xact_lock_shared`` on the same key. Readers never block
    each other, writers block everyone, and different workspaces never contend. A writer must never
    use the shared form (the call-site table in docs/privacy.md is enforced by an AST test).

    The transaction-scoped lock also fences the absent-row default against a concurrent privacy
    update that would otherwise insert the first MemoryPrivacyRecord after a Chat read. Callers must
    keep the transaction open through their guarded read/write and commit or rollback promptly; this
    never exposes Memory's ORM model across the public module boundary.
    """
    key = privacy_lock_key(workspace_id)
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await session.execute(
        text(f"SELECT {function}(:namespace, :workspace_key)"),
        {"namespace": key[0], "workspace_key": key[1]},
    )
    _note_privacy_lock(session, key, shared=shared)


async def lock_export_privacy_workspaces(
    session: AsyncSession,
    workspace_ids: Iterable[UUID | str],
    *,
    shared: bool = False,
) -> None:
    """Take the privacy fence of every listed workspace in ascending key order (deadlock-free).

    Account-level operations (erasure/export across workspaces) and periodic jobs that touch rows of
    several workspaces use this so two such transactions always acquire overlapping keys in the same
    order. Duplicate ids (and 32-bit key collisions) are locked once.
    """
    keyed = {privacy_lock_key(item): item for item in workspace_ids}
    for key in sorted(keyed):
        await lock_export_privacy(session, keyed[key], shared=shared)


async def lock_export_privacy_in_uow(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Take the privacy advisory lock only when the caller's held admission is still current.

    The non-locking owner read must equal the fence the caller already holds (otherwise 409), so
    a revoked membership or changed configuration never reaches the shared privacy lock.
    """
    if (
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        != access_fence
    ):
        raise ConflictError("Memory cleanup access fence is stale")
    await lock_export_privacy(session, scope.workspace_id)


async def _lock_live_provenance_evidence(
    session: AsyncSession,
    provenance: object,
    *,
    require_copy_evidence: bool,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence | None = None,
) -> None:
    """Acquire privacy-held live evidence locks in Source then Document order and reject stale claims.

    Callers first read a provenance hint, acquire the privacy lock, then use this helper before
    locking the copied Memory/Candidate rows and comparing the locked row to its hint. Every
    owner call runs under the caller's workspace scope; ``access_fence`` (the caller's locked
    admission) is compared before each Source lock so a revoked membership cannot publish.
    """
    if not isinstance(provenance, dict):
        raise ConflictError("Memory provenance is not verifiable")
    from modules.chat import public as chat_public
    from modules.knowledge.documents import public as documents_public
    from modules.sources import public as sources_public
    from modules.sources.public import SourceExportFence

    raw = provenance
    if require_copy_evidence and raw.keys() - {
        "conversation_id",
        "message_id",
        "source_id",
        "document_id",
        "document_version_id",
        "chunk_id",
        "origin",
    }:
        raise ConflictError("Memory provenance contains unsupported copied fields")
    source_value = raw.get("source_id")
    source_id = _provenance_uuid(source_value) if source_value is not None else None
    if source_value is not None and source_id is None:
        raise ConflictError("Memory source provenance is not verifiable")
    document_values = [raw.get(key) for key in ("document_id", "document_version_id", "chunk_id")]
    has_document_identity = any(value is not None for value in document_values)
    document_verified = False
    if has_document_identity:
        document_id, version_id, chunk_id = (_provenance_uuid(value) for value in document_values)
        if document_id is None or version_id is None or chunk_id is None:
            raise ConflictError("Memory document provenance is incomplete")
        try:
            refs = await documents_public.read_evidence_refs(
                session,
                [(version_id, chunk_id)],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                for_write=True,
            )
        except ValueError:
            # Documents raises (rather than returning fewer rows) when the evidence Source vanishes
            # while lock_source waits; a pending purge must hide the copy, not fail with a 500.
            refs = []
        if (
            len(refs) != 1
            or refs[0].document_id != document_id
            or (source_id is not None and refs[0].source_id != source_id)
        ):
            raise ConflictError("Memory document evidence is removed or unavailable")
        source_id = refs[0].source_id
        document_verified = True
    elif source_id is not None:
        source = await sources_public.lock_source(
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        eligible = bool(
            source
            and await sources_public.filter_export_eligible_sources(
                session,
                [
                    SourceExportFence(
                        source_id=source_id,
                        workspace_id=scope.workspace_id,
                        generation=source.generation,
                    )
                ],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            and not await sources_public.source_data_purge_exists(
                session,
                source_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        if not eligible:
            raise ConflictError("Memory source evidence is removed or unavailable")

    conversation_value, message_value = raw.get("conversation_id"), raw.get("message_id")
    conversation_id = (
        _provenance_uuid(conversation_value) if conversation_value is not None else None
    )
    message_id = _provenance_uuid(message_value) if message_value is not None else None
    chat_verified = False
    if conversation_value is not None or message_value is not None:
        if conversation_id is None or message_id is None:
            raise ConflictError("Memory conversation provenance is incomplete")
        chat_verified = (
            await chat_public.read_memory_export_origin(
                session,
                owner_id=_actor(scope),
                conversation_id=conversation_id,
                message_id=message_id,
            )
            is not None
        )
        if not chat_verified:
            raise ConflictError("Memory transcript evidence is removed or unavailable")
    if require_copy_evidence and not (document_verified or chat_verified):
        raise ConflictError("Memory copied-content evidence is not verifiable")


async def read_export_privacy(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> MemoryExportPrivacy:
    """Read only the history-retention value and its persisted-row snapshot fence.

    Admits the caller's owner scope first (members are denied before any query) and reads the
    one privacy row of that workspace actor.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = (
        await session.execute(
            select(
                MemoryPrivacyRecord.store_conversation_history,
                MemoryPrivacyRecord.updated_at,
            ).where(
                MemoryPrivacyRecord.workspace_id == scope.workspace_id,
                MemoryPrivacyRecord.owner_id == _actor(scope),
            )
        )
    ).one_or_none()
    if row is None:
        # Match MemoryService.get_privacy_config without inserting defaults or committing.
        return MemoryExportPrivacy(
            store_conversation_history=True,
            persisted=False,
            updated_at=None,
        )
    store_history, updated_at = row
    if (
        type(store_history) is not bool
        or not isinstance(updated_at, datetime)
        or updated_at.tzinfo is None
        or updated_at.utcoffset() is None
    ):
        raise UnavailableError("Memory export privacy state is invalid")
    return MemoryExportPrivacy(
        store_conversation_history=store_history,
        persisted=True,
        updated_at=updated_at,
    )


def _document_cleanup_scope_fingerprint(scope: "DocumentCleanupEvidenceScope") -> str:
    """Bind a cursor to one immutable Documents identity page."""
    refs = sorted(
        (
            str(item.document_version_id),
            str(item.chunk_id) if item.chunk_id else "",
            item.reference_kind,
        )
        for item in scope.references
    )
    encoded = json.dumps(
        [str(scope.operation_id), str(scope.source_id), str(scope.document_id), refs],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_document_memory_cleanup_cursor(
    scope: "DocumentCleanupEvidenceScope",
    kind: str,
    after: UUID | None,
) -> str:
    """Encode the operation/page/owner-kind keyset position canonically."""
    value = json.dumps(
        [
            str(scope.operation_id),
            _document_cleanup_scope_fingerprint(scope),
            kind,
            str(after) if after else None,
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return b64url_encode(value)


def _decode_document_memory_cleanup_cursor(
    cursor: str,
    scope: "DocumentCleanupEvidenceScope",
) -> tuple[str, UUID | None]:
    """Reject malformed or cross-operation/reference-page Memory continuation tokens."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError
        payload = json.loads(raw)
        if (
            not isinstance(payload, list)
            or len(payload) != 4
            or payload[:2] != [str(scope.operation_id), _document_cleanup_scope_fingerprint(scope)]
            or payload[2] not in {"memories", "candidates"}
        ):
            raise ValueError
        after = UUID(payload[3]) if payload[3] is not None else None
        if (
            payload[3] is not None
            and str(after) != payload[3]
            or _encode_document_memory_cleanup_cursor(scope, payload[2], after) != cursor
        ):
            raise ValueError
        return payload[2], after
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Memory cleanup cursor is invalid") from exc


def _provenance_uuid(value: object) -> UUID | None:
    """Parse a stored provenance UUID without coercing arbitrary values into evidence."""
    if not isinstance(value, (str, UUID)):
        return None
    try:
        parsed = value if isinstance(value, UUID) else UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed


def _document_provenance_match(
    provenance: object,
    scope: "DocumentCleanupEvidenceScope",
) -> tuple[bool, bool]:
    """Return exact-scope match and source-tied unresolved provenance flags."""
    if not isinstance(provenance, dict):
        # An opaque legacy payload cannot be attributed to this particular Document. Reads
        # still fail closed in the verified projection, but charging it to every deletion
        # receipt would make an unrelated malformed row permanently block all Documents.
        return False, False
    versions = {item.document_version_id for item in scope.references}
    chunks = {item.chunk_id for item in scope.references if item.chunk_id is not None}
    document_id = _provenance_uuid(provenance.get("document_id"))
    version_id = _provenance_uuid(provenance.get("document_version_id"))
    chunk_id = _provenance_uuid(provenance.get("chunk_id"))
    matched = document_id == scope.document_id or version_id in versions or chunk_id in chunks
    source_id = _provenance_uuid(provenance.get("source_id"))
    source_tied = source_id == scope.source_id
    has_document_fields = any(
        key in provenance for key in ("document_id", "document_version_id", "chunk_id")
    )
    malformed = (
        provenance.get("document_id") is not None
        and document_id is None
        or provenance.get("document_version_id") is not None
        and version_id is None
        or provenance.get("chunk_id") is not None
        and chunk_id is None
    )
    # A different valid Document UUID is explicit evidence that this copy belongs elsewhere.
    identified_elsewhere = (document_id is not None and document_id != scope.document_id) or (
        document_id is None and version_id is not None and version_id not in versions
    )
    unsupported = bool(
        provenance.keys()
        - {
            "conversation_id",
            "message_id",
            "source_id",
            "document_id",
            "document_version_id",
            "chunk_id",
            "origin",
        }
    )
    # A Source-scoped copy carrying no Document identity at all is not attributable to this
    # Document: the Source-wide sweep owns it, so it is skipped here rather than failed.
    unresolved = (
        not matched
        and not identified_elsewhere
        and source_tied
        and (
            malformed
            or unsupported
            or (
                has_document_fields
                and not all(
                    provenance.get(key) is not None
                    for key in ("document_id", "document_version_id", "chunk_id")
                )
            )
        )
    )
    return matched, unresolved


def _scrub_manual_document_provenance(
    provenance: object,
    scope: "DocumentCleanupEvidenceScope",
) -> dict[str, object] | None:
    """Remove only exact revoked Document identifiers from explicitly manual provenance."""
    if not isinstance(provenance, dict):
        return None
    result = dict(provenance)
    for key, expected in (("document_id", scope.document_id),):
        if _provenance_uuid(result.get(key)) == expected:
            result.pop(key, None)
    versions = {item.document_version_id for item in scope.references}
    chunks = {item.chunk_id for item in scope.references if item.chunk_id is not None}
    if _provenance_uuid(result.get("document_version_id")) in versions:
        result.pop("document_version_id", None)
    if _provenance_uuid(result.get("chunk_id")) in chunks:
        result.pop("chunk_id", None)
    return result


def _scrub_derived_memory(item: Memory, now: datetime) -> None:
    """Physically erase a derived copy while retaining its stable lifecycle identity."""
    item.content = ""
    item.reason = _DOCUMENT_MEMORY_CLEANUP_REASON
    item.provenance = {}
    item.status = "forgotten"
    item.forgotten_at = now
    item.updated_at = now


def _scrub_candidate(item: MemoryCandidate, now: datetime) -> None:
    """Physically erase a derived review copy while retaining its stable lifecycle identity."""
    item.content = ""
    item.reason = _DOCUMENT_MEMORY_CLEANUP_REASON
    item.rejection_reason = None
    item.provenance = {}
    item.status = "expired"
    item.updated_at = now


@dataclass
class _DocumentSweepTally:
    """Counters accumulated across the Memory and Candidate pages of one Document cleanup call."""

    scrubbed_memories: int = 0
    scrubbed_candidates: int = 0
    unresolved: int = 0
    examined: int = 0


async def _lock_linked_candidate(
    session: AsyncSession,
    candidate_id: UUID | None,
    *,
    scope: Scope,
) -> MemoryCandidate | None:
    """Row-lock the workspace/actor-owned Candidate linked to a Memory (``None`` when unlinked/absent)."""
    if candidate_id is None:
        return None
    return await session.scalar(
        select(MemoryCandidate)
        .where(
            MemoryCandidate.id == candidate_id,
            *_owned(MemoryCandidate, scope),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _sweep_document_memory_page(
    session: AsyncSession,
    evidence: "DocumentCleanupEvidenceScope",
    after: UUID | None,
    remaining: int,
    now: datetime,
    tally: _DocumentSweepTally,
    *,
    scope: Scope,
) -> tuple[bool, UUID | None]:
    """Lock and scrub up to ``remaining`` Memory rows after ``after``; returns ``(has_more, last_id)``.

    Selects ``remaining + 1`` rows ``FOR UPDATE`` (the extra is a lookahead that is not touched),
    locks at most one linked Candidate per row, and updates ``tally``. Manual rows lose only the
    matching Document annotations; derived rows matching directly or through their Candidate are
    physically erased; uncertain provenance is counted unresolved. Caller owns the transaction.
    """
    statement = (
        select(Memory).where(*_owned(Memory, scope)).order_by(Memory.id).limit(remaining + 1)
    )
    if after is not None:
        statement = statement.where(Memory.id > after)
    rows = list((await session.scalars(statement.with_for_update())).all())
    has_more = len(rows) > remaining
    rows = rows[:remaining]
    for item in rows:
        matched, uncertain = _document_provenance_match(item.provenance, evidence)
        candidate = await _lock_linked_candidate(session, item.candidate_id, scope=scope)
        candidate_match, candidate_uncertain = (
            _document_provenance_match(candidate.provenance, evidence)
            if candidate is not None
            else (False, False)
        )
        if item.is_manual is True:
            if matched:
                scrubbed = _scrub_manual_document_provenance(item.provenance, evidence)
                if scrubbed is not None and scrubbed != item.provenance:
                    item.provenance = scrubbed
                    item.updated_at = now
                    tally.scrubbed_memories += 1
        elif matched or candidate_match:
            _scrub_derived_memory(item, now)
            tally.scrubbed_memories += 1
        elif uncertain or candidate_uncertain:
            tally.unresolved += 1
        tally.examined += 1
    return has_more, rows[-1].id if rows else after


async def _sweep_document_candidate_page(
    session: AsyncSession,
    evidence: "DocumentCleanupEvidenceScope",
    after: UUID | None,
    remaining: int,
    now: datetime,
    tally: _DocumentSweepTally,
    *,
    scope: Scope,
) -> tuple[bool, UUID | None]:
    """Scrub up to ``remaining`` Candidates after ``after``; returns ``(has_more, last_id)``.

    Runs only after the full Memory sweep so shared Candidate lineage survives until every
    linked derived Memory copy was examined. IDs are listed first, then each row is locked
    individually (a vanished row is skipped without counting). Updates ``tally``.
    """
    id_statement = (
        select(MemoryCandidate.id)
        .where(*_owned(MemoryCandidate, scope))
        .order_by(MemoryCandidate.id)
        .limit(remaining + 1)
    )
    if after is not None:
        id_statement = id_statement.where(MemoryCandidate.id > after)
    candidate_ids = list((await session.scalars(id_statement)).all())
    has_more = len(candidate_ids) > remaining
    candidate_ids = candidate_ids[:remaining]
    for candidate_id in candidate_ids:
        candidate = await _lock_linked_candidate(session, candidate_id, scope=scope)
        if candidate is None:
            continue
        matched, uncertain = _document_provenance_match(candidate.provenance, evidence)
        if matched:
            _scrub_candidate(candidate, now)
            tally.scrubbed_candidates += 1
        elif uncertain:
            tally.unresolved += 1
        tally.examined += 1
    return has_more, candidate_ids[-1] if candidate_ids else after


async def purge_document_copied_evidence_page(
    session: AsyncSession,
    evidence: "DocumentCleanupEvidenceScope",
    *,
    cursor: str | None,
    limit: int = 100,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DocumentMemoryCleanupProgress:
    """Flush one bounded Memory/Candidate sweep for a detached deleted-Document scope.

    The caller owns the transaction, privacy lock, cursor/stage receipt, commit and postcommit
    cache eviction. This hook never reads or locks Source or Document rows; it matches only
    immutable captured IDs and changes at most ``limit`` owner records per page. A Memory page
    locks at most ``limit`` Memory rows plus its one-row lookahead and may inspect one linked
    Candidate per selected Memory. The Candidate sweep starts only after the full Memory sweep,
    preserving shared Candidate lineage until all linked derived Memory copies were examined.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Memory cleanup page size must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if evidence.workspace_id != scope.workspace_id or evidence.actor_user_id != _actor(scope):
        raise ConflictError("Document cleanup evidence belongs to another workspace")
    await bound_cleanup_lock_waits(session)
    kind, after = (
        _decode_document_memory_cleanup_cursor(cursor, evidence) if cursor else ("memories", None)
    )
    tally = _DocumentSweepTally()
    now = datetime.now(UTC)

    next_cursor: str | None  # declared only: the loop always binds it before use
    while tally.examined < limit and kind in {"memories", "candidates"}:
        remaining = limit - tally.examined
        if kind == "memories":
            has_more, last = await _sweep_document_memory_page(
                session,
                evidence,
                after,
                remaining,
                now,
                tally,
                scope=scope,
            )
            if has_more:
                next_cursor = _encode_document_memory_cleanup_cursor(evidence, kind, last)
                break
            kind, after = "candidates", None
            next_cursor = _encode_document_memory_cleanup_cursor(evidence, kind, None)
            if tally.examined == limit:
                break
        else:
            has_more, last = await _sweep_document_candidate_page(
                session,
                evidence,
                after,
                remaining,
                now,
                tally,
                scope=scope,
            )
            next_cursor = (
                _encode_document_memory_cleanup_cursor(evidence, kind, last) if has_more else None
            )
            break

    complete = next_cursor is None
    return DocumentMemoryCleanupProgress(
        complete=complete,
        next_cursor=next_cursor,
        changed=bool(tally.scrubbed_memories or tally.scrubbed_candidates),
        scrubbed_memories=tally.scrubbed_memories,
        scrubbed_candidates=tally.scrubbed_candidates,
        unresolved_count=tally.unresolved,
        unresolved_reason=_DOCUMENT_MEMORY_UNRESOLVED_REASON if tally.unresolved else None,
    )


def _source_memory_cursor(scope: SourceCopiedEvidenceScope, kind: str, after: UUID | None) -> str:
    """Encode a canonical version/operation/source/generation/kind/after keyset position."""
    value = json.dumps(
        [
            1,
            str(scope.operation_id),
            str(scope.source_id),
            scope.generation,
            kind,
            str(after) if after else None,
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return b64url_encode(value)


def _decode_source_memory_cursor(
    cursor: str, scope: SourceCopiedEvidenceScope
) -> tuple[str, UUID | None]:
    """Reject malformed or cross-operation/source/generation Source Memory continuation tokens."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError
        payload = json.loads(raw)
        if (
            not isinstance(payload, list)
            or len(payload) != 6
            or payload[0] != 1
            or payload[1:4] != [str(scope.operation_id), str(scope.source_id), scope.generation]
            or payload[4] not in {"memories", "candidates"}
        ):
            raise ValueError
        after = UUID(payload[5]) if payload[5] is not None else None
        if _source_memory_cursor(scope, payload[4], after) != cursor:
            raise ValueError
        return payload[4], after
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Source Memory cleanup cursor is invalid") from exc


def _scrub_manual_source_provenance(provenance: object) -> dict[str, object]:
    """Remove only revoked Source/Document annotations from verified-manual provenance."""
    result = dict(provenance) if isinstance(provenance, dict) else {}
    for key in ("source_id", "document_id", "document_version_id", "chunk_id"):
        result.pop(key, None)
    return result


def _source_progress(
    scope: SourceCopiedEvidenceScope,
    kind: str,
    last: UUID | None,
    *,
    complete: bool,
    processed: int,
    unresolved: int,
    changed: bool,
) -> SourceMemoryCleanupProgress:
    """Build one page result; an incomplete page carries the cursor bound to its position."""
    return SourceMemoryCleanupProgress(
        complete=complete,
        next_cursor=None if complete else _source_memory_cursor(scope, kind, last),
        processed=processed,
        unresolved_count=unresolved,
        reason=_DOCUMENT_MEMORY_UNRESOLVED_REASON if unresolved else None,
        changed=changed,
    )


async def _sweep_source_memory_page(
    session: AsyncSession,
    evidence: SourceCopiedEvidenceScope,
    after: UUID | None,
    limit: int,
    now: datetime,
    *,
    scope: Scope,
) -> tuple[bool, UUID | None, int, bool]:
    """Scrub one keyset page of Memory rows tied to the purged Source.

    Selects ``limit + 1`` rows ``FOR UPDATE`` (the extra is an untouched lookahead). Verified-manual
    rows lose only Source/Document annotations; derived rows with the Source identity directly or
    through their linked Candidate are erased together with that Candidate. Returns
    ``(has_more, last_id, processed, changed)``. Caller owns the transaction.
    """
    source_text = str(evidence.source_id)
    linked_candidates = select(MemoryCandidate.id).where(
        MemoryCandidate.provenance["source_id"].astext == source_text,
        *_owned(MemoryCandidate, scope),
    )
    statement = (
        select(Memory)
        .where(
            *_owned(Memory, scope),
            or_(
                Memory.provenance["source_id"].astext == source_text,
                Memory.candidate_id.in_(linked_candidates),
            ),
        )
        .order_by(Memory.id)
        .limit(limit + 1)
    )
    if after is not None:
        statement = statement.where(Memory.id > after)
    rows = list(
        (
            await session.scalars(
                statement.with_for_update().execution_options(populate_existing=True)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    changed = False
    processed = 0
    for item in rows:
        own = isinstance(item.provenance, dict) and (
            _provenance_uuid(item.provenance.get("source_id")) == evidence.source_id
        )
        candidate = None
        if item.candidate_id is not None:
            candidate = await session.scalar(
                select(MemoryCandidate)
                .where(
                    MemoryCandidate.id == item.candidate_id,
                    *_owned(MemoryCandidate, scope),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        candidate_linked = (
            candidate is not None
            and isinstance(candidate.provenance, dict)
            and (_provenance_uuid(candidate.provenance.get("source_id")) == evidence.source_id)
        )
        if item.is_manual is True:
            # Same rule as the accepted Document page: manual is independent, strip annotations only.
            if own:
                item.provenance = _scrub_manual_source_provenance(item.provenance)
                item.updated_at = now
                changed = True
        elif own or candidate_linked:
            _scrub_derived_memory(item, now)
            if candidate is not None and (candidate.content or candidate.status != "expired"):
                _scrub_candidate(candidate, now)
            changed = True
        processed += 1
    return has_more, rows[-1].id if rows else after, processed, changed


async def purge_source_copied_evidence_page_in_uow(
    session: AsyncSession,
    evidence: SourceCopiedEvidenceScope,
    *,
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: "SourceFence",
    cursor: str | None = None,
    limit: int = 100,
) -> SourceMemoryCleanupProgress:
    """Flush one bounded Memory/Candidate sweep for a whole purged Source.

    Selection is exact owner-local ``provenance.source_id`` equality plus Memory rows whose linked
    candidate carries that identity; no text inference, and unrelated or opaque records are never
    scanned into this Source's uncertainty. Memories and candidates are separate keyset pages
    (<=``limit`` rows with a one-row lookahead) bound by the cursor to version/operation/source/
    generation/kind/after; locks are plain ``FOR UPDATE`` so the cursor never skips a held row.
    Whole-Source identity suffices to revoke a classified source-derived (non-manual) record:
    content/reason/provenance are erased and a stable tombstone is retained, along with its linked
    candidate. Verified-manual records keep their independent content and lose only Source/Document
    annotations, exactly as the accepted Document page classifies ``is_manual`` records; a linked
    candidate is swept with the rest of the Source's candidates. The caller owns
    the transaction, privacy/Source/operation locks, stage receipt, commit and cache eviction.
    The held ``access_fence`` must still equal a fresh owner read (else 409) and the locked
    ``source_fence`` must match the receipt's Source, generation and this workspace; every
    statement carries the workspace and actor predicates.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Source Memory cleanup page size must be between 1 and 100")
    if (
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        != access_fence
    ):
        raise ConflictError("Memory cleanup access fence is stale")
    if (
        source_fence.workspace_id != scope.workspace_id
        or source_fence.id != evidence.source_id
        or source_fence.generation != evidence.generation
        or scope.source_id != evidence.source_id
        or scope.source_generation != evidence.generation
    ):
        raise ConflictError("Source cleanup fence does not match the receipt")
    await bound_cleanup_lock_waits(session)
    kind, after = _decode_source_memory_cursor(cursor, evidence) if cursor else ("memories", None)
    now = datetime.now(UTC)
    changed = False
    unresolved = processed = 0

    if kind == "memories":
        has_more, last, processed, changed = await _sweep_source_memory_page(
            session,
            evidence,
            after,
            limit,
            now,
            scope=scope,
        )
        if has_more:
            return _source_progress(
                evidence,
                "memories",
                last,
                complete=False,
                processed=processed,
                unresolved=unresolved,
                changed=changed,
            )
        kind, after = "candidates", None
        if processed >= limit:
            return _source_progress(
                evidence,
                kind,
                None,
                complete=False,
                processed=processed,
                unresolved=unresolved,
                changed=changed,
            )

    remaining = limit - processed
    candidate_statement = (
        select(MemoryCandidate)
        .where(
            MemoryCandidate.provenance["source_id"].astext == str(evidence.source_id),
            *_owned(MemoryCandidate, scope),
        )
        .order_by(MemoryCandidate.id)
        .limit(remaining + 1)
    )
    if after is not None:
        candidate_statement = candidate_statement.where(MemoryCandidate.id > after)
    candidates = list(
        (
            await session.scalars(
                candidate_statement.with_for_update().execution_options(populate_existing=True)
            )
        ).all()
    )
    has_more = len(candidates) > remaining
    candidates = candidates[:remaining]
    for candidate in candidates:
        _scrub_candidate(candidate, now)
        changed = True
        processed += 1
    return _source_progress(
        evidence,
        "candidates",
        candidates[-1].id if candidates else after,
        complete=not has_more,
        processed=processed,
        unresolved=unresolved,
        changed=changed,
    )
