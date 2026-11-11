"""Public module contracts and API boundary for the chat capability."""

# Cross-module contract re-exports (importers use this public module, never private submodules).
from modules.chat.generative_ui import (
    external_router,
)
from modules.chat.web_search import WebSearchError
from modules.chat.web_search import search as run_web_search

# isort: split


from modules.chat._public.agents import (
    _require_activity_owner as _require_activity_owner,
)
from modules.chat._public.agents import (
    authorize_agent_run_access as authorize_agent_run_access,
)
from modules.chat._public.agents import (
    filter_live_agent_run_ids as filter_live_agent_run_ids,
)
from modules.chat._public.agents import (
    get_agent_activity as get_agent_activity,
)
from modules.chat._public.agents import (
    get_run_meta_by_id as get_run_meta_by_id,
)
from modules.chat._public.agents import (
    has_live_agent_run_link as has_live_agent_run_link,
)
from modules.chat._public.agents import (
    link_agent_run as link_agent_run,
)
from modules.chat._public.agents import (
    list_agent_run_ids_for_delete as list_agent_run_ids_for_delete,
)
from modules.chat._public.agents import (
    list_agent_run_ids_for_owner as list_agent_run_ids_for_owner,
)
from modules.chat._public.agents import (
    list_run_meta as list_run_meta,
)
from modules.chat._public.agents import (
    live_agent_conversation_id as live_agent_conversation_id,
)
from modules.chat._public.agents import (
    publish_agent_activity as publish_agent_activity,
)
from modules.chat._public.cleanup import (
    _EVIDENCE_FIELDS as _EVIDENCE_FIELDS,
)
from modules.chat._public.cleanup import (
    _REMOVE_EVIDENCE as _REMOVE_EVIDENCE,
)
from modules.chat._public.cleanup import (
    CopiedEvidenceCleanupProgress as CopiedEvidenceCleanupProgress,
)
from modules.chat._public.cleanup import (
    _admit_cleanup_page as _admit_cleanup_page,
)
from modules.chat._public.cleanup import (
    _cleanup_scope_fingerprint as _cleanup_scope_fingerprint,
)
from modules.chat._public.cleanup import (
    _cleanup_scope_parts as _cleanup_scope_parts,
)
from modules.chat._public.cleanup import (
    _cleanup_uuid as _cleanup_uuid,
)
from modules.chat._public.cleanup import (
    _decode_cleanup_cursor as _decode_cleanup_cursor,
)
from modules.chat._public.cleanup import (
    _encode_cleanup_cursor as _encode_cleanup_cursor,
)
from modules.chat._public.cleanup import (
    _filter_citation_values as _filter_citation_values,
)
from modules.chat._public.cleanup import (
    _matches_cleanup_scope as _matches_cleanup_scope,
)
from modules.chat._public.cleanup import (
    _scrub_cleanup_event as _scrub_cleanup_event,
)
from modules.chat._public.cleanup import (
    _scrub_cleanup_message as _scrub_cleanup_message,
)
from modules.chat._public.cleanup import (
    _scrub_cleanup_payload as _scrub_cleanup_payload,
)
from modules.chat._public.cleanup import (
    _scrub_cleanup_run as _scrub_cleanup_run,
)
from modules.chat._public.cleanup import (
    _select_cleanup_candidates as _select_cleanup_candidates,
)
from modules.chat._public.cleanup import (
    filter_current_citations as filter_current_citations,
)
from modules.chat._public.cleanup import (
    purge_document_copied_evidence_page as purge_document_copied_evidence_page,
)
from modules.chat._public.conversations import (
    CHAT_SELECTION_LOCAL_ONLY as CHAT_SELECTION_LOCAL_ONLY,
)
from modules.chat._public.conversations import (
    MAX_CHAT_ATTACHMENTS_PER_MESSAGE as MAX_CHAT_ATTACHMENTS_PER_MESSAGE,
)
from modules.chat._public.conversations import (
    _parse_selection_items as _parse_selection_items,
)
from modules.chat._public.conversations import (
    _resolve_gadget_instance_context as _resolve_gadget_instance_context,
)
from modules.chat._public.conversations import (
    _resolve_selected_chunk_ids as _resolve_selected_chunk_ids,
)
from modules.chat._public.conversations import (
    _selection_detail as _selection_detail,
)
from modules.chat._public.conversations import (
    build_answer_context as build_answer_context,
)
from modules.chat._public.conversations import (
    count_source_conversations as count_source_conversations,
)
from modules.chat._public.conversations import (
    delete_conversation as delete_conversation,
)
from modules.chat._public.conversations import (
    purge_unpinned_conversations as purge_unpinned_conversations,
)
from modules.chat._public.conversations import (
    reject_unsendable_selection as reject_unsendable_selection,
)
from modules.chat._public.conversations import (
    resolve_gadget_context as resolve_gadget_context,
)
from modules.chat._public.export import (
    CHAT_EXPORT_MAX_CITATIONS_PER_PAGE as CHAT_EXPORT_MAX_CITATIONS_PER_PAGE,
)
from modules.chat._public.export import (
    CHAT_EXPORT_PAGE_MAX_BYTES as CHAT_EXPORT_PAGE_MAX_BYTES,
)
from modules.chat._public.export import (
    _chat_export_count as _chat_export_count,
)
from modules.chat._public.export import (
    _chat_export_evidence_fences as _chat_export_evidence_fences,
)
from modules.chat._public.export import (
    _chat_export_item_bytes as _chat_export_item_bytes,
)
from modules.chat._public.export import (
    _chat_export_payload_bytes as _chat_export_payload_bytes,
)
from modules.chat._public.export import (
    _chat_export_privacy as _chat_export_privacy,
)
from modules.chat._public.export import (
    _chat_export_privacy_marker as _chat_export_privacy_marker,
)
from modules.chat._public.export import (
    _chat_export_scope as _chat_export_scope,
)
from modules.chat._public.export import (
    _chat_export_scope_retained as _chat_export_scope_retained,
)
from modules.chat._public.export import (
    _decode_chat_export_cursor as _decode_chat_export_cursor,
)
from modules.chat._public.export import (
    _encode_chat_export_cursor as _encode_chat_export_cursor,
)
from modules.chat._public.export import (
    _export_conversation_records as _export_conversation_records,
)
from modules.chat._public.export import (
    _export_message_records as _export_message_records,
)
from modules.chat._public.export import (
    _export_owner_allowed as _export_owner_allowed,
)
from modules.chat._public.export import (
    _parse_export_citations as _parse_export_citations,
)
from modules.chat._public.export import (
    _rebuild_export_citations as _rebuild_export_citations,
)
from modules.chat._public.export import (
    _require_chat_export_owner as _require_chat_export_owner,
)
from modules.chat._public.export import (
    _revalidate_export_citations as _revalidate_export_citations,
)
from modules.chat._public.export import (
    _revalidate_export_rows as _revalidate_export_rows,
)
from modules.chat._public.export import (
    _safe_chat_export_url as _safe_chat_export_url,
)
from modules.chat._public.export import (
    _stream_export_message_candidates as _stream_export_message_candidates,
)
from modules.chat._public.export import (
    export_page as export_page,
)
from modules.chat._public.export import (
    read_memory_export_origin as read_memory_export_origin,
)
from modules.chat._public.export import (
    search_conversations as search_conversations,
)
from modules.chat._public.export import (
    validate_export_fences as validate_export_fences,
)
from modules.chat.citations import (
    validate_answer_citations,
    validate_citations,
)
from modules.chat.generative_ui.skill_backend import load_skill as load_generative_ui_skill
from modules.chat.models import (
    AgentActivityLink,
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
    AgentActivityRead,
    AnswerContext,
    AnswerContextRequest,
    CancelResponse,
    ChatExportCitation,
    ChatExportCitationFence,
    ChatExportConversationRead,
    ChatExportFence,
    ChatExportFenceValidation,
    ChatExportMessageRead,
    ChatExportPage,
    ChatExportWebCitation,
    ChatMemoryExportOrigin,
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
    WebCitation,
)
from modules.chat.seed import ensure_demo_conversation
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
    "AgentActivityLink",
    "AgentActivityRead",
    "AnswerContext",
    "AnswerContextRequest",
    "CancelResponse",
    "ChatExportCitation",
    "ChatExportCitationFence",
    "ChatExportConversationRead",
    "ChatExportFence",
    "ChatExportFenceValidation",
    "ChatExportMessageRead",
    "ChatExportPage",
    "ChatExportWebCitation",
    "ChatMemoryExportOrigin",
    "Citation",
    "CitationValidationResult",
    "Conversation",
    "ConversationCreate",
    "ConversationDetailRead",
    "ConversationPatch",
    "ConversationRead",
    "CopiedEvidenceCleanupProgress",
    "EntityContextItem",
    "EvidenceItem",
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
    "WebCitation",
    "WebSearchError",
    "authorize_agent_run_access",
    "build_answer_context",
    "build_context",
    "count_source_conversations",
    "delete_conversation",
    "ensure_demo_conversation",
    "export_page",
    "external_router",
    "filter_current_citations",
    "filter_live_agent_run_ids",
    "format_grounded_context",
    "format_sse_event",
    "get_agent_activity",
    "has_live_agent_run_link",
    "is_history_storage_enabled",
    "link_agent_run",
    "list_agent_run_ids_for_delete",
    "list_agent_run_ids_for_owner",
    "live_agent_conversation_id",
    "load_generative_ui_skill",
    "make_event_id",
    "parse_event_id",
    "process_chat_response",
    "publish_agent_activity",
    "purge_document_copied_evidence_page",
    "purge_expired_chat_runs",
    "purge_unpinned_conversations",
    "read_memory_export_origin",
    "revalidate_context_fence",
    "run_response_generation",
    "run_web_search",
    "search_conversations",
    "validate_answer_citations",
    "validate_citations",
    "validate_export_fences",
]
