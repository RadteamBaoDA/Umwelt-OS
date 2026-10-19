"""Private persistence models for chat conversations, messages, response runs, and stream events."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class Conversation(Base):
    """Store persistent chat conversations, optional context linkage, and owner lifecycle state.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index("ix_chat_conversations_created_at", "created_at"),
        Index("ix_chat_conversations_updated_at", "updated_at"),
        Index("ix_chat_conversations_archived", "archived"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_chat_conversations_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_chat_conversations_principal", ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "id", name="uq_w2_chat_conversations_id"),
        Index("ix_w2_chat_conversations_scope", 'workspace_id', 'id'),
        Index("ix_w2_chat_conversations_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New conversation")
    context_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context_resource_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    # passive_deletes: chat_messages/chat_response_runs FKs are ON DELETE CASCADE (models + p06/p14
    # migrations), so deleting a Conversation lets Postgres remove children instead of the ORM
    # loading and deleting them row by row. delete-orphan is kept for in-session removals.
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True,
        order_by="Message.created_at",
    )
    response_runs: Mapped[list["ResponseRun"]] = relationship(
        "ResponseRun", back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True,
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
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    response_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revision_of_message_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")


class ResponseRun(Base):
    """Store generation runs, model metadata, execution status, and grounding context.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "chat_response_runs"
    __table_args__ = (
        Index("ix_chat_response_runs_conversation_id", "conversation_id"),
        Index("ix_chat_response_runs_client_request_id", "client_request_id"),
        Index("ix_chat_response_runs_status", "status"),
        Index("ix_chat_response_runs_created_at", "created_at"),
        Index("ix_chat_response_runs_user_message_id", "user_message_id"),
        Index("ix_chat_response_runs_assistant_message_id", "assistant_message_id"),
        Index(
            "ix_chat_response_runs_terminal_completed", "completed_at",
            postgresql_where=text("status IN ('completed', 'cancelled', 'failed')"),
        ),
        CheckConstraint(
            "status IN ('pending', 'streaming', 'awaiting_input', 'completed', 'cancelled', 'failed')",
            name="ck_chat_response_runs_status",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_chat_response_runs_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "actor_user_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_chat_response_runs_principal", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "conversation_id"], ["chat_conversations.workspace_id", "chat_conversations.id"], name="fk_w2_chat_response_runs_conversation_id", ondelete="CASCADE"),
        Index("ix_w2_chat_response_runs_scope", 'workspace_id', 'id'),
        Index("ix_w2_chat_response_runs_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
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
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    retrieval_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The single open `request_user_input` request while status is awaiting_input (see modules/chat/user_input.py).
    input_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="response_runs")
    stream_events: Mapped[list["StreamEvent"]] = relationship(
        "StreamEvent", back_populates="response_run", cascade="all, delete-orphan", passive_deletes=True,
        order_by="StreamEvent.seq",  # chat_stream_events.response_id is ON DELETE CASCADE
    )



class MessageMutationReceipt(Base):
    """Persist idempotent append-only edit/regenerate outcomes for one conversation request key."""

    __tablename__ = "chat_message_mutation_receipts"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_request_id", name="uq_chat_message_mutation_request"),
        CheckConstraint("action IN ('edit', 'regenerate')", name="ck_chat_message_mutation_action"),
        CheckConstraint("length(request_digest) = 64", name="ck_chat_message_mutation_digest"),
        Index("ix_chat_message_mutation_receipts_response_id", "response_id"),
        Index("ix_chat_message_mutation_receipts_target_message_id", "target_message_id"),
        Index("ix_chat_message_mutation_receipts_result_user_message_id", "result_user_message_id"),
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
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    response_run: Mapped["ResponseRun"] = relationship("ResponseRun", back_populates="stream_events")


class AgentActivityLink(Base):
    """Store bounded agent activity owned by a chat conversation and its privacy retention.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "chat_agent_activity_links"
    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_chat_agent_activity_run"),
        Index("ix_chat_agent_activity_conversation", "conversation_id", "updated_at"),
        CheckConstraint("length(auth_session_hash) = 64", name="ck_chat_agent_activity_owner_auth"),
        CheckConstraint("jsonb_array_length(activities) <= 64", name="ck_chat_agent_activity_bound"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_chat_agent_activity_links_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_chat_agent_activity_links_principal", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "conversation_id"], ["chat_conversations.workspace_id", "chat_conversations.id"], name="fk_w2_chat_agent_activity_links_conversation_id", ondelete="CASCADE"),
        Index("ix_w2_chat_agent_activity_links_scope", 'workspace_id', 'id'),
        Index("ix_w2_chat_agent_activity_links_work", 'workspace_id', 'created_at', 'id'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    agent_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    activities: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())



class ChatTodo(Base):
    """Store one conversation-scoped to-do item managed by the model tools or the owner's panel.

    Rows hold only owner/model-authored task text, never document or evidence content. The composite
    foreign key cascades from ``chat_conversations`` so conversation deletion and retention purges remove
    todos with the transcript; there is no separate soft-delete state to leak past a purge.
    """

    __tablename__ = "chat_todos"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'completed')", name="ck_chat_todos_status"),
        CheckConstraint("position >= 0", name="ck_chat_todos_position"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_chat_todos_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"], ["chat_conversations.workspace_id", "chat_conversations.id"],
            name="fk_chat_todos_conversation", ondelete="CASCADE",
        ),
        Index("ix_chat_todos_conversation_position", "conversation_id", "position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, server_default="")
    emoji: Mapped[str] = mapped_column(String(8), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
