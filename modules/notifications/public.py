"""Public notification service used by dashboard, tasks and later automation (P10)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from modules.notifications.models import Notification
from modules.notifications.schemas import (
    NotificationEmit,
    NotificationEvidence,
    NotificationPage,
    NotificationRead,
)

__all__ = [
    "NotificationCleanupProgress",
    "NotificationEmit",
    "NotificationEvidence",
    "NotificationMissing",
    "NotificationPage",
    "emit",
    "list_notifications",
    "scrub_document_evidence",
    "set_read",
]
MAX_CLEANUP_PAGE = 100


class NotificationMissing(Exception):
    """Raised when a notification is absent or owned by another owner."""


@dataclass(frozen=True)
class NotificationCleanupProgress:
    """Return bounded progress and stable owner-row classifications for one cleanup operation.

    Provisional unavailable IDs can be resolved by later reference pages; terminal IDs must be
    retained by the caller under ``operation_id``. No notification content is exposed.
    """

    next_cursor: UUID | None
    complete: bool
    changed_count: int
    operation_id: UUID
    provisional_unavailable_ids: tuple[UUID, ...] = ()
    unavailable_ids: tuple[UUID, ...] = ()


def _highlight_identity(kind: str, dedupe_key: str, params: Mapping[str, object]) -> tuple[UUID, int, UUID, UUID] | None:
    """Parse only the exact historical highlight dedupe grammar and its independent metadata copy."""
    if kind != "dashboard_highlight" or not isinstance(params, dict):
        return None
    parts = dedupe_key.split(":")
    if len(parts) != 6 or parts[0] != "highlight":
        return None
    try:
        definition_id, rule_id, version_id = (UUID(parts[index]) for index in (1, 4, 5))
        revision = int(parts[2])
    except (ValueError, TypeError):
        return None
    fingerprint = parts[3]
    if (
        str(definition_id) != parts[1] or str(rule_id) != parts[4] or str(version_id) != parts[5]
        or revision < 1 or str(revision) != parts[2]
        or len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint)
        or params.get("definition_id") != str(definition_id)
        or type(params.get("definition_revision")) is not int
        or params.get("definition_revision") != revision
        or not isinstance(params.get("severity"), str)
    ):
        return None
    return definition_id, revision, rule_id, version_id


async def _visible_read(session: AsyncSession, row: Notification) -> NotificationRead:
    """Build a detached DTO, holding a Source eligibility lock through copied-highlight publication.

    The first locator lookup discovers Source identity only. The authoritative locator is repeated
    after the Source lock, which serializes this read with source/document purge admission. Paused
    and connector-only archived Sources remain browseable; unfinished with-data purges fail closed.
    """
    read = NotificationRead.model_validate(row)
    if row.kind != "dashboard_highlight":
        return read
    identity = _highlight_identity(row.kind, row.dedupe_key, row.params)
    document_id, version_id = row.document_id, row.document_version_id
    if identity is not None:
        parsed_version = identity[3]
        if version_id is not None and version_id != parsed_version:
            identity = None
        else:
            version_id = parsed_version
    if (
        row.copied_evidence_revoked or identity is None or version_id is None
        or (document_id is not None and row.document_version_id != version_id)
    ):
        return read.model_copy(update={"title": None, "link": None})
    from modules.knowledge.documents import public as documents

    owner = await documents.review_version_locator(session, version_id)
    if owner is None or (document_id is not None and owner[0] != document_id):
        return read.model_copy(update={"title": None, "link": None})
    from modules.sources import public as sources

    fence = await sources.lock_retained_evidence_source(session, owner[1])
    if fence is None:
        return read.model_copy(update={"title": None, "link": None})
    version_fence = (await documents.review_version_fences(session, [version_id])).get(version_id)
    if (version_fence is None or version_fence.source_id != fence.id
            or version_fence.current_source_generation != fence.generation
            or (document_id is not None and version_fence.document_id != document_id)):
        return read.model_copy(update={"title": None, "link": None})
    return read


async def _lock_notification_read_sources(session: AsyncSession, rows: list[Notification]) -> None:
    """Acquire candidate Source read locks in stable UUID order before projecting a page.

    A notification page can refer to multiple sources. Prelocking the bounded source set avoids
    lock-order inversions with producers that already lock several selected sources in UUID order.
    `_visible_read` still rechecks each source and version after these locks are held.
    """
    version_ids: set[UUID] = set()
    for row in rows:
        if row.kind != "dashboard_highlight" or row.copied_evidence_revoked:
            continue
        identity = _highlight_identity(row.kind, row.dedupe_key, row.params)
        if identity is None:
            continue
        version_id = identity[3]
        if row.document_version_id is not None and row.document_version_id != version_id:
            continue
        version_ids.add(version_id)
    if not version_ids:
        return
    from modules.knowledge.documents import public as documents
    from modules.sources import public as sources

    fences = await documents.review_version_fences(session, sorted(version_ids, key=str))
    for source_id in sorted({fence.source_id for fence in fences.values()}, key=str):
        await sources.lock_retained_evidence_source(session, source_id)


async def emit(
    session: AsyncSession, owner_id: int, payload: NotificationEmit,
    *, evidence: NotificationEvidence | None = None,
) -> bool:
    """Insert an idempotent notification in the caller's transaction.

    Only dashboard highlights may attach exact copied-Document provenance. The Source row is locked
    before rechecking the exact ready current version and remains locked through insertion/commit;
    this serializes publication with Source purge and lifecycle changes. The sidecar stays private.
    """
    values = payload.model_dump()
    if evidence is not None:
        identity = _highlight_identity(payload.kind, payload.dedupe_key, payload.params)
        if payload.kind != "dashboard_highlight" or identity is None or identity[3] != evidence.document_version_id:
            raise ValueError("Copied notification evidence does not match a supported highlight identity")
        from modules.knowledge.documents import public as documents
        from modules.sources import public as sources

        locator = await documents.review_version_locator(session, evidence.document_version_id)
        if locator is None or locator[0] != evidence.document_id:
            return False
        fence = await sources.lock_retained_evidence_source(session, locator[1])
        current = await documents.get_ready_version_ref(session, evidence.document_version_id) if fence else None
        if (fence is None or current is None or current.document_id != evidence.document_id
                or current.source_id != fence.id or current.source_generation != fence.generation):
            return False
        values.update(
            document_id=evidence.document_id,
            document_version_id=evidence.document_version_id,
            copied_evidence_revoked=False,
        )
    result = await session.execute(
        insert(Notification)
        .values(owner_id=owner_id, **values)
        .on_conflict_do_nothing(constraint="uq_notifications_dedupe")
        .returning(Notification.id)
    )
    return result.scalar_one_or_none() is not None


async def list_notifications(
    session: AsyncSession, owner_id: int, *, unread_only: bool = False, limit: int = 50
) -> NotificationPage:
    """Return a bounded safe page with every unread row counted, even when copied display fields are withheld."""
    statement = select(Notification).where(Notification.owner_id == owner_id)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    rows = (await session.scalars(
        statement.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(min(max(limit, 1), 100))
    )).all()
    await _lock_notification_read_sources(session, list(rows))
    unread_count = int(await session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.owner_id == owner_id, Notification.read_at.is_(None),
        )
    ) or 0)
    return NotificationPage(
        items=[await _visible_read(session, row) for row in rows],
        unread_count=unread_count,
    )


async def set_read(session: AsyncSession, owner_id: int, notification_id: UUID, read: bool) -> NotificationRead:
    """Mark one owned notification read or unread and return its currently safe detached projection."""
    row = await session.scalar(
        select(Notification).where(Notification.id == notification_id, Notification.owner_id == owner_id).with_for_update()
    )
    if row is None:
        raise NotificationMissing
    row.read_at = datetime.now(UTC) if read else None
    await session.commit()
    return await _visible_read(session, row)


async def scrub_document_evidence(
    session: AsyncSession, *, operation_id: UUID, document_id: UUID, version_ids: tuple[UUID, ...],
    final_reference_page: bool = False, after: UUID | None = None, limit: int = MAX_CLEANUP_PAGE,
) -> NotificationCleanupProgress:
    """Flush-only scrub exact Document-backed highlight titles in a stable bounded notification page.

    New rows match their private document ID; legacy rows match only the proven historical dedupe
    grammar and the caller's detached immutable-version receipt. If evidence is paged, the caller's
    durable cursor retains the operation ID and both page positions. Unsupported candidate identities
    are provisional until the caller declares the final reference page, then remain explicitly unavailable.
    The caller owns commit and cursor.
    """
    if not 1 <= limit <= MAX_CLEANUP_PAGE or len(version_ids) > MAX_CLEANUP_PAGE:
        raise ValueError("Notification evidence cleanup exceeds its page bound")
    known_versions = set(version_ids)
    candidates = select(Notification).where(or_(
        Notification.document_id == document_id,
        and_(Notification.kind == "dashboard_highlight", Notification.dedupe_key.like("highlight:%")),
    ))
    if after is not None:
        candidates = candidates.where(Notification.id > after)
    rows = list((await session.scalars(
        candidates.order_by(Notification.id).limit(limit + 1).with_for_update()
    )).all())
    complete = len(rows) <= limit
    rows = rows[:limit]
    changed = 0
    provisional: list[UUID] = []
    unavailable: list[UUID] = []
    for row in rows:
        if row.kind != "dashboard_highlight":
            continue
        if row.copied_evidence_revoked:
            continue
        # A sidecar naming another Document is positive foreign ownership: never this cleanup's concern.
        if row.document_id is not None and row.document_id != document_id:
            continue
        exact_match = row.document_id == document_id
        version_id = row.document_version_id
        identity = _highlight_identity(row.kind, row.dedupe_key, row.params)
        # Even when the legacy parser rejects formatting, a canonical embedded version that conflicts
        # with the private sidecar is positive contradictory identity and must not be scrubbed.
        parts = row.dedupe_key.split(":")
        dedupe_version: UUID | None = None
        if len(parts) == 6 and parts[0] == "highlight":
            try:
                candidate = UUID(parts[5])
                dedupe_version = candidate if str(candidate) == parts[5] else None
            except (TypeError, ValueError):
                pass
        if exact_match and version_id is not None and dedupe_version is not None and dedupe_version != version_id:
            (unavailable if final_reference_page else provisional).append(row.id)
            continue
        if exact_match and version_id is not None and (identity is None or identity[3] == version_id):
            # The private exact Document/version sidecar is sufficient authority to scrub copied fields.
            version_id = row.document_version_id
        elif exact_match and (identity is not None and version_id is not None and identity[3] != version_id):
            (unavailable if final_reference_page else provisional).append(row.id)
            continue
        elif identity is None:
            if row.document_id is None or exact_match:
                (unavailable if final_reference_page else provisional).append(row.id)
            continue
        else:
            parsed_version = identity[3]
            if version_id is not None and version_id != parsed_version:
                (unavailable if final_reference_page else provisional).append(row.id)
                continue
            version_id = version_id or parsed_version
        assert version_id is not None  # every surviving branch above resolved a version
        if not exact_match and version_id not in known_versions:
            from modules.knowledge.documents import public as documents

            # Report only genuinely undecidable legacy rows; a version that resolves elsewhere is foreign.
            locator = await documents.review_version_locator(session, version_id)
            if locator is not None and locator[0] != document_id:
                continue
            # A Document already deleted (its own receipt not yet scrubbed) resolves from retained evidence.
            if locator is None:
                retained = await documents.cleanup_evidence_version_document(session, version_id)
                if retained is not None and retained != document_id:
                    continue
            (unavailable if final_reference_page else provisional).append(row.id)
            continue
        row.document_id = document_id
        row.document_version_id = version_id
        row.title = None
        row.link = None
        row.copied_evidence_revoked = True
        changed += 1
    if changed:
        await session.flush()
    return NotificationCleanupProgress(
        next_cursor=rows[-1].id if rows else after,
        complete=complete,
        changed_count=changed,
        operation_id=operation_id,
        provisional_unavailable_ids=tuple(provisional),
        unavailable_ids=tuple(unavailable),
    )
