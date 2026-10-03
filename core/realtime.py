from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base

MAX_REPLAY_EVENTS = 10_000
MAX_REPLAY_BATCH = 100
MAX_REPLAY_EVENT_BYTES = 16_384
MAX_CURSOR_LENGTH = 60
_CURSOR_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(0|[1-9][0-9]{0,18})$")


class _Payload(BaseModel):
    """Strict immutable base for versioned realtime event payloads."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1


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
    def validate_scope(self) -> "KnowledgeChanged":
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


ReplayDraft: TypeAlias = Annotated[
    SourceChanged | IngestionChanged | KnowledgeChanged,
    Field(discriminator="type"),
]


class ReplayHead(Base):
    """Singleton durable replay-stream epoch, sequence, retention floor, and update timestamp."""
    __tablename__ = "realtime_replay_head"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    floor_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReplayRecord(Base):
    """Persisted ordered realtime event payload associated with one replay epoch and sequence."""
    __tablename__ = "realtime_replay_events"

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    epoch: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReplayCursor(BaseModel):
    """Immutable epoch/sequence position used to resume replay delivery."""
    model_config = ConfigDict(frozen=True)

    epoch: UUID
    sequence: int = Field(ge=0)

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
    return ReplayCursor(epoch=epoch, sequence=int(sequence_text))


def make_source_change(
    source_id: UUID,
    generation: int,
    status: str,
    connector_state: str | None = None,
    operation_id: UUID | None = None,
) -> SourceChanged:
    """Construct a validated source-changed event from source identity and current source state."""
    return SourceChanged(
        source_id=source_id, generation=generation, status=status,
        connector_state=connector_state, operation_id=operation_id,
    )


def make_ingestion_change(
    source_id: UUID,
    run_id: UUID,
    status: str,
    stage_key: str | None = None,
    stage_status: str | None = None,
) -> IngestionChanged:
    """Construct a validated ingestion-changed event from run and optional stage state."""
    return IngestionChanged(
        source_id=source_id, run_id=run_id, status=status,
        stage_key=stage_key, stage_status=stage_status,
    )


def make_knowledge_change(
    source_id: UUID,
    document_id: UUID | None = None,
    version: int | None = None,
    *,
    deleted: bool = False,
) -> KnowledgeChanged:
    """Construct a validated source-scope knowledge event, including optional document version and deletion state."""
    return KnowledgeChanged(
        source_id=source_id, document_id=document_id, version=version,
        deleted=deleted,
    )


def make_index_change(
    generation_id: UUID,
    status: str,
    indexed_items: int,
    failed_items: int,
) -> KnowledgeChanged:
    """Construct a validated index-scope knowledge event with generation progress counts."""
    return KnowledgeChanged(
        scope="index",
        index_generation_id=generation_id,
        index_status=status,
        indexed_items=indexed_items,
        failed_items=failed_items,
    )


def make_graph_change(
    *, entity_id: UUID | None = None, relationship_id: UUID | None = None, deleted: bool = False
) -> KnowledgeChanged:
    """Construct a validated graph-scope event for exactly one entity or relationship."""
    return KnowledgeChanged(
        scope="graph", entity_id=entity_id, relationship_id=relationship_id, deleted=deleted
    )


def make_timeline_change(event_id: UUID, revision: int, *, deleted: bool = False) -> KnowledgeChanged:
    """Construct a canonical timeline replay event without overloading graph or document identity."""
    return KnowledgeChanged(scope="timeline", event_id=event_id, event_revision=revision, deleted=deleted)


def make_timeline_collection_change(
    *, source_id: UUID | None = None, entity_id: UUID | None = None,
) -> KnowledgeChanged:
    """Construct a bounded collection invalidation scoped to exactly one source or entity."""
    if (source_id is None) == (entity_id is None):
        raise ValueError("Timeline collection invalidations require exactly one source or entity identity")
    return KnowledgeChanged(scope="timeline_collection", source_id=source_id, entity_id=entity_id)


async def commit_with_replay(session: AsyncSession, drafts: list[ReplayDraft] | tuple[ReplayDraft, ...] = ()) -> None:
    """Commit domain writes and replay rows atomically, taking the replay lock last."""
    try:
        if len(drafts) > MAX_REPLAY_BATCH:
            raise ValueError("Replay batch exceeds its event limit")
        encoded: list[tuple[str, dict[str, object]]] = []
        for draft in drafts:
            payload = draft.model_dump(mode="json")
            if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > MAX_REPLAY_EVENT_BYTES:
                raise ValueError("Replay event exceeds its byte limit")
            encoded.append((draft.type, payload))
        await session.flush()
        if not encoded:
            await session.commit()
            return
        head = await session.scalar(
            select(ReplayHead)
            .where(ReplayHead.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if head is None:
            raise RuntimeError("Realtime replay head has not been initialized")
        for event_type, payload in encoded:
            head.sequence += 1
            session.add(ReplayRecord(
                sequence=head.sequence,
                epoch=head.epoch,
                event_type=event_type,
                payload=payload,
            ))
        retained_floor = max(1, head.sequence - MAX_REPLAY_EVENTS + 1)
        if retained_floor > head.floor_sequence:
            await session.execute(delete(ReplayRecord).where(ReplayRecord.sequence < retained_floor))
            head.floor_sequence = retained_floor
        head.updated_at = datetime.now(UTC)
        await session.flush()
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def current_head(session: AsyncSession) -> ReplayHead:
    """Read the initialized replay head or raise when the migration/bootstrap row is missing."""
    head = await session.scalar(
        select(ReplayHead).where(ReplayHead.id == 1).execution_options(populate_existing=True)
    )
    if head is None:
        raise RuntimeError("Realtime replay head has not been initialized")
    return head
