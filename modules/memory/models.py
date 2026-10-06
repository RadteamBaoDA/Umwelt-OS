"""Private persistence models for selective memory, candidates, and privacy management."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from core.database import Base


class Memory(Base):
    """Store persistent memory items, provenance links, confidence, and lifecycle state.

    Differentiates manual owner-created facts from model-derived candidates. Supports
    active, invalidated, superseded, and forgotten lifecycle states.
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_status", "status"),
        Index("ix_memories_memory_type", "memory_type"),
        Index("ix_memories_created_at", "created_at"),
        Index("ix_memories_is_manual", "is_manual"),
        # Whole-Source purge selects exact provenance.source_id equality and candidate linkage.
        Index("ix_memories_provenance_source_id", text("(provenance ->> 'source_id')"), "id"),
        Index("ix_memories_candidate_id", "candidate_id"),
        CheckConstraint(
            "status IN ('active', 'invalidated', 'superseded', 'forgotten')",
            name="ck_memories_status",
        ),
        CheckConstraint(
            "memory_type IN ('fact', 'preference', 'instruction', 'decision', 'procedural')",
            name="ck_memories_type",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memories_confidence_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    provenance: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("memories.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("memory_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forgotten_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    candidate: Mapped["MemoryCandidate | None"] = relationship(
        "MemoryCandidate", foreign_keys=[candidate_id], back_populates="accepted_memory"
    )


class MemoryCandidate(Base):
    """Store evaluated memory candidates extracted from conversations or sources prior to acceptance."""

    __tablename__ = "memory_candidates"
    __table_args__ = (
        Index("ix_memory_candidates_status", "status"),
        Index("ix_memory_candidates_created_at", "created_at"),
        Index("ix_memory_candidates_provenance_source_id", text("(provenance ->> 'source_id')"), "id"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'superseded', 'expired')",
            name="ck_memory_candidates_status",
        ),
        CheckConstraint(
            "memory_type IN ('fact', 'preference', 'instruction', 'decision', 'procedural')",
            name="ck_memory_candidates_type",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_candidates_confidence_range",
        ),
        CheckConstraint(
            "novelty_score >= 0.0 AND novelty_score <= 1.0",
            name="ck_memory_candidates_novelty_range",
        ),
        CheckConstraint(
            "usefulness_score >= 0.0 AND usefulness_score <= 1.0",
            name="ck_memory_candidates_usefulness_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    provenance: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    novelty_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    usefulness_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    accepted_memory: Mapped["Memory | None"] = relationship(
        "Memory", foreign_keys=[Memory.candidate_id], back_populates="candidate", uselist=False
    )


class MemoryPrivacyRecord(Base):
    """Persist owner-scoped memory and conversation privacy controls.

    Enforces the single-user boundary via owner_id check constraint. Controls
    conversation history persistence, agent memory persistence, and auto-acceptance.
    """

    __tablename__ = "memory_privacy_settings"
    __table_args__ = (
        CheckConstraint(
            "owner_id = 1",
            name="ck_memory_privacy_settings_single_owner",
        ),
    )

    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True
    )
    store_conversation_history: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    store_agent_memory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    auto_accept_memory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
