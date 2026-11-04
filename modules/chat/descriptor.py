"""Declare chat module ownership, public capabilities, dependencies, and streaming routes."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class ChatDescriptor(ModuleDescriptor):
    """Declare persistent chat conversations, grounded model generation, and SSE event streaming capabilities."""

    id: str = "chat"
    name: str = "Chat"
    version: str = "1.0.0"
    description: str = "Persistent conversations, grounded model answer generation, and replayable SSE streaming."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("knowledge.documents", "knowledge.entities")
    scheduled_jobs: tuple[str, ...] = ("process_chat_response", "recover_chat_runs")
    provides: tuple[str, ...] = ("conversations", "chat_stream", "grounded_answers")
    requires: tuple[str, ...] = ("document_versions", "document_chunks")
    routes: tuple[str, ...] = ("/api/v1/conversations", "/api/v1/responses")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ({"label": "Chat", "href": "/chat"},)
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.chat.routes", 410),
        RouterSpec("modules.chat.media_routes", 420),
        RouterSpec("modules.chat.todos_routes", 430),
        RouterSpec("modules.chat.templates_routes", 440),
    )
    erasure_module: str | None = "modules.chat.erasure"
    erasure_order: int = 30


descriptor = ChatDescriptor()
