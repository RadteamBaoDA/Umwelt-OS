"""Owner-scoped topic profiles, optimistic mutations, paging, and REST routes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ColumnElement,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    func,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import Base, get_session

MAX_REVISION = 9_007_199_254_740_991
MAX_KEYWORDS = 50
MAX_KEYWORD_LENGTH = 100
MAX_ENTITIES = 100


class Topic(Base):
    """Private persistence model for an owner's current and tombstoned topic."""

    __tablename__ = "news_topics"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(keywords) = 'array'", name="ck_news_topics_keywords_array"),
        CheckConstraint("jsonb_typeof(entity_ids) = 'array' AND jsonb_array_length(entity_ids) <= 100", name="ck_news_topics_entity_ids_array"),
        CheckConstraint("weight >= 0 AND weight <= 10", name="ck_news_topics_weight_range"),
        CheckConstraint(f"revision >= 1 AND revision <= {MAX_REVISION}", name="ck_news_topics_revision_range"),
        Index("ix_news_topics_owner_id", "owner_id"),
        Index("ix_news_topics_owner_deleted", "owner_id", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    entity_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


def _normalize_name(value: str) -> str:
    """Normalize a topic name and reject whitespace-only or oversized values."""
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 200:
        raise ValueError("Topic name must contain 1 to 200 normalized characters")
    return normalized


def _normalize_keywords(value: list[str]) -> list[str]:
    """Normalize distinct keywords within the shared count and character bounds."""
    normalized = list(dict.fromkeys(" ".join(item.split()) for item in value if item.strip()))
    if len(value) > MAX_KEYWORDS or len(normalized) > MAX_KEYWORDS or any(len(item) > MAX_KEYWORD_LENGTH for item in normalized):
        raise ValueError("Topics accept at most 50 keywords, each no longer than 100 characters")
    return normalized


def _normalize_entity_ids(value: list[UUID]) -> list[UUID]:
    """Require unique entity references within the public 100-reference ceiling."""
    if len(value) > MAX_ENTITIES or len(set(value)) != len(value):
        raise ValueError("Topics accept up to 100 unique entity IDs")
    return value


class TopicCreate(BaseModel):
    """Validate topic creation fields and document profile input bounds."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    entity_ids: list[UUID] = Field(default_factory=list, max_length=MAX_ENTITIES)
    is_active: bool = True
    weight: float = Field(default=1.0, ge=0.0, le=10.0, allow_inf_nan=False)

    @field_validator("name")
    @classmethod
    def normalized_name(cls, value: str) -> str:
        """Store one normalized nonblank topic name."""
        return _normalize_name(value)

    @field_validator("description")
    @classmethod
    def normalized_description(cls, value: str | None) -> str | None:
        """Normalize optional descriptions and collapse blank text to null."""
        normalized = value.strip() if value is not None else None
        return normalized or None

    @field_validator("keywords")
    @classmethod
    def normalized_keywords(cls, value: list[str]) -> list[str]:
        """Normalize the bounded set of tracked keywords."""
        return _normalize_keywords(value)

    @field_validator("entity_ids")
    @classmethod
    def unique_entity_ids(cls, value: list[UUID]) -> list[UUID]:
        """Reject duplicate or excessive linked entity identities."""
        return _normalize_entity_ids(value)


class TopicUpdate(BaseModel):
    """Validate partial topic changes with a mandatory safe revision fence."""
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, le=MAX_REVISION)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    keywords: list[str] | None = Field(default=None, max_length=MAX_KEYWORDS)
    entity_ids: list[UUID] | None = Field(default=None, max_length=MAX_ENTITIES)
    is_active: bool | None = None
    weight: float | None = Field(default=None, ge=0.0, le=10.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_change(self) -> TopicUpdate:
        """Reject revision-only patches so every accepted write changes a field."""
        if set(self.model_fields_set) <= {"expected_revision"}:
            raise ValueError("At least one topic field must be updated")
        return self

    @field_validator("name")
    @classmethod
    def normalized_name(cls, value: str | None) -> str | None:
        """Normalize provided names while preventing explicit null clears."""
        if value is None:
            raise ValueError("Topic name cannot be null")
        return _normalize_name(value)

    @field_validator("description")
    @classmethod
    def normalized_description(cls, value: str | None) -> str | None:
        """Normalize optional description text; null explicitly clears it."""
        return value.strip() or None if value is not None else None

    @field_validator("keywords")
    @classmethod
    def normalized_keywords(cls, value: list[str] | None) -> list[str] | None:
        """Normalize the keyword collection when the field is provided."""
        if value is None:
            raise ValueError("Keywords cannot be null; use an empty list to clear them")
        return _normalize_keywords(value)

    @field_validator("entity_ids")
    @classmethod
    def unique_entity_ids(cls, value: list[UUID] | None) -> list[UUID] | None:
        """Validate provided entity identities while preserving explicit empty clear."""
        if value is None:
            raise ValueError("Entity IDs cannot be null; use an empty list to clear them")
        return _normalize_entity_ids(value)

    @field_validator("is_active")
    @classmethod
    def active_required(cls, value: bool | None) -> bool:
        """Prevent nullable JSON values from reaching the nonnull persistence field."""
        if value is None:
            raise ValueError("is_active cannot be null")
        return value

    @field_validator("weight")
    @classmethod
    def importance_required(cls, value: float | None) -> float:
        """Prevent null importance writes while preserving omission semantics."""
        if value is None:
            raise ValueError("weight cannot be null")
        return value


class TopicRead(BaseModel):
    """Detached owner DTO; entity links are current canonical public references."""
    id: UUID
    owner_id: int
    name: str
    description: str | None
    keywords: list[str]
    entity_ids: list[UUID]
    is_active: bool
    weight: float
    revision: int
    created_at: datetime
    updated_at: datetime


class TopicPage(BaseModel):
    """Bounded owner topic page with a continuation cursor and filtered count."""
    items: list[TopicRead]
    next_cursor: str | None
    total: int


class TopicExportFence(BaseModel):
    """Bind a live topic export record to its stored revision and canonical DTO digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1, le=MAX_REVISION)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TopicExportPage(BaseModel):
    """Return one bounded page of live owner topics and fixed-cutoff inventory data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: int = Field(ge=1)
    record_kind: Literal["topics"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[TopicRead] = Field(max_length=100)
    fences: list[TopicExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: None = None


class TopicExportValidation(BaseModel):
    """Report whether exported topics and the owner inventory remain unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


class TopicFilter(BaseModel):
    """Bound topic collection reads to 100 rows and an optional active filter."""
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)
    is_active: bool | None = None


class TopicMissing(Exception):
    """Represent a topic that is missing, tombstoned, or owned by another user."""


class TopicConflict(Exception):
    """Represent a stale or exhausted topic revision."""
    def __init__(self, code: str, message: str, current_revision: int) -> None:
        """Keep stable conflict details for the protected HTTP boundary."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


def _to_topic_read(topic: Topic, entity_ids: list[UUID]) -> TopicRead:
    """Copy a live topic row and canonical entity IDs into a detached DTO."""
    return TopicRead(id=topic.id, owner_id=topic.owner_id, name=topic.name,
        description=topic.description, keywords=list(topic.keywords or []), entity_ids=entity_ids,
        is_active=topic.is_active, weight=topic.weight, revision=topic.revision,
        created_at=topic.created_at, updated_at=topic.updated_at)


async def _visible_entity_refs(session: AsyncSession, identifiers: list[UUID]) -> list[Any]:
    """Resolve entity refs, falling back to one lookup per ID so a deleted entity drops only itself."""
    from modules.knowledge.entities import public as entities
    try:
        return list(await entities.get_entity_refs(session, identifiers))
    except LookupError:
        visible = []
        for identifier in identifiers:
            try:
                visible.append((await entities.get_entity_refs(session, [identifier]))[0])
            except LookupError:
                continue
        return visible


async def _topic_read(session: AsyncSession, topic: Topic) -> TopicRead:
    """Build a detached projection using entity-owner read resolution only."""
    # Entity deletion must not make the topic owner record unreadable.
    refs = await _visible_entity_refs(session, [UUID(value) for value in (topic.entity_ids or [])])
    return _to_topic_read(topic, list(dict.fromkeys(ref.canonical_id for ref in refs)))


async def resolve_topic_terms(
    session: AsyncSession, owner_id: int, topic_ids: list[UUID],
) -> dict[UUID, list[str]]:
    """Read-only: return current keywords plus entity names for the owner's live, active topics.

    Missing, foreign, deleted or inactive topics are simply absent from the result so callers
    treat them as unresolved. Never writes and never contacts a provider.
    """
    if not topic_ids:
        return {}
    rows = (await session.scalars(select(Topic).where(
        Topic.id.in_(topic_ids), Topic.owner_id == owner_id,
        Topic.deleted_at.is_(None), Topic.is_active.is_(True),
    ))).all()
    resolved: dict[UUID, list[str]] = {}
    for topic in rows:
        terms = list(topic.keywords or [])
        entity_ids = [UUID(value) for value in (topic.entity_ids or [])]
        if entity_ids:
            terms += [ref.name for ref in await _visible_entity_refs(session, entity_ids) if ref.name]
        resolved[topic.id] = list(dict.fromkeys(terms))
    return resolved


async def live_topic_ids(session: AsyncSession, owner_id: int, topic_ids: list[UUID]) -> set[UUID]:
    """Return which of the given IDs are the owner's live (non-deleted) topics."""
    if not topic_ids:
        return set()
    return set((await session.scalars(select(Topic.id).where(
        Topic.id.in_(topic_ids), Topic.owner_id == owner_id, Topic.deleted_at.is_(None),
    ))).all())


def _encode_topic_cursor(created_at: datetime, topic_id: UUID, owner_id: int, is_active: bool | None) -> str:
    """Encode deterministic keyset state bound to owner and active filter."""
    value = json.dumps([created_at.isoformat(), str(topic_id), owner_id, is_active], separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_topic_cursor(cursor: str, owner_id: int, is_active: bool | None) -> tuple[datetime, UUID]:
    """Validate canonical cursor encoding and reject cross-owner/filter reuse."""
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError("noncanonical")
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 4:
            raise ValueError("invalid shape")
        timestamp, identifier, cursor_owner, cursor_active = value
        if (not isinstance(timestamp, str) or not isinstance(identifier, str)
                or type(cursor_owner) is not int
                or (cursor_active is not None and type(cursor_active) is not bool)):
            raise ValueError("invalid field types")
        parsed = datetime.fromisoformat(timestamp)
        if parsed.utcoffset() is None or cursor_owner != owner_id or cursor_active is not is_active:
            raise ValueError("scope mismatch")
        return parsed, UUID(identifier)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid or out-of-scope topic cursor") from exc


async def create_topic(session: AsyncSession, owner_id: int, payload: TopicCreate) -> TopicRead:
    """Create a validated owner profile and commit the caller's entire session.

    Entity references are write-validated first. The commit persists every pending
    change already attached to this session, not only the topic; callers that need
    a larger unit of work must not call this helper until they intend to commit it.
    Errors before commit leave rollback/disposal to the caller; after a failed
    commit, the caller must rollback before reusing the session.
    """
    from modules.knowledge.entities import public as entities
    await entities.get_entity_refs(session, payload.entity_ids, for_write=True)
    topic = Topic(owner_id=owner_id, name=payload.name, description=payload.description,
        keywords=payload.keywords, entity_ids=[str(item) for item in payload.entity_ids],
        is_active=payload.is_active, weight=payload.weight)
    session.add(topic)
    await session.flush()
    result = await _topic_read(session, topic)
    await session.commit()
    return result


async def get_topic(session: AsyncSession, owner_id: int, topic_id: UUID) -> TopicRead:
    """Return a live topic owned by the caller or hide it as missing."""
    topic = await session.scalar(select(Topic).where(Topic.id == topic_id, Topic.owner_id == owner_id, Topic.deleted_at.is_(None)))
    if topic is None:
        raise TopicMissing
    return await _topic_read(session, topic)


async def list_topics(session: AsyncSession, owner_id: int, filters: TopicFilter) -> TopicPage:
    """Read a bounded owner-scoped page using a cursor tied to its filter."""
    statement = select(Topic).where(Topic.owner_id == owner_id, Topic.deleted_at.is_(None))
    count_statement = select(func.count()).select_from(Topic).where(Topic.owner_id == owner_id, Topic.deleted_at.is_(None))
    if filters.is_active is not None:
        statement = statement.where(Topic.is_active == filters.is_active)
        count_statement = count_statement.where(Topic.is_active == filters.is_active)
    total = int(await session.scalar(count_statement) or 0)
    if filters.cursor:
        created_at, topic_id = _decode_topic_cursor(filters.cursor, owner_id, filters.is_active)
        statement = statement.where((Topic.created_at > created_at) | ((Topic.created_at == created_at) & (Topic.id > topic_id)))
    statement = statement.order_by(Topic.created_at, Topic.id).limit(filters.limit + 1)
    rows = list((await session.scalars(statement)).all())
    more = len(rows) > filters.limit
    rows = rows[:filters.limit]
    items = [await _topic_read(session, row) for row in rows]
    next_cursor = _encode_topic_cursor(rows[-1].created_at, rows[-1].id, owner_id, filters.is_active) if more and rows else None
    return TopicPage(items=items, next_cursor=next_cursor, total=total)


def _encode_topic_export_cursor(snapshot_at: datetime, created_at: datetime, topic_id: UUID) -> str:
    """Bind a canonical topic keyset position to one fixed export cutoff."""
    raw = json.dumps([1, "topics", snapshot_at.isoformat(), created_at.isoformat(), str(topic_id)],
                     separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_topic_export_cursor(cursor: str) -> tuple[datetime, datetime, UUID]:
    """Reject oversized, noncanonical, cross-dataset, or future topic export cursors."""
    try:
        if len(cursor) > 512 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 5 or value[:2] != [1, "topics"]:
            raise ValueError
        snapshot_at, created_at = datetime.fromisoformat(value[2]), datetime.fromisoformat(value[3])
        topic_id = UUID(value[4])
        if (any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, created_at))
                or snapshot_at.isoformat() != value[2] or created_at.isoformat() != value[3]
                or created_at > snapshot_at or snapshot_at > datetime.now(UTC)
                or str(topic_id) != value[4]
                or _encode_topic_export_cursor(snapshot_at, created_at, topic_id) != cursor):
            raise ValueError
        return snapshot_at, created_at, topic_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Topic export cursor is invalid") from exc


def _topic_export_scope(owner_id: int, snapshot_at: datetime) -> tuple[ColumnElement[bool], ...]:
    """Select only live owner topics that existed unchanged at the export cutoff."""
    return (
        Topic.owner_id == owner_id, Topic.deleted_at.is_(None),
        Topic.created_at <= snapshot_at, Topic.updated_at <= snapshot_at,
    )


async def export_page(
    session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50, cursor: str | None = None,
) -> TopicExportPage:
    """Return a bounded canonical owner topic page without tombstones or provider content."""
    if owner_id != 1 or record_kind != "topics" or not 1 <= limit <= 100:
        raise ValueError("Topic export owner, kind or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_topic_export_cursor(cursor)
        position = (position_at, position_id)
    scope = _topic_export_scope(owner_id, snapshot_at)
    snapshot_count = int(await session.scalar(select(func.count()).select_from(Topic).where(*scope)) or 0)
    statement = select(Topic).where(*scope)
    if position is not None:
        statement = statement.where(tuple_(Topic.created_at, Topic.id) > position)
    rows = list((await session.scalars(
        statement.order_by(Topic.created_at, Topic.id).limit(limit + 1).execution_options(populate_existing=True)
    )).all())
    has_more, rows = len(rows) > limit, rows[:limit]
    items = [await _topic_read(session, row) for row in rows]
    encoded = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, encoded)) + max(0, len(items) - 1)
    if payload_bytes > 16_777_216:
        raise HTTPException(status_code=413, detail="Topic export page exceeds its byte bound")
    fences = [TopicExportFence(
        id=row.id, created_at=row.created_at, updated_at=row.updated_at, revision=row.revision,
        content_digest=hashlib.sha256(raw).hexdigest(),
    ) for row, raw in zip(rows, encoded, strict=True)]
    return TopicExportPage(
        owner_id=owner_id, record_kind="topics", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences, payload_bytes=payload_bytes,
        next_cursor=_encode_topic_export_cursor(snapshot_at, rows[-1].created_at, rows[-1].id)
        if has_more and rows else None,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[TopicExportFence],
) -> TopicExportValidation:
    """Re-read topic projections and inventory before publishing the portable download."""
    if owner_id != 1 or record_kind != "topics" or len(fences) > 100:
        raise ValueError("Topic export validation input is invalid")
    observed = int(await session.scalar(
        select(func.count()).select_from(Topic).where(*_topic_export_scope(owner_id, snapshot_at))
    ) or 0)
    if observed != expected_snapshot_count:
        return TopicExportValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = await session.scalar(select(Topic).where(
            Topic.id == fence.id, *_topic_export_scope(owner_id, snapshot_at),
        ).execution_options(populate_existing=True))
        if row is None:
            return TopicExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        item = await _topic_read(session, row)
        if (item.created_at != fence.created_at or item.updated_at != fence.updated_at
                or item.revision != fence.revision
                or hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest() != fence.content_digest):
            return TopicExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return TopicExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)


async def update_topic(session: AsyncSession, owner_id: int, topic_id: UUID, payload: TopicUpdate) -> TopicRead:
    """Apply a revision-fenced patch and commit the caller's entire session.

    The owner row lock and expected revision are checked before changes. Entity
    references are validated before assigning them. Commit also persists any
    other pending work in this session; callers own rollback on pre-commit errors
    and must rollback after a failed commit before they reuse the session.
    """
    from modules.knowledge.entities import public as entities
    statement = select(Topic).where(Topic.id == topic_id, Topic.owner_id == owner_id, Topic.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True)
    topic = await session.scalar(statement)
    if topic is None:
        raise TopicMissing
    if topic.revision != payload.expected_revision:
        raise TopicConflict("stale_revision", "Topic changed since it was loaded", topic.revision)
    if topic.revision >= MAX_REVISION:
        raise TopicConflict("revision_exhausted", "Topic revision cannot be incremented", topic.revision)
    changes = payload.model_dump(exclude_unset=True, exclude={"expected_revision"})
    if "entity_ids" in changes and changes["entity_ids"] is not None:
        await entities.get_entity_refs(session, changes["entity_ids"], for_write=True)
        changes["entity_ids"] = [str(item) for item in changes["entity_ids"]]
    for key, value in changes.items():
        setattr(topic, key, value)
    topic.revision += 1
    topic.updated_at = datetime.now(UTC)
    result = await _topic_read(session, topic)
    await session.commit()
    return result


async def delete_topic(session: AsyncSession, owner_id: int, topic_id: UUID, expected_revision: int) -> None:
    """Tombstone a profile and commit the caller's entire session.

    The owner-scoped lock is refreshed from the database before comparing the
    expected revision. Commit persists every pending session change; callers own
    rollback/disposal after pre-commit errors and must rollback after commit
    failure before reusing the session.
    """
    topic = await session.scalar(select(Topic).where(Topic.id == topic_id, Topic.owner_id == owner_id, Topic.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True))
    if topic is None:
        raise TopicMissing
    if topic.revision != expected_revision:
        raise TopicConflict("stale_revision", "Topic changed since it was loaded", topic.revision)
    if topic.revision >= MAX_REVISION:
        raise TopicConflict("revision_exhausted", "Topic revision cannot be incremented", topic.revision)
    topic.deleted_at = datetime.now(UTC)
    topic.revision += 1
    topic.updated_at = topic.deleted_at
    await session.commit()


class TopicService:
    """Injectable facade whose mutations commit every pending change in its session.

    Reads leave transaction ownership untouched. Mutation methods delegate to the
    committing functions below; callers must treat them as unit-of-work boundaries,
    rollback after failed commits before session reuse, and handle their own
    uncommitted work when an operation raises before commit.
    """
    def __init__(self, session: AsyncSession) -> None:
        """Bind topic operations to the caller's active unit of work."""
        self.session = session

    async def create_topic(self, owner_id: int, payload: TopicCreate) -> TopicRead:
        """Create and commit through the owner contract, including other pending session work."""
        return await create_topic(self.session, owner_id, payload)

    async def get_topic(self, owner_id: int, topic_id: UUID) -> TopicRead:
        """Read a live profile through the owner contract."""
        return await get_topic(self.session, owner_id, topic_id)

    async def list_topics(self, owner_id: int, filters: TopicFilter) -> TopicPage:
        """Page owner profiles through the public query contract."""
        return await list_topics(self.session, owner_id, filters)

    async def update_topic(self, owner_id: int, topic_id: UUID, payload: TopicUpdate) -> TopicRead:
        """Patch and commit through the owner contract, including other pending session work."""
        return await update_topic(self.session, owner_id, topic_id, payload)

    async def delete_topic(self, owner_id: int, topic_id: UUID, expected_revision: int) -> None:
        """Tombstone and commit through the owner contract, including other pending session work."""
        await delete_topic(self.session, owner_id, topic_id, expected_revision)


from modules.settings.public import module_dependency

router = APIRouter(prefix="/api/v1/topics", tags=["news"], dependencies=[Depends(module_dependency("news"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


def _no_store(response: Response) -> None:
    """Prevent shared or browser caches from retaining private topic profiles."""
    response.headers["Cache-Control"] = "private, no-store"


def _raise_topic_error(exc: Exception) -> NoReturn:
    """Translate domain missing/conflict failures into stable protected HTTP errors."""
    if isinstance(exc, TopicMissing):
        raise HTTPException(404, detail={"code": "topic_not_found", "message": "Topic not found", "details": {}}) from exc
    if isinstance(exc, TopicConflict):
        raise HTTPException(409, detail={"code": exc.code, "message": str(exc), "details": {"current_revision": exc.current_revision}}) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(422, detail={"code": "invalid_topic_cursor", "message": str(exc), "details": {}}) from exc
    raise exc


@router.get("", response_model=TopicPage)
async def list_topics_route(session: Session, owner: OwnerRead, response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: Annotated[str | None, Query(max_length=512)] = None,
    is_active: Annotated[bool | None, Query()] = None) -> TopicPage:
    """Return a no-store, bounded page of current topics for the authenticated owner."""
    _no_store(response)
    try:
        return await list_topics(session, owner.owner_id, TopicFilter(limit=limit, cursor=cursor, is_active=is_active))
    except ValueError as exc:
        _raise_topic_error(exc)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TopicRead)
async def create_topic_route(payload: TopicCreate, session: Session, owner: OwnerWrite, response: Response) -> TopicRead:
    """Create a topic only after owner-write authorization and entity validation."""
    _no_store(response)
    try:
        return await create_topic(session, owner.owner_id, payload)
    except (LookupError, ValueError) as exc:
        raise HTTPException(422, detail={"code": "invalid_entity_reference", "message": str(exc), "details": {}}) from exc


@router.get("/{topic_id}", response_model=TopicRead)
async def get_topic_route(topic_id: UUID, session: Session, owner: OwnerRead, response: Response) -> TopicRead:
    """Read one current owner profile with private cache controls."""
    _no_store(response)
    try:
        return await get_topic(session, owner.owner_id, topic_id)
    except (TopicMissing, TopicConflict) as exc:
        _raise_topic_error(exc)


@router.patch("/{topic_id}", response_model=TopicRead)
async def update_topic_route(topic_id: UUID, payload: TopicUpdate, session: Session, owner: OwnerWrite, response: Response) -> TopicRead:
    """Apply a revision-fenced owner update behind write authorization."""
    _no_store(response)
    try:
        return await update_topic(session, owner.owner_id, topic_id, payload)
    except (TopicMissing, TopicConflict) as exc:
        _raise_topic_error(exc)
    except (LookupError, ValueError) as exc:
        raise HTTPException(422, detail={"code": "invalid_entity_reference", "message": str(exc), "details": {}}) from exc


@router.delete("/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic_route(topic_id: UUID, expected_revision: Annotated[int, Query(ge=1, le=MAX_REVISION)],
    session: Session, owner: OwnerWrite, response: Response) -> None:
    """Tombstone one owner profile using the revision observed by the client."""
    _no_store(response)
    try:
        await delete_topic(session, owner.owner_id, topic_id, expected_revision)
    except (TopicMissing, TopicConflict) as exc:
        _raise_topic_error(exc)
