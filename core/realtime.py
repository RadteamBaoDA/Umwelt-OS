from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypeAlias, cast
from uuid import UUID, uuid5

import anyio
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    and_,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext

MAX_REPLAY_EVENTS = 10_000
MAX_INSTANCE_REPLAY_EVENTS = 100_000
MAX_REPLAY_BATCH = 100
MAX_REPLAY_EVENT_BYTES = 16_384
MAX_REPLAY_SEQUENCE = 2**63 - 1
REPLAY_SQL_BUDGET_SECONDS = 3
REPLAY_CLEANUP_BUDGET_SECONDS = 2
_EPOCH_NAMESPACE = UUID("50954cbb-d568-4f35-8df4-fca2e2a38dd7")
MAX_CURSOR_LENGTH = 60
_CURSOR_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(0|[1-9][0-9]{0,18})$")


class _Payload(BaseModel):
    """Immutable typed draft; excluded principal metadata binds append without changing wire identity.

    Construction captures a subject, never authorization. Commit compares it with actual
    caller-held admission and the original fence before serializing the public payload.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    principal_workspace_id: UUID = Field(exclude=True)
    principal_user_id: int = Field(strict=True, ge=1, exclude=True)
    principal_membership_revision: int = Field(strict=True, ge=1, exclude=True)


class SourceChanged(_Payload):
    """Realtime payload describing a source status or connector-generation change."""
    type: Literal["source.changed"] = "source.changed"
    source_id: UUID
    generation: int = Field(ge=1)
    status: Literal["active", "paused", "archived"]
    connector_state: str | None = Field(default=None, max_length=48)
    operation_id: UUID | None = None


class IngestionChanged(_Payload):
    """Realtime payload describing ingestion run or stage progress."""
    type: Literal["ingestion.changed"] = "ingestion.changed"
    source_id: UUID
    run_id: UUID
    status: Literal["queued", "running", "retrying", "succeeded", "failed", "cancelled", "needs_ocr"]
    stage_key: str | None = Field(default=None, max_length=48)
    stage_status: Literal["pending", "queued", "running", "retrying", "succeeded", "failed", "cancelled"] | None = None


class KnowledgeChanged(_Payload):
    """Realtime payload for source, index, graph, or timeline changes with scoped identities."""
    type: Literal["knowledge.changed"] = "knowledge.changed"
    scope: Literal["source", "index", "graph", "timeline", "timeline_collection"] = "source"
    source_id: UUID | None = None
    document_id: UUID | None = None
    version: int | None = Field(default=None, ge=1)
    deleted: bool = False
    entity_id: UUID | None = None
    relationship_id: UUID | None = None
    event_id: UUID | None = None
    event_revision: int | None = Field(default=None, ge=1)
    index_generation_id: UUID | None = None
    index_status: Literal["queued", "running", "active", "failed", "retired"] | None = None
    indexed_items: int | None = Field(default=None, ge=0)
    failed_items: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_scope(self) -> KnowledgeChanged:
        """Enforce mutually exclusive identity fields for source, index, graph, event, and collection invalidations."""
        if self.scope == "source":
            if self.source_id is None or any((
                self.entity_id is not None,
                self.relationship_id is not None,
                self.index_generation_id is not None,
                self.index_status is not None,
                self.indexed_items is not None,
                self.failed_items is not None,
                self.event_id is not None,
                self.event_revision is not None,
            )):
                raise ValueError("Source knowledge events require a source identity only")
            return self
        if (
            self.scope == "index" and (
                self.source_id is not None or self.document_id is not None or self.version is not None
                or self.deleted or self.entity_id is not None or self.relationship_id is not None
                or self.index_generation_id is None or self.index_status is None
                or self.indexed_items is None or self.failed_items is None
                or self.event_id is not None or self.event_revision is not None
            )
        ):
            raise ValueError("Index knowledge events require only a generation identity and progress")
        if self.scope == "graph" and (
            (self.entity_id is None) == (self.relationship_id is None)
            or self.source_id is not None or self.document_id is not None or self.version is not None
            or self.index_generation_id is not None or self.index_status is not None
            or self.indexed_items is not None or self.failed_items is not None
            or self.event_id is not None or self.event_revision is not None
        ):
            raise ValueError("Graph knowledge events require exactly one entity or relationship identity")
        if self.scope == "timeline" and (
            self.event_id is None or self.event_revision is None
            or self.source_id is not None or self.document_id is not None or self.version is not None
            or self.entity_id is not None or self.relationship_id is not None
            or self.index_generation_id is not None or self.index_status is not None
            or self.indexed_items is not None or self.failed_items is not None
        ):
            raise ValueError("Timeline knowledge events require an event identity and revision only")
        if self.scope == "timeline_collection" and (
            (self.source_id is None) == (self.entity_id is None)
            or self.document_id is not None or self.version is not None or self.deleted
            or self.relationship_id is not None
            or self.event_id is not None or self.event_revision is not None
            or self.index_generation_id is not None or self.index_status is not None
            or self.indexed_items is not None or self.failed_items is not None
        ):
            raise ValueError("Timeline collection invalidations require exactly one source or entity identity")
        return self


class DashboardChanged(_Payload):
    """Identify a committed dashboard/definition revision without disclosing its configuration.

    ``scope`` selects the owning revision counter, ``id`` identifies that resource,
    and the strict boolean ``deleted`` marks its terminal revision. Revisions are strict positive safe
    integers so JavaScript clients can compare them without precision loss.
    """
    type: Literal["dashboard.changed"] = "dashboard.changed"
    scope: Literal["dashboard", "definition", "brief"]
    id: UUID
    revision: int = Field(strict=True, ge=1, le=2**53 - 1)
    deleted: bool = Field(strict=True)


ReplayDraft: TypeAlias = Annotated[  # noqa: UP040  # keep TypeVar/TypeAlias spelling; PEP 695 rewrite is style-only
    SourceChanged | IngestionChanged | KnowledgeChanged | DashboardChanged,
    Field(discriminator="type"),
]


class ReplayHead(Base):
    """Per-principal durable replay-stream epoch, sequence, retention floor, and update timestamp.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "realtime_replay_head"
    __table_args__ = (
        CheckConstraint("sequence >= 0", name="ck_realtime_replay_head_sequence_nonnegative"),
        CheckConstraint("floor_sequence >= 1", name="ck_realtime_replay_head_floor_positive"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_realtime_replay_head_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "user_id"], ['workspace_memberships.workspace_id', 'workspace_memberships.user_id'], name="fk_w2_realtime_replay_head_principal", ondelete="RESTRICT"),
        Index("ix_w2_realtime_replay_head_work", "workspace_id", "user_id", "updated_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)


    epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    floor_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)



class ReplayRecord(Base):
    """Persisted ordered realtime event payload associated with one replay epoch and sequence.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """
    __tablename__ = "realtime_replay_events"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_realtime_replay_sequence_positive"),
        CheckConstraint("event_type IN ('source.changed', 'ingestion.changed', 'knowledge.changed', 'dashboard.changed')", name="ck_realtime_replay_event_type"),
        Index("ix_realtime_replay_events_epoch_sequence", "epoch", "sequence"),
        Index("ix_realtime_replay_events_created_at", "created_at"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_realtime_replay_events_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "user_id"], ['workspace_memberships.workspace_id', 'workspace_memberships.user_id'], name="fk_w2_realtime_replay_events_principal", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "user_id"], ["realtime_replay_head.workspace_id", "realtime_replay_head.user_id"], name="fk_w2_realtime_replay_events_head", ondelete="CASCADE"),
        Index("ix_w2_realtime_replay_events_work", "workspace_id", "user_id", "created_at", "sequence"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)


    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)



class ReplayCursor(BaseModel):
    """Immutable epoch/sequence position used to resume replay delivery."""
    model_config = ConfigDict(frozen=True)

    epoch: UUID
    sequence: int = Field(ge=0, le=MAX_REPLAY_SEQUENCE)

    def encode(self) -> str:
        """Return this replay cursor in its canonical epoch:sequence representation."""
        return f"{self.epoch}:{self.sequence}"


def parse_cursor(value: str) -> ReplayCursor:
    """Parse a bounded canonical replay cursor; reject malformed UUIDs, sequence values, and noncanonical text."""
    if len(value) > MAX_CURSOR_LENGTH:
        raise ValueError("Replay cursor is too long")
    match = _CURSOR_RE.fullmatch(value)
    if match is None:
        raise ValueError("Replay cursor is malformed")
    epoch_text, sequence_text = match.groups()
    epoch = UUID(epoch_text)
    if str(epoch) != epoch_text:
        raise ValueError("Replay cursor epoch is not canonical")
    sequence = int(sequence_text)
    if sequence > MAX_REPLAY_SEQUENCE:
        raise ValueError("Replay cursor sequence is too large")
    return ReplayCursor(epoch=epoch, sequence=sequence)


def _scope_principal(scope: Scope) -> dict[str, Any]:
    """Capture only a typed owner subject for private replay, never membership-based fanout.

    Internal job authority is established by its owner before append; member Document/Brief
    invalidations await W3's owner-authorized projection contract and cannot use private kinds.
    """
    if isinstance(scope, InternalJobScope):
        actor = scope.actor_user_id
    elif isinstance(scope, WorkspaceContext) and scope.role == "owner":
        actor = scope.user_id
    else:
        raise ValueError("Private replay requires a typed owner scope")
    return {"principal_workspace_id": scope.workspace_id, "principal_user_id": actor,
            "principal_membership_revision": scope.membership_revision}


def make_source_change(
    source_id: UUID,
    generation: int,
    status: str,
    connector_state: str | None = None,
    operation_id: UUID | None = None,
    *, scope: Scope,
) -> SourceChanged:
    """Capture an actual owner principal and validated source state; caller must authorize publication."""
    return SourceChanged(
        **_scope_principal(scope),
        source_id=source_id, generation=generation, status=status,
        connector_state=connector_state, operation_id=operation_id,
    )


def make_ingestion_change(
    source_id: UUID,
    run_id: UUID,
    status: str,
    stage_key: str | None = None,
    stage_status: str | None = None,
    *, scope: Scope,
) -> IngestionChanged:
    """Capture an actual owner principal and validated run/stage state without authorizing delivery."""
    return IngestionChanged(
        **_scope_principal(scope),
        source_id=source_id, run_id=run_id, status=status,
        stage_key=stage_key, stage_status=stage_status,
    )


def make_knowledge_change(
    source_id: UUID,
    document_id: UUID | None = None,
    version: int | None = None,
    *,
    scope: Scope,
    deleted: bool = False,
) -> KnowledgeChanged:
    """Construct owner-bound source knowledge state; preserve payload domain scope and document identity."""
    return KnowledgeChanged(
        **_scope_principal(scope),
        source_id=source_id, document_id=document_id, version=version,
        deleted=deleted,
    )


def make_index_change(
    generation_id: UUID,
    status: str,
    indexed_items: int,
    failed_items: int,
    *, scope: Scope,
) -> KnowledgeChanged:
    """Capture owner-bound index generation/counts; these private counts never reach members."""
    return KnowledgeChanged(
        **_scope_principal(scope),
        scope="index",
        index_generation_id=generation_id,
        index_status=status,
        indexed_items=indexed_items,
        failed_items=failed_items,
    )


def make_graph_change(
    *, scope: Scope, entity_id: UUID | None = None, relationship_id: UUID | None = None, deleted: bool = False
) -> KnowledgeChanged:
    """Capture owner-bound graph state for exactly one identity; grant no member graph visibility."""
    return KnowledgeChanged(
        **_scope_principal(scope),
        scope="graph", entity_id=entity_id, relationship_id=relationship_id, deleted=deleted
    )


def make_timeline_change(event_id: UUID, revision: int, *, scope: Scope, deleted: bool = False) -> KnowledgeChanged:
    """Capture an owner-bound timeline revision, preserving its separate payload discriminator."""
    return KnowledgeChanged(**_scope_principal(scope), scope="timeline", event_id=event_id, event_revision=revision, deleted=deleted)


def make_timeline_collection_change(
    *, scope: Scope, source_id: UUID | None = None, entity_id: UUID | None = None,
) -> KnowledgeChanged:
    """Capture owner-bound collection invalidation for exactly one source/entity; members cannot use it."""
    if (source_id is None) == (entity_id is None):
        raise ValueError("Timeline collection invalidations require exactly one source or entity identity")
    return KnowledgeChanged(**_scope_principal(scope), scope="timeline_collection", source_id=source_id, entity_id=entity_id)

def make_dashboard_change(category: str, resource_id: UUID, revision: int, *, scope: Scope, deleted: bool = False) -> DashboardChanged:
    """Create a validated identifier-only event for one committed dashboard revision.

    ``category`` selects the payload's domain scope; the required ``scope`` keyword
    captures its actual owner principal. ``resource_id`` and its positive safe
    ``revision`` identify the owner counter. ``deleted`` marks final deletion.
    Pydantic raises ``ValidationError`` for unsupported scopes or invalid values.
    """
    return DashboardChanged(**_scope_principal(scope), scope=category, id=resource_id, revision=revision, deleted=deleted)



@dataclass(frozen=True, slots=True)
class ReplayState:
    """Detached state of one admitted stream; a missing/stale head has a virtual empty state."""

    epoch: UUID
    sequence: int
    floor_sequence: int


class _ReplayStorageError(RuntimeError):
    """Internal replay-only consistency failure; never disclose identities or occupancy to callers."""


async def _validate_replay_fence(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Compare the supplied original owner fence with fresh nonlocking admission.

    For append the caller already holds ordered auth/session/workspace/domain locks in
    this transaction. This read neither proves those locks nor reacquires them late.
    Members are denied even with empty drafts; source/resource/job authority remains owned
    by the caller. Current visible stale revisions are409, invisible admission stays401/404.
    """
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise ValueError("A typed replay scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool or not isinstance(access_fence, AccessFence):
        raise ValueError("Actual configured flag and original access fence are required")
    actor = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
    if (access_fence.workspace_id != scope.workspace_id or access_fence.user_id != actor
            or access_fence.membership_revision != scope.membership_revision):
        raise HTTPException(status_code=409, detail="Workspace access fence changed")
    actual = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if actual != access_fence:
        raise HTTPException(status_code=409, detail="Workspace access fence changed")


def _replay_epoch(fence: AccessFence) -> UUID:
    """Fingerprint matched principal and monotonic revisions; this public UUID is never authority."""
    return uuid5(_EPOCH_NAMESPACE, f"{fence.workspace_id}:{fence.user_id}:"
                 f"{fence.membership_revision}:{fence.configuration_revision}")


async def _bounded_replay_count(session: AsyncSession) -> int:
    """Count at most cap+1 replay metadata rows; corrupt over-cap state fails controlled repair."""
    bounded = select(1).select_from(ReplayRecord).limit(MAX_INSTANCE_REPLAY_EVENTS + 1).subquery()
    count = int(await session.scalar(select(func.count()).select_from(bounded)) or 0)
    if count > MAX_INSTANCE_REPLAY_EVENTS:
        raise _ReplayStorageError()
    return count


async def _retained_window(session: AsyncSession, head: ReplayHead) -> int:
    """Validate a head's exact epoch and contiguous retained prefix from scalar metadata only.

    Called under the final replay gate; no foreign payload or domain permission is read.
    Empty streams require floor=sequence+1. Holes, mixed epochs or oversized windows fail
    storage consistency rather than widening eviction or inventing resumable continuity.
    """
    row = (await session.execute(select(
        func.count(), func.min(ReplayRecord.sequence), func.max(ReplayRecord.sequence),
        func.count().filter(ReplayRecord.epoch != head.epoch),
    ).where(ReplayRecord.workspace_id == head.workspace_id, ReplayRecord.user_id == head.user_id))).one()
    count, first, last, wrong_epoch = row
    if (head.sequence < 0 or head.sequence > MAX_REPLAY_SEQUENCE or head.floor_sequence < 1
            or head.floor_sequence > head.sequence + 1 or count > MAX_REPLAY_EVENTS or wrong_epoch
            or count != head.sequence - head.floor_sequence + 1
            or (count and (first != head.floor_sequence or last != head.sequence))):
        raise _ReplayStorageError()
    return int(count)


async def _retain_and_append(
    session: AsyncSession, encoded: list[tuple[str, dict[str, Any]]], *, fence: AccessFence,
) -> None:
    """Expire replay-only prefixes fairly and append atomically under the final advisory gate.

    Approved core retention authority may inspect other streams' scalar metadata, delete
    their oldest replay copies and advance floors; it never reads payloads/domain/auth,
    changes victim epochs/sequences or acquires earlier locks. Plan at most100 extra row
    deletions and lock writer plus at most100 victims in canonical principal order. New
    batch rows are protected. Caller flushed domain writes and disables autoflush here.
    """
    await session.execute(select(func.set_config("statement_timeout", "3000", True),
                                 func.set_config("lock_timeout", "3000", True)))
    await session.execute(select(func.pg_advisory_xact_lock(1380994137, 1)))
    writer_key = (fence.workspace_id, fence.user_id)
    epoch = _replay_epoch(fence)
    planned_head = await session.scalar(select(ReplayHead).where(
        ReplayHead.workspace_id == writer_key[0], ReplayHead.user_id == writer_key[1],
    ).execution_options(populate_existing=True))
    total = await _bounded_replay_count(session)
    old_count = await _retained_window(session, planned_head) if planned_head is not None else 0
    reset = planned_head is None or planned_head.epoch != epoch
    persisted_sequence = 0 if planned_head is None else planned_head.sequence
    persisted_floor = 1 if planned_head is None else planned_head.floor_sequence
    old_sequence = 0 if reset else persisted_sequence
    final_sequence = old_sequence + len(encoded)
    # BIGINT floor must also fit after a fully evicted stream. Never rewind this epoch.
    if final_sequence >= MAX_REPLAY_SEQUENCE:
        raise _ReplayStorageError()
    floor = max(1 if reset else persisted_floor, final_sequence - MAX_REPLAY_EVENTS + 1)
    local_deleted = old_count if reset else floor - persisted_floor
    remaining = old_count - local_deleted
    excess = max(0, total - local_deleted + len(encoded) - MAX_INSTANCE_REPLAY_EVENTS)
    if excess > MAX_REPLAY_BATCH or remaining < 0:
        raise _ReplayStorageError()

    # The bounded top100 are sufficient for <=100 virtual removals. Writer's new
    # batch contributes to projected size but can never be selected for removal.
    allocations: dict[tuple[UUID, int], int] = {}
    if excess:
        size = func.count().label("retained")
        candidates = list((await session.execute(select(
            ReplayRecord.workspace_id, ReplayRecord.user_id, size,
        ).where(~and_(ReplayRecord.workspace_id == writer_key[0], ReplayRecord.user_id == writer_key[1]))
            .group_by(ReplayRecord.workspace_id, ReplayRecord.user_id)
            .order_by(size.desc(), ReplayRecord.workspace_id, ReplayRecord.user_id)
            .limit(MAX_REPLAY_BATCH))).all())
        projected = {(workspace_id, user_id): int(count) for workspace_id, user_id, count in candidates}
        eligible = dict(projected)
        projected[writer_key] = remaining + len(encoded)
        eligible[writer_key] = remaining
        for _ in range(excess):
            available = [key for key in projected if eligible[key] > allocations.get(key, 0)]
            if not available:
                raise _ReplayStorageError()
            chosen = min(available, key=lambda key: (-projected[key], key[0].int, key[1]))
            allocations[chosen] = allocations.get(chosen, 0) + 1
            projected[chosen] -= 1

    keys = sorted({writer_key, *allocations}, key=lambda key: (key[0].int, key[1]))
    heads: dict[tuple[UUID, int], ReplayHead] = {}
    now = datetime.now(UTC)
    for key in keys:
        head = await session.scalar(select(ReplayHead).where(
            ReplayHead.workspace_id == key[0], ReplayHead.user_id == key[1],
        ).with_for_update().execution_options(populate_existing=True))
        if head is None:
            if key != writer_key or planned_head is not None:
                raise _ReplayStorageError()
            head = ReplayHead(workspace_id=key[0], user_id=key[1], epoch=epoch,
                              sequence=0, floor_sequence=1, updated_at=now)
            session.add(head)
            # New writer creation occurs at its canonical position. Only admitted
            # writer parent keys are referenced; victim updates are non-key fields.
            await session.flush([head])
        elif key != writer_key:
            await _retained_window(session, head)
        heads[key] = head

    writer = heads[writer_key]
    if local_deleted:
        local_predicate = [ReplayRecord.workspace_id == writer_key[0], ReplayRecord.user_id == writer_key[1],
                           ReplayRecord.epoch == writer.epoch]
        if not reset:
            local_predicate.append(ReplayRecord.sequence < floor)
        deleted = cast(CursorResult[Any], await session.execute(
            delete(ReplayRecord).where(*local_predicate).execution_options(synchronize_session=False)))
        if deleted.rowcount != local_deleted:
            raise _ReplayStorageError()
    if reset:
        writer.epoch, writer.sequence, writer.floor_sequence = epoch, 0, 1
    writer.floor_sequence = floor

    for key in sorted(allocations, key=lambda item: (item[0].int, item[1])):
        head = heads[key]
        amount = allocations[key]
        prefix = list((await session.scalars(select(ReplayRecord.sequence).where(
            ReplayRecord.workspace_id == key[0], ReplayRecord.user_id == key[1],
            ReplayRecord.epoch == head.epoch, ReplayRecord.sequence >= head.floor_sequence,
        ).order_by(ReplayRecord.sequence).limit(amount))).all())
        if len(prefix) != amount or prefix != list(range(head.floor_sequence, head.floor_sequence + amount)):
            raise _ReplayStorageError()
        deleted = cast(CursorResult[Any], await session.execute(delete(ReplayRecord).where(
            ReplayRecord.workspace_id == key[0], ReplayRecord.user_id == key[1],
            ReplayRecord.epoch == head.epoch, ReplayRecord.sequence >= head.floor_sequence,
            ReplayRecord.sequence <= prefix[-1],
        ).execution_options(synchronize_session=False)))
        if deleted.rowcount != amount:
            raise _ReplayStorageError()
        head.floor_sequence = prefix[-1] + 1
        head.updated_at = now

    for index, (event_type, payload) in enumerate(encoded, start=1):
        session.add(ReplayRecord(workspace_id=writer_key[0], user_id=writer_key[1],
                                 sequence=old_sequence + index, epoch=epoch,
                                 event_type=event_type, payload=payload))
    writer.sequence, writer.updated_at = final_sequence, now
    await session.flush()
    # Recheck scalar cap after planned mutations; no occupancy escapes this module.
    await _bounded_replay_count(session)


async def _rollback_replay(session: AsyncSession) -> None:
    """Finish caller-unit rollback under cancellation shielding; invalidate on bounded cleanup failure."""
    with anyio.CancelScope(shield=True):
        try:
            with anyio.fail_after(REPLAY_CLEANUP_BUDGET_SECONDS):
                await session.rollback()
        except BaseException:
            with anyio.fail_after(REPLAY_CLEANUP_BUDGET_SECONDS):
                await session.invalidate()
            raise


async def commit_with_replay(
    session: AsyncSession, drafts: Sequence[ReplayDraft] = (), *, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Commit owner domain/outbox and bounded principal replay as one ordered unit.

    Caller actually holds original ordered admission/session/domain locks. Validate that
    original fence by fresh nonlocking comparison, including empty commits; never acquire
    earlier locks late. Flush domain writes before final replay gate, fair replay-only GC,
    append and commit. Invalid drafts raise ValueError; stale admission retains401/403/404/409;
    bounded storage failure/exhaustion is generic503. Every failure/cancellation rolls back.
    """
    try:
        await _validate_replay_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                                     access_fence=access_fence)
        if len(drafts) > MAX_REPLAY_BATCH:
            raise ValueError("Replay batch exceeds its event limit")
        encoded: list[tuple[str, dict[str, Any]]] = []
        principal = _scope_principal(scope)
        for draft in drafts:
            if not isinstance(draft, (SourceChanged, IngestionChanged, KnowledgeChanged, DashboardChanged)):
                raise ValueError("A typed replay draft is required")
            if any(getattr(draft, key) != value for key, value in principal.items()):
                raise ValueError("Replay draft principal does not match admission")
            # Round trip the exact Unicode serialization checked here. SSE uses these
            # same compact/ensure_ascii=False settings; excluded principal never leaks.
            serialized = json.dumps(draft.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
            if len(serialized.encode("utf-8")) > MAX_REPLAY_EVENT_BYTES:
                raise ValueError("Replay event exceeds its byte limit")
            encoded.append((draft.type, json.loads(serialized)))
        await session.flush()
        if not encoded:
            await session.commit()
            return
        with session.no_autoflush:
            async with asyncio.timeout(REPLAY_SQL_BUDGET_SECONDS):
                await _retain_and_append(session, encoded, fence=access_fence)
                await session.commit()
    except (TimeoutError, SQLAlchemyError, _ReplayStorageError) as exc:
        await _rollback_replay(session)
        raise HTTPException(status_code=503, detail="Realtime replay storage unavailable") from exc
    except BaseException:
        await _rollback_replay(session)
        raise


async def current_head(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> ReplayState:
    """Read only the admitted principal's detached head with its original revision-bound epoch.

    Fresh nonlocking fence equality is preparation, never held-lock proof. Missing/stale
    head returns virtual empty state; no insert/reset/lock/commit or old-epoch row exposure.
    Caller owns short transaction release and actual later publication admission.
    """
    await _validate_replay_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                                 access_fence=access_fence)
    epoch = _replay_epoch(access_fence)
    head = await session.scalar(
        select(ReplayHead).where(ReplayHead.workspace_id == access_fence.workspace_id,
                                 ReplayHead.user_id == access_fence.user_id)
        .execution_options(populate_existing=True)
    )
    if head is None or head.epoch != epoch:
        return ReplayState(epoch=epoch, sequence=0, floor_sequence=1)
    if head.sequence < 0 or head.sequence >= MAX_REPLAY_SEQUENCE or not 1 <= head.floor_sequence <= head.sequence + 1:
        raise HTTPException(status_code=503, detail="Realtime replay storage unavailable")
    return ReplayState(epoch=head.epoch, sequence=head.sequence, floor_sequence=head.floor_sequence)

