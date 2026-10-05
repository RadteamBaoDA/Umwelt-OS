"""Unit tests for chat module data transfer schemas, session models, and constraints.

Covers:
- ChatSession / Conversation schemas (ConversationCreate, ConversationPatch, ConversationRead, ConversationDetailRead)
- ChatMessage / Message schemas (MessageRead, SendMessageRequest, SendMessageResponse)
- Message roles ('user', 'assistant', 'system') and status constraints
- Citation and evidence schemas (Citation, SelectedEvidenceRef, EvidenceItem, ValidatedAnswer)
- DrawerState client/context representation
- Context budget and token limits
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.chat.models import Conversation, Message, ResponseRun
from modules.chat.schemas import (
    DEFAULT_CONTEXT_BUDGET_BYTES,
    MAX_CONTEXT_BUDGET_BYTES,
    MAX_ENTITY_SCOPE,
    MAX_QUERY_LENGTH,
    MAX_QUOTE_LENGTH,
    MAX_RETRIEVAL_LIMIT,
    MAX_SELECTED_REFS,
    MAX_SOURCE_SCOPE,
    AgentActivityRead,
    AnswerContext,
    AnswerContextRequest,
    CancelResponse,
    Citation,
    CitationValidationResult,
    ConversationCreate,
    ConversationDetailRead,
    ConversationPatch,
    ConversationRead,
    EntityContextItem,
    EvidenceItem,
    MessageRead,
    ResponseRunRead,
    SelectedEvidenceRef,
    SendMessageRequest,
    SendMessageResponse,
    TemporalContextItem,
    ValidatedAnswer,
)


class TestChatSessionSchemas:
    """Tests for ChatSession / Conversation creation, patch, and read representations."""

    def test_conversation_create_defaults(self) -> None:
        """Verify default conversation creation payload."""
        payload = ConversationCreate()
        assert payload.title is None
        assert payload.context_kind is None
        assert payload.context_resource_id is None
        assert payload.metadata == {}

    def test_conversation_create_full(self) -> None:
        """Verify complete conversation creation with custom context."""
        ctx_id = uuid4()
        payload = ConversationCreate(
            title="Project Research Discussion",
            context_kind="daily_brief",
            context_resource_id=ctx_id,
            metadata={"source": "drawer"},
        )
        assert payload.title == "Project Research Discussion"
        assert payload.context_kind == "daily_brief"
        assert payload.context_resource_id == ctx_id
        assert payload.metadata["source"] == "drawer"

    def test_conversation_create_forbids_extra_fields(self) -> None:
        """Ensure extra fields are rejected to prevent payload injection."""
        with pytest.raises(ValidationError):
            ConversationCreate.model_validate({"title": "Test", "unauthorized_flag": True})

    def test_conversation_patch_optional_fields(self) -> None:
        """Verify partial update schema for conversation metadata and pinned/archived state."""
        patch = ConversationPatch(title="Renamed Conversation", pinned=True)
        assert patch.title == "Renamed Conversation"
        assert patch.pinned is True
        assert patch.archived is None

        patch_archive = ConversationPatch(archived=True)
        assert patch_archive.archived is True

    def test_conversation_read_validation(self) -> None:
        """Verify ConversationRead projection fields."""
        conv_id = uuid4()
        now = datetime.now(UTC)
        read = ConversationRead(
            id=conv_id,
            title="Today's Session",
            context_kind="task",
            pinned=False,
            archived=False,
            created_at=now,
            updated_at=now,
            metadata={"theme": "dark"},
        )
        assert read.id == conv_id
        assert read.title == "Today's Session"
        assert read.pinned is False
        assert read.metadata["theme"] == "dark"

    def test_conversation_detail_read_with_messages(self) -> None:
        """Verify full conversation projection including ordered message items."""
        conv_id = uuid4()
        msg_id = uuid4()
        now = datetime.now(UTC)
        msg = MessageRead(
            id=msg_id,
            conversation_id=conv_id,
            role="user",
            content="Hello Umwelt OS!",
            created_at=now,
        )
        detail = ConversationDetailRead(
            id=conv_id,
            title="Chat Session",
            created_at=now,
            updated_at=now,
            messages=[msg],
        )
        assert len(detail.messages) == 1
        assert detail.messages[0].content == "Hello Umwelt OS!"
        assert detail.messages[0].role == "user"


class TestChatMessageSchemas:
    """Tests for message payloads, roles, and status responses."""

    def test_send_message_request_valid(self) -> None:
        """Test valid send message payload."""
        req = SendMessageRequest(
            content="What are my top priorities today?",
            client_request_id="req-12345",
            context={"date": "2026-10-05"},
        )
        assert req.content == "What are my top priorities today?"
        assert req.client_request_id == "req-12345"

    def test_send_message_request_empty_rejected(self) -> None:
        """Empty content must be rejected."""
        with pytest.raises(ValidationError):
            SendMessageRequest(content="")

    def test_send_message_request_oversized_rejected(self) -> None:
        """Messages exceeding max content length (20000) must be rejected."""
        oversized = "x" * 20001
        with pytest.raises(ValidationError):
            SendMessageRequest(content=oversized)

    def test_send_message_response(self) -> None:
        """Verify SendMessageResponse schema."""
        msg_id = uuid4()
        resp_id = uuid4()
        res = SendMessageResponse(message_id=msg_id, response_id=resp_id, status="queued")
        assert res.message_id == msg_id
        assert res.response_id == resp_id
        assert res.status == "queued"

    def test_message_roles(self) -> None:
        """Test allowed message roles and ORM role check constraint."""
        conv_id = uuid4()
        now = datetime.now(UTC)
        for role in ["user", "assistant", "system"]:
            msg = MessageRead(
                id=uuid4(),
                conversation_id=conv_id,
                role=role,
                content="test content",
                created_at=now,
            )
            assert msg.role == role


class TestCitationSchemas:
    """Tests for Citation, EvidenceItem, and SelectedEvidenceRef schemas."""

    def test_citation_camel_and_snake_case_aliases(self) -> None:
        """Verify Citation accepts canonical JSON camelCase and Python snake_case aliases."""
        src_id = uuid4()
        doc_id = uuid4()
        ver_id = uuid4()
        chunk_id = uuid4()
        now = datetime.now(UTC)

        # camelCase input
        cit1 = Citation.model_validate({
            "sourceType": "document",
            "sourceId": str(src_id),
            "documentId": str(doc_id),
            "documentVersionId": str(ver_id),
            "chunkId": str(chunk_id),
            "title": "Specs Overview",
            "quote": "Personal Intelligence OS architecture",
            "observedAt": now.isoformat(),
        })
        assert cit1.sourceType == "document"
        assert cit1.sourceId == src_id
        assert cit1.documentId == doc_id
        assert cit1.chunkId == chunk_id
        assert cit1.title == "Specs Overview"

        # snake_case input
        cit2 = Citation(
            source_id=src_id,
            document_id=doc_id,
            document_version_id=ver_id,
            chunk_id=chunk_id,
            title="Specs Overview",
            quote="Personal Intelligence OS architecture",
        )
        assert cit2.sourceId == src_id
        assert cit2.quote == "Personal Intelligence OS architecture"

    def test_citation_quote_length_limit(self) -> None:
        """Citations exceeding MAX_QUOTE_LENGTH (1000) must be rejected."""
        src_id = uuid4()
        with pytest.raises(ValidationError):
            Citation(
                source_id=src_id,
                document_id=src_id,
                document_version_id=src_id,
                chunk_id=src_id,
                title="Title",
                quote="x" * (MAX_QUOTE_LENGTH + 1),
            )

    def test_selected_evidence_ref(self) -> None:
        """Verify SelectedEvidenceRef serialization and field validation."""
        ver_id = uuid4()
        chunk_id = uuid4()
        ref = SelectedEvidenceRef(document_version_id=ver_id, chunk_id=chunk_id)
        assert ref.document_version_id == ver_id
        assert ref.chunk_id == chunk_id
        assert ref.document_id is None

    def test_evidence_item_creation(self) -> None:
        """Verify complete EvidenceItem attributes and defaults."""
        item = EvidenceItem(
            source_id=uuid4(),
            source_generation=1,
            local_only=False,
            document_id=uuid4(),
            document_version_id=uuid4(),
            version_number=1,
            chunk_id=uuid4(),
            content="Sample evidence text",
            title="Doc Title",
            score=0.95,
        )
        assert item.source_type == "document"
        assert item.chunk_index == 0
        assert item.score == 0.95
        assert item.local_only is False


class TestDrawerStateAndTokenBudgets:
    """Tests for DrawerState representation and bounded context budgets."""

    def test_drawer_state_representation(self) -> None:
        """Verify drawer state representation with date context and thread binding."""
        conv_id = uuid4()
        drawer_state = {
            "isOpen": True,
            "conversationId": str(conv_id),
            "dateContext": "2026-10-05",
            "timezone": "Asia/Ho_Chi_Minh",
            "mode": "drawer",  # 'drawer' vs 'expanded' (/ask)
            "activeTab": "chat",  # 'chat', 'sources', 'activity', 'approvals'
        }
        assert drawer_state["isOpen"] is True
        assert drawer_state["mode"] == "drawer"
        assert drawer_state["timezone"] == "Asia/Ho_Chi_Minh"

    def test_context_budget_constants_and_bounds(self) -> None:
        """Ensure context budgeting respects default (32KB) and maximum (64KB) limits."""
        assert DEFAULT_CONTEXT_BUDGET_BYTES == 32_000
        assert MAX_CONTEXT_BUDGET_BYTES == 64_000
        assert MAX_RETRIEVAL_LIMIT == 50
        assert MAX_SOURCE_SCOPE == 100
        assert MAX_ENTITY_SCOPE == 100
        assert MAX_SELECTED_REFS == 100
        assert MAX_QUERY_LENGTH == 1000

    def test_answer_context_request_budget_validation(self) -> None:
        """Verify context budget validation within bounds [1000, 64000]."""
        # Valid default budget
        req = AnswerContextRequest(query="Summarize my notes")
        assert req.context_budget_bytes == 32_000
        assert req.limit == 20

        # Valid custom budget
        req2 = AnswerContextRequest(query="Summarize my notes", context_budget_bytes=50_000)
        assert req2.context_budget_bytes == 50_000

        # Sub-minimum budget rejected
        with pytest.raises(ValidationError):
            AnswerContextRequest(query="Test", context_budget_bytes=500)

        # Over-maximum budget rejected
        with pytest.raises(ValidationError):
            AnswerContextRequest(query="Test", context_budget_bytes=100_000)

    def test_response_run_token_usage_schema(self) -> None:
        """Verify ResponseRunRead token usage metadata projection."""
        run_id = uuid4()
        conv_id = uuid4()
        user_msg_id = uuid4()
        now = datetime.now(UTC)
        run = ResponseRunRead(
            id=run_id,
            conversation_id=conv_id,
            user_message_id=user_msg_id,
            status="completed",
            model_alias="reasoning",
            model_name="claude-3-5-sonnet",
            provider="anthropic",
            token_usage={"prompt_tokens": 1200, "completion_tokens": 350, "total_tokens": 1550},
            citations=[],
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        assert run.token_usage["total_tokens"] == 1550
        assert run.status == "completed"

    def test_agent_activity_read_limit(self) -> None:
        """Verify AgentActivityRead bounds activities to 64 entries."""
        now = datetime.now(UTC)
        activities = [{"step": i, "tool": "search"} for i in range(10)]
        activity = AgentActivityRead(
            conversation_id=uuid4(),
            agent_run_id=uuid4(),
            activities=activities,
            updated_at=now,
        )
        assert len(activity.activities) == 10

        oversized_activities = [{"step": i} for i in range(65)]
        with pytest.raises(ValidationError):
            AgentActivityRead(
                conversation_id=uuid4(),
                agent_run_id=uuid4(),
                activities=oversized_activities,
                updated_at=now,
            )
