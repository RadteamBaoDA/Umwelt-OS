"""Narrow public facade for knowledge owners consumed by presentation routes."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships import public as relationships
from modules.knowledge.temporal import public as temporal
from modules.timeline import public as timeline


class KnowledgeService:
    """Adapt the public entity and relationship contracts for route consumers."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the request-scoped session used by delegated contract calls."""
        self.session = session

    async def entities(self, *, limit: int, cursor: str | None, entity_type: str | None, query: str | None):
        """List entities through the owning public contract with its page filters."""
        return await entities.list_entities(self.session, limit, cursor, entity_type, query)

    async def entity(self, entity_id: UUID):
        """Resolve one entity through its owning public contract."""
        return await entities.get_entity(self.session, entity_id)

    async def entity_evidence(self, entity_id: UUID, *, limit: int, cursor: str | None):
        """List versioned evidence for an entity through its public contract."""
        return await entities.list_entity_evidence(self.session, entity_id, limit, cursor)

    async def entity_neighbors(self, entity_id: UUID, *, limit: int, cursor: str | None):
        """Read adjacent entities and relationships through the relationship owner."""
        return await relationships.get_neighbors(self.session, entity_id, limit, cursor)

    async def entity_review(self, *, limit: int, cursor: str | None = None):
        """List owner-review candidates using the extraction snapshot contract."""
        return await entities.list_review_candidates(self.session, limit, cursor)

    async def assign_review_candidate(self, candidate_id: UUID, payload, *, actor_id: int):
        """Delegate a review assignment with its authenticated actor identity."""
        return await entities.assign_review_candidate(self.session, candidate_id, payload, actor_id=actor_id)

    async def resolve_relationship_review(self, candidate_id: UUID, payload, *, actor_id: int):
        """Delegate relationship review with its authenticated actor identity."""
        return await entities.resolve_relationship_review(self.session, candidate_id, payload, actor_id=actor_id)

    async def get_events(self, *, limit=50, cursor=None, source_id=None):
        """Delegate canonical event paging; derived graph outage never removes canonical results."""
        return await timeline.list_events(self.session, limit=limit, cursor=cursor, source_id=source_id)

    async def get_timeline(self, query, *, limit=50, cursor=None):
        """Delegate all server filters and precision partitions to the accepted timeline owner."""
        return await timeline.list_timeline(self.session, query, limit=limit, cursor=cursor)

    async def get_entity_timeline(self, entity_id: UUID, query, *, graph_enabled: bool, limit=50, cursor=None):
        """Resolve participant history and authorized graph status using the caller's runtime enablement setting."""
        canonical_id = await entities.resolve_canonical_entity_id(self.session, entity_id)
        selected = query.model_copy(update={"entity_id": canonical_id})
        page = await self.get_timeline(selected, limit=limit, cursor=cursor)
        versions = sorted({UUID(str(item["document_version_id"])) for event in page.items
                           for item in event.evidence if item.get("document_version_id")}, key=str)
        statuses = []
        for offset in range(0, len(versions), 100):
            statuses.extend(await temporal.mapping_statuses(self.session, versions[offset:offset + 100],
                                                            graph_enabled=graph_enabled))
        return {"canonical_entity_id": canonical_id, "timeline": page, "graph_statuses": statuses}

    async def entity_history(self, entity_id: UUID, *, limit=50, cursor=None, membership_cursor=None):
        """Delegate actual owner correction audit and independently paged retained membership history."""
        return await entities.list_entity_history(self.session, entity_id, limit=limit, cursor=cursor,
                                                   membership_cursor=membership_cursor)

    async def find_changes(self, **filters):
        """Delegate bounded recorded canonical mutations with current evidence/deletion checks."""
        return await temporal.find_changes(self.session, **filters)

    async def chat_evidence(
        self, refs: list[tuple[UUID, UUID]], *, require_active_source: bool = True
    ) -> list[documents.ChatEvidenceChunk]:
        """Read bounded detached evidence chunks with exact content and source privacy fence.

        Args:
            refs: Unique list of (document_version_id, chunk_id) pairs, bounded to 100.
            require_active_source: If True, only returns chunks from active sources.

        Returns:
            Ordered list of detached ChatEvidenceChunk DTOs.
        """
        return await documents.read_chat_evidence_chunks(
            self.session, refs, require_active_source=require_active_source
        )

    async def build_answer_context(self, redis, settings, request):
        """Compose grounded retrieval context across search, entities, temporal and documents.

        Args:
            redis: Redis client instance for caching and rate limiting.
            settings: Application settings configuration.
            request: Validated AnswerContextRequest DTO.

        Returns:
            AnswerContext DTO containing deduplicated evidence, summaries, and privacy snapshots.
        """
        from modules.chat import retrieval

        return await retrieval.build_context(self.session, redis, settings, request)

    async def get_memories(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        memory_type: str | None = None,
        status: str = "active",
        query: str | None = None,
    ):
        """Retrieve memories through the public memory service interface.

        Args:
            limit: Maximum items to return (bounded to 100).
            cursor: Opaque cursor for pagination.
            memory_type: Optional filter by memory type ('fact', 'preference', 'instruction').
            status: Status filter, defaults to 'active'.
            query: Optional substring or semantic search query.

        Returns:
            MemoryPage DTO of matching memory items.
        """
        from modules.memory.public import MemoryService

        return await MemoryService(self.session).get_memories(
            limit=limit,
            cursor=cursor,
            memory_type=memory_type,
            status=status,
            query=query,
        )

    async def get_memory_context(self, *, limit: int = 20):
        """Retrieve active memories formatted for prompt or agent context injection.

        Excluded forgotten or invalidated memories. Bounded by privacy settings.

        Args:
            limit: Maximum memory items to return (default 20).

        Returns:
            List of active MemoryRead DTOs.
        """
        from modules.memory.public import MemoryService

        return await MemoryService(self.session).get_active_memory_context(limit=limit)

