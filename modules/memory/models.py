"""Private persistence models for selective memory, candidates, and privacy management."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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

    Workspace identity is mandatory and survives nullable or detached canonical references.
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
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_memories_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_memories_principal", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_memories_id"),
        # Scalar SET NULL clears only the parent ID; this deferred FK retains workspace.
        ForeignKeyConstraint(["workspace_id", "superseded_by_id"], ["memories.workspace_id", "memories.id"], name="fk_w2_memories_superseded_by_id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        ForeignKeyConstraint(["workspace_id", "candidate_id"], ["memory_candidates.workspace_id", "memory_candidates.id"], name="fk_w2_memories_candidate_id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        Index("ix_w2_memories_scope", 'workspace_id', 'id'),
        Index("ix_w2_memories_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    provenance: Mapped[dict[str, Any]] = mapped_column(
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
    """Store evaluated memory candidates extracted from conversations or sources prior to acceptance.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

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
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_memory_candidates_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_memory_candidates_principal", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_memory_candidates_id"),
        Index("ix_w2_memory_candidates_scope", 'workspace_id', 'id'),
        Index("ix_w2_memory_candidates_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    provenance: Mapped[dict[str, Any]] = mapped_column(
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
    """Persist owner-scoped memory and conversation privacy controls. Controls
    conversation history persistence, agent memory persistence, and auto-acceptance.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "memory_privacy_settings"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_memory_privacy_settings_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_memory_privacy_settings_principal", ondelete="RESTRICT"),
        Index("ix_w2_memory_privacy_settings_scope", 'workspace_id', 'owner_id'),
        Index("ix_w2_memory_privacy_settings_work", 'workspace_id', 'created_at', 'owner_id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


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

