"""Private persistence models for chat conversations, messages, response runs, and stream events."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from core.database import Base


class Conversation(Base):
    """Store persistent chat conversations, optional context linkage, and owner lifecycle state."""

    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index("ix_chat_conversations_created_at", "created_at"),
        Index("ix_chat_conversations_updated_at", "updated_at"),
        Index("ix_chat_conversations_archived", "archived"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New conversation")
    context_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context_resource_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )
    response_runs: Mapped[list["ResponseRun"]] = relationship(
        "ResponseRun", back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    """Store immutable transcript entries and links to append-only prompt or answer revisions."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_conversation_id", "conversation_id"),
        Index("ix_chat_messages_client_request_id", "client_request_id"),
        Index("ix_chat_messages_response_id", "response_id"),
        Index("ix_chat_messages_revision_of_message_id", "revision_of_message_id"),
        Index("ix_chat_messages_created_at", "created_at"),
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_chat_messages_role"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    metadata_json: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    response_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revision_of_message_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")


class ResponseRun(Base):
    """Store generation runs, model metadata, execution status, and grounding context."""

    __tablename__ = "chat_response_runs"
    __table_args__ = (
        Index("ix_chat_response_runs_conversation_id", "conversation_id"),
        Index("ix_chat_response_runs_client_request_id", "client_request_id"),
        Index("ix_chat_response_runs_status", "status"),
        Index("ix_chat_response_runs_created_at", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'cancelled', 'failed')",
            name="ck_chat_response_runs_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    client_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    model_alias: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    retrieval_context: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="response_runs")
    stream_events: Mapped[list["StreamEvent"]] = relationship(
        "StreamEvent", back_populates="response_run", cascade="all, delete-orphan", order_by="StreamEvent.seq"
    )


class MessageMutationReceipt(Base):
    """Persist idempotent append-only edit/regenerate outcomes for one conversation request key."""

    __tablename__ = "chat_message_mutation_receipts"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_request_id", name="uq_chat_message_mutation_request"),
        CheckConstraint("action IN ('edit', 'regenerate')", name="ck_chat_message_mutation_action"),
        CheckConstraint("length(request_digest) = 64", name="ck_chat_message_mutation_digest"),
        Index("ix_chat_message_mutation_receipts_response_id", "response_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    target_message_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    result_user_message_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    response_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_response_runs.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StreamEvent(Base):
    """Store sequenced, replayable SSE chunks for a generation run."""

    __tablename__ = "chat_stream_events"
    __table_args__ = (
        Index("ix_chat_stream_events_response_id", "response_id"),
        Index("ix_chat_stream_events_seq", "response_id", "seq"),
        UniqueConstraint("response_id", "seq", name="uq_chat_stream_events_seq"),
        UniqueConstraint("event_id", name="uq_chat_stream_events_event_id"),
        CheckConstraint("seq >= 1", name="ck_chat_stream_events_seq_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    response_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_response_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    response_run: Mapped["ResponseRun"] = relationship("ResponseRun", back_populates="stream_events")


class AgentActivityLink(Base):
    """Store bounded agent activity owned by a chat conversation and its privacy retention."""

    __tablename__ = "chat_agent_activity_links"
    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_chat_agent_activity_run"),
        Index("ix_chat_agent_activity_conversation", "conversation_id", "updated_at"),
        CheckConstraint("owner_id = 1 AND length(auth_session_hash) = 64", name="ck_chat_agent_activity_owner_auth"),
        CheckConstraint("jsonb_array_length(activities) <= 64", name="ck_chat_agent_activity_bound"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    activities: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
