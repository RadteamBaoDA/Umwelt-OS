"""Document cleanup receipts: identities, evidence scope capture, Source progress and wakeups."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import (
    ColumnElement,
    and_,
    func,
    insert,
    literal,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, ForbiddenError
from core.events import DomainEvent
from core.workspaces import public as workspaces
from core.workspaces.admission import actor_id
from core.workspaces.schemas import (
    AccessFence,
    InternalJobScope,
    Scope,
)
from modules.knowledge.documents._scope import _admit_document_scope, _require_document_owner
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentCleanupEvidenceReference,
    DocumentCleanupOperation,
    DocumentVersion,
)
from modules.knowledge.documents.schemas import (
    DocumentCleanupJobIdentity,
)
from modules.sources import public as sources


@dataclass(frozen=True)
class DocumentCleanupEvidenceIdentity:
    """Carry one captured immutable version or chunk identity after hard deletion."""

    document_version_id: UUID
    chunk_id: UUID | None
    reference_kind: str


@dataclass(frozen=True)
class DocumentCleanupEvidenceScope:
    """Return a bounded detached page of one deleted document's evidence identities."""

    operation_id: UUID
    source_id: UUID
    document_id: UUID
    references: tuple[DocumentCleanupEvidenceIdentity, ...]
    next_cursor: UUID | None
    workspace_id: UUID
    actor_user_id: int


@dataclass(frozen=True)
class SourceCleanupProgress:
    """Expose bounded Documents-owned aggregate counts without receipt identities.

    ``child_count``/``pending_count``/``failed_count`` describe receipts linked to the asking
    Source operation. ``historical_*`` describe every other retained same-source receipt (NULL or
    older linkage). ``all_required_complete`` requires both populations to be fully complete.
    """

    child_count: int
    capture_complete: bool
    pending_count: int
    failed_count: int
    all_required_complete: bool
    pending_owner_codes: tuple[str, ...]
    historical_count: int = 0
    historical_pending_count: int = 0
    historical_failed_count: int = 0
    active_copy_work: bool = False


async def pending_document_memory_cleanup_ids(
    session: AsyncSession,
    *,
    after: UUID | None = None,
    limit: int = 100,
) -> tuple[UUID, ...]:
    """Return a bounded keyset page of captured receipts ready for their Memory stage.

    The worker uses stable receipt-derived event IDs, so repeated reconciliation is idempotent.
    Chat must be terminal-success before Memory is admitted; unavailable scopes are not retried.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Memory cleanup reconciliation page size must be between 1 and 100")
    statement = select(DocumentCleanupOperation.id).where(
        DocumentCleanupOperation.evidence_scope_status == "captured",
        DocumentCleanupOperation.chat_status == "succeeded",
        DocumentCleanupOperation.memory_status == "queued",
        DocumentCleanupOperation.membership_revision.is_not(None),
    )
    if after is not None:
        statement = statement.where(DocumentCleanupOperation.id > after)
    return tuple(
        (await session.scalars(statement.order_by(DocumentCleanupOperation.id).limit(limit))).all()
    )


async def pending_document_agent_cleanup_ids(
    session: AsyncSession,
    *,
    after: UUID | None = None,
    limit: int = 100,
) -> tuple[UUID, ...]:
    """Return cleanup receipts whose Agent owner stage needs event reconciliation."""
    if not 1 <= limit <= 100:
        raise ValueError("Agent cleanup reconciliation page size must be between 1 and 100")
    statement = select(DocumentCleanupOperation.id).where(
        DocumentCleanupOperation.agent_status.in_({"queued", "running"}),
        DocumentCleanupOperation.membership_revision.is_not(None),
    )
    if after is not None:
        statement = statement.where(DocumentCleanupOperation.id > after)
    return tuple(
        (await session.scalars(statement.order_by(DocumentCleanupOperation.id).limit(limit))).all()
    )


async def pending_document_copied_stage_cleanup_ids(
    session: AsyncSession,
    *,
    after: UUID | None = None,
    limit: int = 100,
) -> tuple[UUID, ...]:
    """Return receipts whose materialization or saved-brief stage still needs event reconciliation.

    Terminal failures (unavailable lineage) are excluded so legacy gaps never hot-loop; retryable
    failures are rescheduled by their own durable event, not by this partial-index scan.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Copied-stage reconciliation page size must be between 1 and 100")
    statement = select(DocumentCleanupOperation.id).where(
        or_(
            DocumentCleanupOperation.materialization_status.in_({"queued", "running"}),
            DocumentCleanupOperation.brief_status.in_({"queued", "running"}),
        ),
        DocumentCleanupOperation.membership_revision.is_not(None),
    )
    if after is not None:
        statement = statement.where(DocumentCleanupOperation.id > after)
    return tuple(
        (await session.scalars(statement.order_by(DocumentCleanupOperation.id).limit(limit))).all()
    )


async def raw_uris(
    session: AsyncSession,
    *,
    instance_operator: bool,
    multi_workspace_enabled: bool,
) -> set[str]:
    """Return every nonempty raw-storage URI for the single-workspace orphan sweep.

    Only the bootstrap instance operator (flag off, active bootstrap account) may ask; any other
    caller raises 403 and never receives an empty set that could be read as "nothing referenced".
    """
    from core.auth.public import get_active_account

    if (
        instance_operator is not True
        or multi_workspace_enabled is not False
        or await get_active_account(session, 1, multi_workspace_enabled=False) is None
    ):
        raise ForbiddenError("Instance operator required")
    return {
        uri
        for uri in (
            await session.scalars(select(Document.raw_uri).where(Document.raw_uri.is_not(None)))
        ).all()
        if uri
    }


async def raw_uri_is_referenced_for_cleanup(
    session: AsyncSession,
    operation_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Return whether any surviving Document (any workspace) still owns this receipt's raw URI.

    The receipt is proven first (workspace/actor/membership/Source generation and configuration
    equal to the held original fence) and its URI is read here, never taken from the caller. The
    reference check is deliberately global because raw files are shared storage; only the
    boolean leaves this function. Any proof failure raises, so cleanup never unlinks on doubt.
    """
    _require_document_owner(scope)
    if (
        await workspaces.read_access_fence(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        != access_fence
    ):
        raise ConflictError("Cleanup access fence is stale")
    row = (
        await session.execute(
            select(
                DocumentCleanupOperation.raw_uri,
                DocumentCleanupOperation.configuration_revision,
            ).where(
                DocumentCleanupOperation.id == operation_id,
                DocumentCleanupOperation.workspace_id == scope.workspace_id,
                DocumentCleanupOperation.actor_user_id == actor_id(scope),
                DocumentCleanupOperation.membership_revision == scope.membership_revision,
            )
        )
    ).one_or_none()
    if row is None or row.configuration_revision != access_fence.configuration_revision:
        raise ConflictError("Cleanup receipt authority is unavailable")
    if row.raw_uri is None:
        return False
    return bool(
        await session.scalar(select(Document.id).where(Document.raw_uri == row.raw_uri).limit(1))
    )


_CLEANUP_IDENTITY_COLUMNS = (
    DocumentCleanupOperation.id,
    DocumentCleanupOperation.workspace_id,
    DocumentCleanupOperation.actor_user_id,
    DocumentCleanupOperation.membership_revision,
    DocumentCleanupOperation.configuration_revision,
    DocumentCleanupOperation.source_id,
    DocumentCleanupOperation.source_generation,
    DocumentCleanupOperation.document_id,
)


def _cleanup_identity(row: Any) -> DocumentCleanupJobIdentity | None:
    """Build the frozen tuple only when every captured epoch is present and valid (else legacy/None)."""
    if row is None or None in (
        row.membership_revision,
        row.configuration_revision,
        row.source_generation,
    ):
        return None
    try:
        return DocumentCleanupJobIdentity(
            operation_id=row.id,
            workspace_id=row.workspace_id,
            actor_user_id=row.actor_user_id,
            membership_revision=row.membership_revision,
            configuration_revision=row.configuration_revision,
            source_id=row.source_id,
            source_generation=row.source_generation,
            document_id=row.document_id,
        )
    except ValueError:
        return None


async def read_document_cleanup_job_identity(
    session: AsyncSession,
    operation_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DocumentCleanupJobIdentity | None:
    """Read one receipt's exact captured authority tuple without locks.

    NULL (legacy) epochs, a different actor/membership, a stale configuration or (for a
    Source-bound job scope) a different Source/generation return None; never rebased.
    """
    _require_document_owner(scope)
    fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    row = (
        await session.execute(
            select(*_CLEANUP_IDENTITY_COLUMNS).where(
                DocumentCleanupOperation.id == operation_id,
                DocumentCleanupOperation.workspace_id == scope.workspace_id,
                DocumentCleanupOperation.actor_user_id == actor_id(scope),
                DocumentCleanupOperation.membership_revision == scope.membership_revision,
            )
        )
    ).one_or_none()
    identity = _cleanup_identity(row)
    if identity is None or identity.configuration_revision != fence.configuration_revision:
        return None
    if (
        isinstance(scope, InternalJobScope)
        and scope.source_id is not None
        and (
            identity.source_id != scope.source_id
            or identity.source_generation != scope.source_generation
        )
    ):
        return None
    return identity


async def resolve_document_cleanup_job_identity(
    session: AsyncSession,
    operation_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> DocumentCleanupJobIdentity | None:
    """Admit one receipt's retained actor/membership/configuration/source tuple (worker entry).

    Identity-only discovery; NULL (legacy, ``cleanup_authority_unavailable``) epochs return None
    before any authorization. An admitted fence that differs from the captured tuple is stale
    (``cleanup_authority_stale``): None, never rebased. Actual permission loss propagates.
    """
    row = (
        await session.execute(
            select(*_CLEANUP_IDENTITY_COLUMNS).where(
                DocumentCleanupOperation.id == operation_id,
            )
        )
    ).one_or_none()
    captured = _cleanup_identity(row)
    if captured is None:
        return None
    try:
        scope = InternalJobScope(
            workspace_id=captured.workspace_id,
            actor_user_id=captured.actor_user_id,
            membership_revision=captured.membership_revision,
            source_id=captured.source_id,
            source_generation=captured.source_generation,
        )
        expected = AccessFence(
            workspace_id=captured.workspace_id,
            user_id=captured.actor_user_id,
            membership_revision=captured.membership_revision,
            configuration_revision=captured.configuration_revision,
        )
    except ValueError:
        return None
    fence = await workspaces.authorize_internal_job(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if fence != expected:
        return None
    reread = await read_document_cleanup_job_identity(
        session,
        operation_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return reread if reread == captured else None


async def get_document_cleanup_operation(
    session: AsyncSession,
    operation_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DocumentCleanupOperation | None:
    """Read an owner's Documents cleanup receipt (workspace + actor scoped) without its raw URI exposure."""
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return await session.scalar(
        select(DocumentCleanupOperation).where(
            DocumentCleanupOperation.id == operation_id,
            DocumentCleanupOperation.workspace_id == scope.workspace_id,
            DocumentCleanupOperation.actor_user_id == actor_id(scope),
        )
    )


async def cleanup_authority_error(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str | None:
    """Project action-required status for an unfinished receipt: unavailable (legacy) or stale epoch."""
    if operation.status == "succeeded":
        return None
    if operation.membership_revision is None or operation.configuration_revision is None:
        return "cleanup_authority_unavailable"
    fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (operation.membership_revision, operation.configuration_revision) != (
        fence.membership_revision,
        fence.configuration_revision,
    ):
        return "cleanup_authority_stale"
    return None


async def capture_document_cleanup_evidence(
    session: AsyncSession, operation: DocumentCleanupOperation
) -> None:
    """Snapshot exact version-only and chunk identities into the operation before its FK cascade.

    Both inserts are owner-local SQL ``INSERT … SELECT`` statements, so document history size
    does not create an unbounded Python snapshot. They write the receipt's NOT NULL workspace
    and are qualified through the workspace Document. The child identities intentionally have no
    foreign keys back to evidence rows and remain readable until the cleanup receipt is removed.
    Row ids come from the in-database ``uuid_generate_v7()`` (one id per selected row; a bound Python
    ``uuid7()`` would repeat across rows).
    """
    # DB-recorded bound for legacy brief coverage; read before the canonical rows are deleted.
    operation.earliest_version_created_at = await session.scalar(
        select(func.min(DocumentVersion.created_at))
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentVersion.document_id == operation.document_id,
            Document.workspace_id == operation.workspace_id,
        )
    )
    identity_columns = [
        "id",
        "workspace_id",
        "operation_id",
        "document_version_id",
        "chunk_id",
        "reference_kind",
    ]
    await session.execute(
        insert(DocumentCleanupEvidenceReference).from_select(
            identity_columns,
            select(
                func.uuid_generate_v7(),
                literal(operation.workspace_id),
                literal(operation.id),
                DocumentVersion.id,
                literal(None),
                literal("version"),
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentVersion.document_id == operation.document_id,
                Document.workspace_id == operation.workspace_id,
            ),
        )
    )
    await session.execute(
        insert(DocumentCleanupEvidenceReference).from_select(
            identity_columns,
            select(
                func.uuid_generate_v7(),
                literal(operation.workspace_id),
                literal(operation.id),
                DocumentChunk.document_version_id,
                DocumentChunk.id,
                literal("chunk"),
            )
            .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentVersion.document_id == operation.document_id,
                Document.workspace_id == operation.workspace_id,
            ),
        )
    )


async def list_document_cleanup_evidence_scope(
    session: AsyncSession,
    operation_id: UUID,
    *,
    after: UUID | None = None,
    limit: int = 100,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DocumentCleanupEvidenceScope | None:
    """Return one deterministic bounded identity page for the durable cleanup cursors.

    Owner admission (non-locking fence read; members denied before SQL) precedes every query, and
    the operation and reference pages are bound to the caller's workspace and actor.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Document cleanup evidence page size must be between 1 and 100")
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    operation = await session.scalar(
        select(DocumentCleanupOperation).where(
            DocumentCleanupOperation.id == operation_id,
            DocumentCleanupOperation.workspace_id == scope.workspace_id,
            DocumentCleanupOperation.actor_user_id == actor_id(scope),
        )
    )
    if operation is None:
        return None
    statement = select(DocumentCleanupEvidenceReference).where(
        DocumentCleanupEvidenceReference.operation_id == operation_id,
        DocumentCleanupEvidenceReference.workspace_id == scope.workspace_id,
    )
    if after is not None:
        statement = statement.where(DocumentCleanupEvidenceReference.id > after)
    rows = list(
        (
            await session.scalars(
                statement.order_by(DocumentCleanupEvidenceReference.id).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return DocumentCleanupEvidenceScope(
        operation_id=operation.id,
        source_id=operation.source_id,
        document_id=operation.document_id,
        references=tuple(
            DocumentCleanupEvidenceIdentity(
                document_version_id=row.document_version_id,
                chunk_id=row.chunk_id,
                reference_kind=row.reference_kind,
            )
            for row in rows
        ),
        next_cursor=rows[-1].id if has_more and rows else None,
        workspace_id=operation.workspace_id,
        actor_user_id=operation.actor_user_id,
    )


@dataclass(frozen=True)
class _StagePredicates:
    """SQL boolean expressions classifying a cleanup receipt as failed/pending per stage."""

    failed: ColumnElement[bool]
    pending: ColumnElement[bool]
    linked: ColumnElement[bool]
    unlinked: ColumnElement[bool]
    raw_pending: ColumnElement[bool]
    chat_pending: ColumnElement[bool]
    agent_pending: ColumnElement[bool]
    materialization_pending: ColumnElement[bool]
    brief_pending: ColumnElement[bool]
    memory_waiting: ColumnElement[bool]


def _stage_predicates(fence: AccessFence, source_purge_operation_id: UUID) -> _StagePredicates:
    """Build the per-stage failed/pending predicates for the Source cleanup aggregate.

    Pure expression construction (no session access, no SQL executed). ``fence`` supplies the
    current authority epochs: a receipt with missing epochs, or with stale epochs that has not
    succeeded, counts as failed (action required) so it can never leave a Source purge pending
    forever. ``linked``/``unlinked`` split receipts by ``source_purge_operation_id`` and treat a
    NULL linkage as historical.
    """
    authority_lost = or_(
        DocumentCleanupOperation.membership_revision.is_(None),
        DocumentCleanupOperation.configuration_revision.is_(None),
        and_(
            DocumentCleanupOperation.status != "succeeded",
            or_(
                DocumentCleanupOperation.actor_user_id != fence.user_id,
                DocumentCleanupOperation.membership_revision != fence.membership_revision,
                DocumentCleanupOperation.configuration_revision != fence.configuration_revision,
            ),
        ),
    )
    failed = or_(
        authority_lost,
        DocumentCleanupOperation.evidence_scope_status == "unavailable",
        DocumentCleanupOperation.raw_status == "failed",
        DocumentCleanupOperation.chat_status == "failed",
        DocumentCleanupOperation.memory_status == "failed",
        DocumentCleanupOperation.agent_status == "failed",
        DocumentCleanupOperation.materialization_status == "failed",
        DocumentCleanupOperation.brief_status == "failed",
        DocumentCleanupOperation.copied_status == "failed",
    )
    scope_pending = DocumentCleanupOperation.evidence_scope_status == "capturing"
    raw_pending = DocumentCleanupOperation.raw_status.not_in(
        ("not_present", "retained_shared", "succeeded", "failed")
    )
    chat_pending = DocumentCleanupOperation.chat_status.not_in(("succeeded", "failed"))
    memory_pending = or_(
        DocumentCleanupOperation.memory_status != "succeeded",
        DocumentCleanupOperation.memory_cache_pending.is_(True),
    )
    agent_pending = DocumentCleanupOperation.agent_status.not_in(("succeeded", "failed"))
    materialization_pending = DocumentCleanupOperation.materialization_status.not_in(
        ("succeeded", "failed")
    )
    brief_pending = DocumentCleanupOperation.brief_status.not_in(("succeeded", "failed"))
    copy_pending = DocumentCleanupOperation.copied_status != "succeeded"
    # Cache eviction is a separate durable Memory obligation, including while a prior
    # content-cleanup stage is terminally failed and awaits its retryable postcommit work.
    pending = or_(
        DocumentCleanupOperation.memory_cache_pending.is_(True),
        and_(
            ~failed,
            or_(
                scope_pending,
                raw_pending,
                chat_pending,
                memory_pending,
                agent_pending,
                materialization_pending,
                brief_pending,
                copy_pending,
            ),
        ),
    )
    linked = DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id
    # NULL linkage must count as historical: ``NOT (NULL = x)`` is NULL, so test it explicitly.
    unlinked = or_(
        DocumentCleanupOperation.source_purge_operation_id.is_(None),
        DocumentCleanupOperation.source_purge_operation_id != source_purge_operation_id,
    )
    memory_waiting_expr = or_(
        and_(~failed, memory_pending),
        DocumentCleanupOperation.memory_cache_pending.is_(True),
    )
    return _StagePredicates(
        failed=failed,
        pending=pending,
        linked=linked,
        unlinked=unlinked,
        raw_pending=raw_pending,
        chat_pending=chat_pending,
        agent_pending=agent_pending,
        materialization_pending=materialization_pending,
        brief_pending=brief_pending,
        memory_waiting=memory_waiting_expr,
    )


def _pending_owner_codes(
    *,
    capture_recorded: bool,
    raw_waiting: int,
    chat_waiting: int,
    memory_waiting: int,
    agent_waiting: int,
    materialization_waiting: int,
    brief_waiting: int,
) -> list[str]:
    """Translate per-stage waiting counts into the bounded owner codes shown to Sources.

    Order is part of the contract (documents, raw, chat, memory, agents, notifications and
    automations, dashboard) because callers surface the list verbatim.
    """
    owners: list[str] = []
    if not capture_recorded:
        owners.append("documents")
    if raw_waiting:
        owners.append("raw")
    if chat_waiting:
        owners.append("chat")
    if memory_waiting:
        owners.append("memory")
    if agent_waiting:
        owners.append("agents")
    if materialization_waiting:
        owners.extend(("notifications", "automations"))
    if brief_waiting:
        owners.append("dashboard")
    return owners


async def source_cleanup_progress(
    session: AsyncSession,
    source_purge_operation_id: UUID,
    *,
    source_id: UUID,
    capture_recorded: bool,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> SourceCleanupProgress:
    """Aggregate every retained same-source receipt in one SQL statement; return counts only.

    The Source owner verified that ``source_id`` owns the operation and supplies its durable
    capture receipt, because only that owner can tell a truly empty Source from a legacy
    operation whose rows vanished. The query is an exact ``source_id`` index aggregate (one output
    row, fixed columns); each receipt is counted once whether it is linked to this operation,
    linked to an older one, or unlinked, and no receipt is mutated, reparented or locked. Row
    work grows with retained receipt history for the Source (capacity unverified). Every stage is
    read afresh: a stage that is pending or failed is never cached as complete. A receipt whose
    captured evidence identities are unavailable is failed; one still ``capturing`` is pending.
    Memory cache eviction is an independent pending obligation until its postcommit retry clears it.
    The aggregate is workspace-qualified. A receipt whose authority epochs are missing (legacy) is
    failed, and one whose captured membership/configuration epoch is stale and not yet finished is
    failed too (action required): neither can ever be resumed, so neither may leave the Source
    purge pending forever or complete.
    """
    fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    preds = _stage_predicates(fence, source_purge_operation_id)
    failed, pending, linked, unlinked = preds.failed, preds.pending, preds.linked, preds.unlinked
    raw_pending, chat_pending = preds.raw_pending, preds.chat_pending
    agent_pending, materialization_pending = preds.agent_pending, preds.materialization_pending
    brief_pending, memory_waiting_expr = preds.brief_pending, preds.memory_waiting
    receipt_id = DocumentCleanupOperation.id
    row = (
        await session.execute(
            select(
                func.count(receipt_id).filter(linked),
                func.count(receipt_id).filter(unlinked),
                func.count(receipt_id).filter(linked, pending),
                func.count(receipt_id).filter(linked, failed),
                func.count(receipt_id).filter(unlinked, pending),
                func.count(receipt_id).filter(unlinked, failed),
                func.count(receipt_id).filter(and_(~failed, raw_pending)),
                func.count(receipt_id).filter(and_(~failed, chat_pending)),
                func.count(receipt_id).filter(memory_waiting_expr),
                func.count(receipt_id).filter(and_(~failed, agent_pending)),
                func.count(receipt_id).filter(and_(~failed, materialization_pending)),
                func.count(receipt_id).filter(and_(~failed, brief_pending)),
                func.count(receipt_id).filter(linked, ~failed, or_(raw_pending, chat_pending)),
            ).where(
                DocumentCleanupOperation.workspace_id == scope.workspace_id,
                DocumentCleanupOperation.source_id == source_id,
            )
        )
    ).one()
    (
        child_count,
        historical_count,
        pending_count,
        failed_count,
        historical_pending,
        historical_failed,
        raw_waiting,
        chat_waiting,
        memory_waiting,
        agent_waiting,
        materialization_waiting,
        brief_waiting,
        linked_active,
    ) = (int(value or 0) for value in row)
    owners = _pending_owner_codes(
        capture_recorded=capture_recorded,
        raw_waiting=raw_waiting,
        chat_waiting=chat_waiting,
        memory_waiting=memory_waiting,
        agent_waiting=agent_waiting,
        materialization_waiting=materialization_waiting,
        brief_waiting=brief_waiting,
    )
    # Complete only when every required stage of every retained receipt succeeded; a terminal
    # unavailable stage is failed (counted above), never relabeled complete.
    all_required_complete = (
        capture_recorded
        and pending_count + historical_pending == 0
        and failed_count + historical_failed == 0
    )
    return SourceCleanupProgress(
        child_count=child_count,
        capture_complete=capture_recorded,
        pending_count=pending_count,
        failed_count=failed_count,
        all_required_complete=all_required_complete,
        pending_owner_codes=tuple(owners),
        historical_count=historical_count,
        historical_pending_count=historical_pending,
        historical_failed_count=historical_failed,
        active_copy_work=bool(linked_active),
    )


@dataclass(frozen=True)
class SourceCleanupWakeup:
    """Detached, content-free hint that one cleanup receipt made durable progress."""

    child_operation_id: UUID
    workspace_id: UUID
    source_id: UUID
    linked_operation_id: UUID | None
    progress_key: str


def source_cleanup_wakeup_hint(
    operation: DocumentCleanupOperation, *, progress_key: str
) -> SourceCleanupWakeup:
    """Snapshot the scalars a wakeup needs while the child receipt is still held (no ORM after commit)."""
    if not re.fullmatch(r"[a-z0-9:_;=-]{1,128}", progress_key):
        raise ValueError("Source cleanup progress key must be a bounded lowercase status token")
    return SourceCleanupWakeup(
        child_operation_id=operation.id,
        workspace_id=operation.workspace_id,
        source_id=operation.source_id,
        linked_operation_id=operation.source_purge_operation_id,
        progress_key=progress_key,
    )


async def publish_source_cleanup_wakeup(
    session: AsyncSession,
    hint: SourceCleanupWakeup,
    observer_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> bool:
    """Publish one idempotent Source aggregate wakeup for one observer; True only if newly published.

    Call in a fresh session holding no locks, after the child stage committed. The observer's own
    retained authority is admitted (never the child's), it must still be an open observer of the
    hinted Source, and the stable event ID is derived per observer so an older observer cannot
    consume a newer wakeup. A same-ID row of any other principal is never adopted. The persisted
    Source coverage reconciler remains the durable fallback for any failure.
    """
    from modules.ingestion import public as ingestion

    scope = await sources.resolve_source_purge_job_scope(
        session,
        observer_id,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        scope is None
        or scope.workspace_id != hint.workspace_id
        or scope.source_id != hint.source_id
    ):
        return False
    predecessor = UUID(int=observer_id.int - 1) if observer_id.int else None
    if observer_id not in await sources.list_source_purge_observer_ids(
        session,
        hint.source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        after=predecessor,
        limit=1,
    ):
        return False
    key = hint.progress_key
    event_id = (
        uuid5(hint.child_operation_id, f"source-purge-progress:{key}")
        if observer_id == hint.linked_operation_id
        else uuid5(hint.child_operation_id, f"source-purge-progress:{observer_id}:{key}")
    )
    if (
        await ingestion.get_event_delivery(
            session,
            event_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        is not None
    ):
        return False
    # proven by the resolver
    if scope.source_id is None or scope.source_generation is None:
        raise RuntimeError(
            "invariant violated: scope.source_id and scope.source_generation must not be None"
        )
    try:
        await ingestion.publish_event(
            session,
            DomainEvent(
                id=event_id,
                type="source.purge.progressed",
                version=1,
                occurred_at=datetime.now(UTC),
                producer="modules.knowledge.documents",
                payload={
                    "operation_id": str(observer_id),
                    "workspace_id": str(scope.workspace_id),
                    "actor_user_id": scope.actor_user_id,
                    "membership_revision": scope.membership_revision,
                    "source_id": str(scope.source_id),
                    "source_generation": scope.source_generation,
                },
            ),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True
