"""Register only implemented, scoped, read-only Knowledge, Search and Source tools."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from core.tools.registry import ToolRegistry
from core.tools.schemas import (
    ToolDefinition,
    ToolDestination,
    ToolExecutionPrincipal,
    ToolOutputFence,
    ToolRisk,
)
from core.workspaces.schemas import Scope


@asynccontextmanager
async def _resolve_session(context: dict[str, Any]) -> AsyncIterator[Any]:
    """Yield the request's trusted session or create one from server composition."""
    if context.get("session") is not None:
        yield context["session"]
        return
    factory = context.get("session_factory")
    if factory is None:
        raise RuntimeError("Database context unavailable")
    async with factory() as session:
        yield session


def _principal_scope(
    context: dict[str, Any],
) -> tuple[ToolExecutionPrincipal, frozenset[UUID], bool, Scope, bool]:
    """Extract the registry-injected principal, workspace scope and flag; never trust tool args."""
    principal = context.get("principal")
    if not isinstance(principal, ToolExecutionPrincipal):
        raise PermissionError("Trusted principal required")
    flag = getattr(context.get("settings"), "multi_workspace_enabled", None)
    if type(flag) is not bool:
        raise PermissionError("Trusted workspace configuration required")
    return (
        principal, frozenset(UUID(value) for value in principal.source_ids),
        principal.owner_all_sources, principal.scope, flag,
    )


def _destination(context: dict[str, Any]) -> ToolDestination:
    """Read the server-supplied privacy class, independent of the exact authorization destination ID."""
    kind = context.get("destination_kind")
    if not isinstance(kind, str):
        raise PermissionError("Trusted destination privacy kind required")
    try:
        return ToolDestination(kind)
    except ValueError as exc:
        raise PermissionError("Unknown destination privacy kind") from exc


def _record_output_fences(
    context: dict[str, Any],
    records: list[ToolOutputFence],
    source_generations: dict[UUID, int],
) -> None:
    """Append bounded server-captured result and source identities to the trusted invocation sink.

    Remote invocations must provide the exact server-composed sink. Only the authenticated local
    owner route may omit it for compatibility; client arguments never supply this context value.

    The function merges only consistent original source generations and appends at most 100 typed
    result identities per invocation. It raises PermissionError for missing remote authority,
    malformed state, out-of-scope rows or any bound/identity mismatch.
    """
    principal, source_ids, owner_all, _, _ = _principal_scope(context)
    destination = _destination(context)
    if "output_fence_sink" not in context:
        if destination == ToolDestination.LOCAL and principal.is_owner:
            return
        raise PermissionError("Native output fence sink is required")
    sink = context.get("output_fence_sink")
    if not isinstance(sink, dict) or set(sink) != {"records", "source_generations"}:
        raise PermissionError("Native output fence sink is invalid")
    sink_records = sink.get("records")
    sink_generations = sink.get("source_generations")
    if (
        not isinstance(sink_records, list) or len(sink_records) > 100
        or not isinstance(sink_generations, dict) or len(sink_generations) > 100
        or len(sink_records) + len(records) > 100
        or len(source_generations) > 100
    ):
        raise PermissionError("Native output fence sink exceeds its bound")
    combined_generations = dict(sink_generations)
    for source_id, generation in source_generations.items():
        if (
            not isinstance(source_id, UUID) or type(generation) is not int or generation < 1
            or (not owner_all and source_id not in source_ids)
            or (source_id in combined_generations and combined_generations[source_id] != generation)
        ):
            raise PermissionError("Native source generation fence is invalid")
        combined_generations[source_id] = generation
    if len(combined_generations) > 100:
        raise PermissionError("Native source fence scope exceeds its bound")
    for fence in records:
        if (
            not isinstance(fence, ToolOutputFence)
            or not isinstance(fence.document_id, UUID)
            or not isinstance(fence.document_version_id, UUID)
            or not isinstance(fence.source_id, UUID)
            or type(fence.source_generation) is not int or fence.source_generation < 1
            or (fence.chunk_id is not None and not isinstance(fence.chunk_id, UUID))
            or (not owner_all and fence.source_id not in source_ids)
            or combined_generations.get(fence.source_id) != fence.source_generation
        ):
            raise PermissionError("Native result fence is invalid")
    if any(
        not isinstance(source_id, UUID) or type(generation) is not int or generation < 1
        for source_id, generation in combined_generations.items()
    ) or any(not isinstance(fence, ToolOutputFence) for fence in sink_records):
        raise PermissionError("Native output fence sink contains invalid records")
    sink_generations.update(source_generations)
    sink_records.extend(records)


async def _handle_knowledge_get_document(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch current ready metadata and capture its exact DTO identity before making the payload.

    Authorization comes from the registry principal and server destination. A null result has no
    row fence, but remote invocations must still provide the server-owned result sink.
    """
    from modules.knowledge.documents import public
    _, source_ids, owner_all, scope, flag = _principal_scope(context)
    async with _resolve_session(context) as session:
        item = await public.get_tool_document(
            session, UUID(args["document_id"]), source_ids=source_ids,
            owner_all=owner_all, destination=_destination(context), scope=scope, multi_workspace_enabled=flag,
        )
    if item is None:
        _record_output_fences(context, [], {})
        return None
    _record_output_fences(
        context,
        [ToolOutputFence(
            document_id=item.id, document_version_id=item.document_version_id,
            source_id=item.source_id, source_generation=item.source_generation,
        )],
        {item.source_id: item.source_generation},
    )
    return {
        "id": item.id, "document_version_id": item.document_version_id,
        "source_id": item.source_id, "title": item.title,
        "content_type": item.content_type, "version_number": item.version_number,
        "created_at": item.created_at,
    }


async def _handle_knowledge_list_documents(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """List bounded current documents and capture every page identity before adding its cursor.

    Source scope and destination are server-derived; a single changed row invalidates the whole
    page at the sender, so the continuation cursor is never separated from its captured rows.
    """
    from modules.knowledge.documents import public
    _, source_ids, owner_all, scope, flag = _principal_scope(context)
    async with _resolve_session(context) as session:
        page = await public.list_tool_documents(
            session, limit=args.get("limit", 20), cursor=args.get("cursor"),
            source_ids=source_ids, owner_all=owner_all, destination=_destination(context),
            scope=scope, multi_workspace_enabled=flag,
        )
    _record_output_fences(
        context,
        [ToolOutputFence(
            document_id=item.id, document_version_id=item.document_version_id,
            source_id=item.source_id, source_generation=item.source_generation,
        ) for item in page.items],
        {item.source_id: item.source_generation for item in page.items},
    )
    items = [{
        "id": item.id, "document_version_id": item.document_version_id,
        "source_id": item.source_id, "title": item.title,
        "content_type": item.content_type, "version_number": item.version_number,
        "created_at": item.created_at,
    } for item in page.items]
    return {"items": items, "next_cursor": page.next_cursor}


async def _handle_search_query(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Search the trusted source intersection and capture pre-await generations plus exact hits.

    The shared server sink includes the searched source scope even for no-hit/cursor results;
    returned chunk identities use those original generations without refreshing or replacing them.
    Agent callers may supply a trusted per-attempt callback to revalidate cancellation, owner
    authorization, embedding privacy, and this original source snapshot immediately before send.
    """
    from modules.search import public
    from modules.search.schemas import SearchFilters, SearchRequest
    _, source_ids, owner_all, scope, flag = _principal_scope(context)
    requested = frozenset(UUID(value) for value in args.get("source_ids", []))
    destination = _destination(context)
    source_generations: dict[UUID, int] = {}
    async with _resolve_session(context) as session:
        from modules.sources import public as sources
        allowed_set: set[UUID] = set()
        if owner_all and not requested:
            page = await sources.list_tool_sources(
                session, limit=100, cursor=None, source_ids=frozenset(), owner_all=True,
                destination=destination, scope=scope, multi_workspace_enabled=flag,
            )
            if page.next_cursor is not None:
                raise PermissionError("Owner source scope exceeds the bounded search fence")
            for source in page.items:
                allowed_set.add(source.id)
                source_generations[source.id] = source.generation
        else:
            candidates = requested if owner_all else (requested.intersection(source_ids) if requested else source_ids)
            if len(candidates) > 100:
                raise PermissionError("Search source scope exceeds the bounded fence")
            for source_id in candidates:
                candidate_source = await sources.get_tool_source(
                    session, source_id, source_ids=source_ids, owner_all=owner_all,
                    destination=destination, scope=scope, multi_workspace_enabled=flag,
                )
                if candidate_source is not None:
                    allowed_set.add(source_id)
                    source_generations[source_id] = candidate_source.generation
        allowed = frozenset(allowed_set)
        if not allowed:
            _record_output_fences(context, [], source_generations)
            return {"items": [], "next_cursor": None, "effective_mode": "lexical", "warnings": []}
    if destination != ToolDestination.LOCAL:  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
        # Remote search is fail-closed unless every queried source has a pre-await generation.
        if set(source_generations) != set(allowed):
            _record_output_fences(context, [], source_generations)
            return {"items": [], "next_cursor": None, "effective_mode": "lexical", "warnings": []}
    request = SearchRequest(
        query=args["query"], limit=args.get("limit", 10), mode=args.get("mode", "lexical"),
        cursor=args.get("cursor"), filters=SearchFilters(source_ids=sorted(allowed, key=str)),
    )
    if not allowed:
        return {"items": [], "next_cursor": None, "effective_mode": "lexical", "warnings": []}
    async with _resolve_session(context) as session:
        result = await public.search(
            session, context["redis"], context["settings"], request,
            destination=destination, source_generation_fences=source_generations,
            before_embedding_send=context.get("before_embedding_send"), scope=scope, multi_workspace_enabled=flag,
            release_during_embed=context.get("session") is None,
        )
    _record_output_fences(
        context,
        [ToolOutputFence(
            document_id=item.document_id, document_version_id=item.document_version_id,
            source_id=item.source.id, source_generation=source_generations[item.source.id],
            chunk_id=item.chunk_id,
        ) for item in result.items],
        source_generations,
    )
    return result.model_dump(mode="json")


async def _handle_sources_list_sources(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """List active detached source identities within exact server-authorized source scope."""
    from modules.sources import public
    _, source_ids, owner_all, scope, flag = _principal_scope(context)
    async with _resolve_session(context) as session:
        page = await public.list_tool_sources(
            session, limit=args.get("limit", 20), cursor=args.get("cursor"),
            source_ids=source_ids, owner_all=owner_all, destination=_destination(context),
            scope=scope, multi_workspace_enabled=flag,
        )
    items = [item.__dict__ for item in page.items]
    return {"items": items, "next_cursor": page.next_cursor}


async def _handle_sources_get_source(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    """Read an active detached source identity only when its exact ID is granted."""
    from modules.sources import public
    _, source_ids, owner_all, scope, flag = _principal_scope(context)
    async with _resolve_session(context) as session:
        item = await public.get_tool_source(
            session, UUID(args["source_id"]), source_ids=source_ids,
            owner_all=owner_all, destination=_destination(context), scope=scope, multi_workspace_enabled=flag,
        )
    return item.__dict__ if item else None


async def _handle_github_list_project_events(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """List canonical GitHub issue, pull, commit and release events for one granted github source.

    Scope and local-only privacy come from the registry principal and server destination via
    ``get_tool_source``; events are read through the Timeline public API and every evidence
    identity is captured as an output fence at the source's current generation.
    """
    from modules.sources import public as sources_public
    from modules.timeline import public as timeline_public
    from modules.timeline.schemas import TimelineQuery
    _, source_ids, owner_all, scope, flag = _principal_scope(context)
    source_id = UUID(args["source_id"])
    async with _resolve_session(context) as session:
        source = await sources_public.get_tool_source(
            session, source_id, source_ids=source_ids, owner_all=owner_all,
            destination=_destination(context), scope=scope, multi_workspace_enabled=flag,
        )
        detached = (
            await sources_public.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=flag) if source else None
        )
        if source is None or detached is None or detached.provider != "github":
            _record_output_fences(context, [], {})
            return {"items": [], "next_cursor": None}
        page = await timeline_public.list_timeline(
            session, TimelineQuery(source_id=source_id, type="github_"),
            limit=args.get("limit", 20), cursor=args.get("cursor"), scope=scope, multi_workspace_enabled=flag,
        )
    # Document-level fences (chunk_id None) are revalidated by the Documents owner, like
    # knowledge.get_document; one per document, so an edit that selects a new version denies the result.
    # Evidence accumulates one row per version; keep only the highest (current) version per
    # document in both the fence and the returned evidence so revalidation passes after edits.
    current: dict[UUID, dict[str, Any]] = {}
    for event in page.items:
        for ref in event.evidence:
            known = current.get(ref["document_id"])
            if known is None or ref["version_number"] > known["version_number"]:
                current[ref["document_id"]] = ref
    unique = current
    fences = [
        ToolOutputFence(
            document_id=ref["document_id"], document_version_id=ref["document_version_id"],
            source_id=source.id, source_generation=source.generation,
        ) for ref in unique.values()
    ][:100]
    _record_output_fences(context, fences, {source.id: source.generation})
    items = [{
        "id": event.id, "type": event.type, "title": event.title, "started_at": event.started_at,
        "metadata": event.metadata,
        "evidence": [{"document_id": ref["document_id"], "document_version_id": ref["document_version_id"],
                      "canonical_url": ref["canonical_url"]}
                     for ref in event.evidence
                     if current[ref["document_id"]]["document_version_id"] == ref["document_version_id"]],
    } for event in page.items]
    return {"items": items, "next_cursor": page.next_cursor}


def register_builtin_tools(registry: ToolRegistry, allowed_names: frozenset[str] | None = None) -> None:
    """Register implemented read tools contributed by enabled descriptors.

    Args:
        registry: The canonical app-scoped core registry.
        allowed_names: Server-composed descriptor contribution intersection; None is reserved
            for explicit composition that accepts all declarations.
    Only read actions are registered here; source creation and effectful actions await their
    owner-specific authorization and durable approval contracts.
    """
    definitions = (
        ("knowledge.get_document", "knowledge.documents", _handle_knowledge_get_document,
         {"type": "object", "properties": {"document_id": {"type": "string", "format": "uuid"}}, "required": ["document_id"], "additionalProperties": False},
         {"type": ["object", "null"]}),
        ("knowledge.list_documents", "knowledge.documents", _handle_knowledge_list_documents,
         {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": {"type": "string", "maxLength": 256}}, "additionalProperties": False},
         {"type": "object", "required": ["items", "next_cursor"], "properties": {"items": {"type": "array", "maxItems": 100}, "next_cursor": {"type": ["string", "null"]}}, "additionalProperties": False}),
        ("search.query", "search", _handle_search_query,
         {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 1000}, "source_ids": {"type": "array", "maxItems": 100, "items": {"type": "string", "format": "uuid"}}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "mode": {"enum": ["lexical", "hybrid"]}, "cursor": {"type": "string", "maxLength": 256}}, "required": ["query"], "additionalProperties": False},
         {"type": "object", "required": ["items", "next_cursor", "effective_mode", "warnings"], "properties": {"items": {"type": "array", "maxItems": 100}, "next_cursor": {"type": ["string", "null"]}, "effective_mode": {"enum": ["lexical", "hybrid"]}, "warnings": {"type": "array", "maxItems": 20}}, "additionalProperties": False}),
        ("sources.list_sources", "sources", _handle_sources_list_sources,
         {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": {"type": "string", "maxLength": 256}}, "additionalProperties": False},
         {"type": "object", "required": ["items", "next_cursor"], "properties": {"items": {"type": "array", "maxItems": 100}, "next_cursor": {"type": ["string", "null"]}}, "additionalProperties": False}),
        ("sources.get_source", "sources", _handle_sources_get_source,
         {"type": "object", "properties": {"source_id": {"type": "string", "format": "uuid"}}, "required": ["source_id"], "additionalProperties": False},
         {"type": ["object", "null"]}),
        ("github.list_project_events", "tools", _handle_github_list_project_events,
         {"type": "object", "properties": {"source_id": {"type": "string", "format": "uuid"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}, "cursor": {"type": "string", "maxLength": 256}}, "required": ["source_id"], "additionalProperties": False},
         {"type": "object", "required": ["items", "next_cursor"], "properties": {"items": {"type": "array", "maxItems": 100}, "next_cursor": {"type": ["string", "null"]}}, "additionalProperties": False}),
    )
    for name, module, handler, input_schema, output_schema in definitions:
        if allowed_names is not None and name not in allowed_names:
            continue
        registry.register_tool(ToolDefinition(
            name=name, version="1", description=f"Registered read action {name}.",
            input_schema=input_schema, output_schema=output_schema, risk=ToolRisk.READ_ONLY,
            timeout_seconds=20, permissions=("source.read",), module=module,
        ), handler)
