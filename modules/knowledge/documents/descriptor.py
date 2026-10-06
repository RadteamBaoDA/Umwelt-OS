from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentDescriptor:
    """Declare document storage's source dependency and public interfaces."""

    id: str = "knowledge.documents"
    name: str = "Documents"
    version: str = "1.0.0"
    description: str = "Store source-backed documents and immutable revisions."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("sources",)
    scheduled_jobs: tuple[str, ...] = ("process_document_ready", "process_document_cleanup")
    provides: tuple[str, ...] = ("documents", "document_versions", "document_chunks")
    requires: tuple[str, ...] = ("sources",)
    routes: tuple[str, ...] = (
        "/api/v1/documents",
        "/api/v1/documents/upload",
        "/api/v1/documents/{id}/raw",
        "/api/v1/documents/deletion-operations/{operation_id}",
        "/api/v1/documents/dashboard-projections",
        "/api/v1/documents/{document_id}/versions/{version_number}/interaction",
    )
    emitted_events: tuple[str, ...] = ("document.cleanup.requested",)
    consumed_events: tuple[str, ...] = ("document.cleanup.requested",)
    tools: tuple[str, ...] = ("knowledge.get_document", "knowledge.list_documents")
    navigation: tuple[dict[str, str], ...] = (
        {"label": "Documents", "href": "/knowledge/documents"},
    )
    settings_schema: dict[str, object] | None = None


descriptor = DocumentDescriptor()
