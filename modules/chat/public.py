"""Public module contracts and API boundary for the chat capability."""

from modules.chat.citations import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    ensure_grounded_answer,
    validate_answer_citations,
    validate_citations,
)
from modules.chat.models import (
    Conversation,
    Message,
    ResponseRun,
    StreamEvent,
)
from modules.chat.retrieval import (
    build_context,
    format_grounded_context,
    revalidate_context_fence,
)
from modules.chat.schemas import (
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
from modules.chat.stream import (
    StreamBuffer,
    format_sse_event,
    make_event_id,
    parse_event_id,
)
from modules.chat.worker import (
    is_history_storage_enabled,
    process_chat_response,
    purge_expired_chat_runs,
    run_response_generation,
)

__all__ = [
    "AnswerContext",
    "AnswerContextRequest",
    "CancelResponse",
    "Citation",
    "CitationValidationResult",
    "Conversation",
    "ConversationCreate",
    "ConversationDetailRead",
    "ConversationPatch",
    "ConversationRead",
    "EntityContextItem",
    "EvidenceItem",
    "INSUFFICIENT_EVIDENCE_MESSAGE",
    "Message",
    "MessageRead",
    "ResponseRun",
    "ResponseRunRead",
    "SelectedEvidenceRef",
    "SendMessageRequest",
    "SendMessageResponse",
    "StreamBuffer",
    "StreamEvent",
    "TemporalContextItem",
    "ValidatedAnswer",
    "build_context",
    "ensure_grounded_answer",
    "format_grounded_context",
    "format_sse_event",
    "is_history_storage_enabled",
    "make_event_id",
    "parse_event_id",
    "process_chat_response",
    "purge_expired_chat_runs",
    "revalidate_context_fence",
    "run_response_generation",
    "validate_answer_citations",
    "validate_citations",
]

