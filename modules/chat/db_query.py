"""Structured & Hybrid Safe Text-to-SQL database query tool for Chat over collected datasources.

Enables the chat model to query collected ``observations`` and ``documents`` in PostgreSQL
(and Redis hot snapshots) without requiring vector embeddings (``pgvector``). Enforces workspace
isolation, ``AccessFence`` verification, ``local_only`` egress protection, a 2000ms statement
timeout, and automatic ``EvidenceItem`` registration for grounded citations.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from core.config import Settings
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, Scope
from modules.chat.schemas import GADGET_SCOPE_KEY, EvidenceItem
from modules.chat.sql_guard import check_select_policy
from modules.knowledge.documents.public import (
    MEASUREMENT_PROVIDER_IDS,
)
from modules.knowledge.documents.public import (
    DocumentChunkReadProjection as DocumentChunk,
)
from modules.knowledge.documents.public import (
    DocumentReadProjection as Document,
)
from modules.knowledge.documents.public import (
    DocumentVersionReadProjection as DocumentVersion,
)
from modules.knowledge.observations.public import ObservationReadProjection as Observation
from modules.sources.public import SourceReadProjection as Source

TOOL_QUERY_COLLECTED_DATA = "query_collected_data"
MAX_DB_QUERY_ROWS = 50
MAX_SQL_LENGTH = 2000


class QueryCollectedDataArgs(BaseModel):
    """Validated input schema for the ``query_collected_data`` chat tool."""

    model_config = ConfigDict(extra="ignore")

    mode: Literal["structured", "sql"] = "structured"
    target: Literal["observations", "documents", "both"] = "both"
    providers: list[str] | None = Field(default=None, max_length=16)
    metrics: list[str] | None = Field(default=None, max_length=16)
    symbols: list[str] | None = Field(default=None, max_length=16)
    region: str | None = Field(default=None, max_length=80)
    text_query: str | None = Field(default=None, max_length=200)
    date_from: str | None = Field(default=None, max_length=64)
    date_to: str | None = Field(default=None, max_length=64)
    current_only: bool = True
    aggregation: Literal["none", "latest", "avg", "min", "max", "sum", "count", "timeseries"] = (
        "none"
    )
    sql_query: str | None = Field(default=None, max_length=MAX_SQL_LENGTH)
    limit: int = Field(default=25, ge=1, le=MAX_DB_QUERY_ROWS)


def build_db_query_tool_spec() -> dict[str, Any]:
    """Return the OpenAI-compatible function tool specification for ``query_collected_data``.

    Returns:
        Tool specification dictionary for the model gateway.
    """
    return {
        "type": "function",
        "function": {
            "name": TOOL_QUERY_COLLECTED_DATA,
            "description": (
                "Query the workspace's collected realtime and near-realtime datasource database "
                "(PostgreSQL observations & documents + Redis hot cache). "
                "Supports 'structured' filter/aggregation mode and 'sql' read-only SELECT mode "
                "against the pre-scoped views `scoped_observations` "
                "(columns: id, source_id, provider, metric, symbol, region, value, unit, currency, "
                "observed_at, published_at, quality, is_current, document_id, document_version_id) "
                "and `scoped_documents` "
                "(columns: id, source_id, provider, title, canonical_url, published_at, observed_at, "
                "language, content_excerpt, document_version_id). "
                "Measurement providers (prices, rates, weather, macro series) store numeric rows in "
                "observations. News and event feeds (earthquake, alert and news providers) store "
                "documents only, so use target='documents' or 'both' for them; observations are empty "
                "for those providers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["structured", "sql"],
                        "description": "Use 'structured' for parameterized filters/aggregations or 'sql' for custom SELECT queries over scoped_observations / scoped_documents.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["observations", "documents", "both"],
                        "description": "Which collected tables to query in structured mode. Event and news feeds only have documents; documents are added automatically when a requested provider is not a measurement provider.",
                    },
                    "providers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional provider IDs (e.g. ['binance', 'world_bank', 'emsc_quakes', 'vnexpress_business']).",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional metric names for measurement providers (e.g. ['price', 'temperature_2m']).",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional market/asset symbols (e.g. ['BTCUSDT', 'ETHUSDT', 'USD']).",
                    },
                    "region": {
                        "type": "string",
                        "description": "Optional region or country code filter (e.g. 'VN', 'US', 'GLOBAL').",
                    },
                    "text_query": {
                        "type": "string",
                        "description": "Optional keyword/full-text search query over collected document titles and content.",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Optional ISO-8601 start timestamp or YYYY-MM-DD date.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Optional ISO-8601 end timestamp or YYYY-MM-DD date.",
                    },
                    "current_only": {
                        "type": "boolean",
                        "description": "When true (default), filter observations to is_current=true unless aggregation is timeseries.",
                    },
                    "aggregation": {
                        "type": "string",
                        "enum": [
                            "none",
                            "latest",
                            "avg",
                            "min",
                            "max",
                            "sum",
                            "count",
                            "timeseries",
                        ],
                        "description": "Optional SQL aggregation over observation values grouped by provider, metric, symbol, unit.",
                    },
                    "sql_query": {
                        "type": "string",
                        "description": (
                            "Read-only SELECT query when mode='sql'. Must query ONLY `scoped_observations` "
                            "and/or `scoped_documents` (no semicolons, comments, or DDL/DML)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum rows to return (1-50, default 25).",
                    },
                },
            },
        },
    }


def is_gadget_turn(retrieval_context: dict[str, Any] | None) -> bool:
    """True when the turn was opened from a Dashboard gadget (the gadget path always gets the tool).

    The legacy ``DB_QUERY_KEY`` request flag is deliberately ignored: the tool no longer has a user switch.
    """
    ctx = retrieval_context or {}
    return ctx.get("kind") == "gadget" or isinstance(ctx.get(GADGET_SCOPE_KEY), dict)


async def has_collected_datasource(
    session: AsyncSession, *, workspace_id: UUID, allow_local_only: bool
) -> bool:
    """Whether the workspace has at least one active connector-collected source this turn may query.

    This is the grant condition for ``query_collected_data``: the owner either has collected data to
    query or the tool would only waste a round. Read-only, one bounded ``EXISTS`` query; ``local_only``
    sources count only when the turn may use them (same rule as ``_allowed_sources``).
    """
    stmt = select(Source.id).where(
        Source.workspace_id == workspace_id,
        Source.status == "active",
        Source.provider.is_not(None),
    )
    if not allow_local_only:
        stmt = stmt.where(Source.local_only.is_(False))
    return (await session.execute(stmt.limit(1))).first() is not None


def validate_readonly_sql(sql: str) -> str:
    """Validate a model-generated SQL string so only a read-only SELECT over the scoped views can run.

    Layers applied here: length bound, comment/semicolon ban (keeps the ``SELECT * FROM (<sql>)``
    wrapper intact), then the ``pglast`` default-deny allowlist in :mod:`modules.chat.sql_guard`. The
    former keyword regex was removed on purpose: the parser rejects every relation/function it guarded
    (and the ones it missed, e.g. ``chat_messages``, ``query_to_xml``) while the regex also refused
    legitimate literals such as ``WHERE symbol = 'users'``. Execution adds two more layers (see
    ``_execute_sql_mode``): a least-privilege role and a read-only transaction.

    Args:
        sql: Raw SQL string supplied by the model in ``mode='sql'``.

    Returns:
        Cleaned single-statement SELECT query string without trailing semicolon.

    Raises:
        ValueError: If the SQL is empty/too long, has comments or semicolons, or violates the allowlist.
    """
    cleaned = (sql or "").strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if not cleaned or len(cleaned) > MAX_SQL_LENGTH:
        raise ValueError("SQL query must be between 1 and 2000 characters")
    if ";" in cleaned or "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
        raise ValueError("SQL query must be a single statement without comments or semicolons")
    check_select_policy(cleaned)
    return cleaned


def _parse_iso_dt(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 date or timestamp into a timezone-aware UTC datetime, or None."""
    if not raw or not isinstance(raw, str):
        return None
    text_val = raw.strip()
    if not text_val:
        return None
    try:
        if len(text_val) == 10 and text_val[4] == "-" and text_val[7] == "-":
            dt = datetime.fromisoformat(f"{text_val}T00:00:00+00:00")
        else:
            dt = datetime.fromisoformat(text_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


async def _allowed_sources(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    source_scope: list[str] | None,
    allow_local_only: bool,
) -> list[tuple[UUID, str | None, str]]:
    """Return ``(id, provider, name)`` for active workspace sources permitted in this chat turn."""
    stmt = select(
        Source.id,
        Source.provider,
        Source.name,
    ).where(
        Source.workspace_id == workspace_id,
        Source.status == "active",
    )
    if not allow_local_only:
        stmt = stmt.where(Source.local_only.is_(False))
    if source_scope:
        scoped_ids: list[UUID] = []
        for raw_id in source_scope[:32]:
            with suppress(ValueError, TypeError):
                scoped_ids.append(UUID(str(raw_id)))
        if scoped_ids:
            stmt = stmt.where(Source.id.in_(scoped_ids))
    rows = (await session.execute(stmt.order_by(Source.created_at.desc()).limit(100))).all()
    return [(row.id, row.provider, row.name) for row in rows]


def _citation_number(index: int, web_numbering: tuple[int, int] | None) -> int:
    """1-based model-facing citation number of ``evidence[index]``.

    ``web_numbering`` is ``(base, web_count)``: the prompt numbered evidence ``1..base`` and then the web
    results, so evidence appended later (by tools) is shifted behind the web results.
    """
    if web_numbering is not None and index >= web_numbering[0]:
        return index + 1 + web_numbering[1]
    return index + 1


async def _register_evidence_for_versions(
    session: AsyncSession,
    version_pairs: list[tuple[UUID, UUID]],
    existing_evidence: list[EvidenceItem],
    web_numbering: tuple[int, int] | None = None,
) -> dict[UUID, int]:
    """Load chunk_index=0 for returned document versions and append new ``EvidenceItem``s.

    Args:
        session: Active asynchronous database session.
        version_pairs: Pairs of ``(document_id, document_version_id)`` from query rows.
        existing_evidence: Mutable list of ``EvidenceItem``s on the current ``AnswerContext``.

    Returns:
        Mapping of ``document_version_id`` to the citation number ``[n]`` the model must use; equal to the
        1-based index in ``existing_evidence`` unless ``web_numbering`` shifts it behind the web results.
    """
    by_version: dict[UUID, int] = {
        item.document_version_id: _citation_number(idx, web_numbering)
        for idx, item in enumerate(existing_evidence)
    }
    needed = [ver_id for _doc_id, ver_id in version_pairs if ver_id not in by_version][:12]
    if not needed:
        return by_version

    rows = (
        await session.execute(
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.content.label("content"),
                DocumentVersion.id.label("document_version_id"),
                DocumentVersion.version_number.label("version_number"),
                DocumentVersion.observed_at.label("observed_at"),
                Document.published_at.label("published_at"),
                Document.id.label("document_id"),
                Document.source_id.label("source_id"),
                Document.title.label("title"),
                Document.canonical_url.label("url"),
                Source.type.label("source_type"),
                Source.generation.label("source_generation"),
                Source.local_only.label("local_only"),
            )
            .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(
                DocumentVersion.id.in_(needed),
                DocumentChunk.chunk_index == 0,
            )
            .limit(12)
        )
    ).all()
    for row in rows:
        if row.document_version_id in by_version:
            continue
        existing_evidence.append(
            EvidenceItem(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                document_version_id=row.document_version_id,
                version_number=int(row.version_number or 1),
                source_id=row.source_id,
                source_type=str(row.source_type or "document"),
                source_generation=int(row.source_generation or 1),
                local_only=bool(row.local_only),
                title=row.title or "Collected record",
                canonical_url=row.url,
                observed_at=row.observed_at,
                published_at=row.published_at,
                content=(row.content or "")[:2000],
                score=1.0,
            )
        )
        by_version[row.document_version_id] = _citation_number(
            len(existing_evidence) - 1, web_numbering
        )
    return by_version


async def execute_db_query_tool(
    session: AsyncSession,
    raw_arguments: dict[str, Any],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence,
    retrieval_context: dict[str, Any] | None,
    allow_local_only: bool,
    evidence_list: list[EvidenceItem],
    redis: Any = None,
    web_numbering: tuple[int, int] | None = None,
) -> str:
    """Execute a validated read-only database query for Chat and register grounded evidence refs.

    Args:
        session: Short-lived read-only database session.
        raw_arguments: Raw JSON arguments from the LLM tool call.
        scope: Admitted workspace scope.
        multi_workspace_enabled: Multi-workspace feature flag.
        expected_access_fence: Captured access fence for the response run.
        retrieval_context: Stored retrieval context on the response run.
        allow_local_only: Whether local-only sources may be queried (False for remote models).
        evidence_list: Mutable ``AnswerContext.evidence`` list to append discovered ``EvidenceItem``s.
        redis: Optional Redis client to read hot connector snapshots.
        web_numbering: ``(base, web_count)`` when web results already occupy citation numbers
            ``base+1..base+web_count``; new evidence is numbered after them.

    Returns:
        Compact JSON string summarizing the query results and their ``[n]`` citation indices.
    """
    current_fence = await workspaces.read_access_fence(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_fence != expected_access_fence:
        return json.dumps({"error": "workspace_access_changed", "rows": []})

    try:
        args = QueryCollectedDataArgs.model_validate(raw_arguments or {})
    except Exception as exc:  # noqa: BLE001  # tool argument validation failure is reported to the model as invalid_arguments
        return json.dumps({"error": f"invalid_arguments: {type(exc).__name__}", "rows": []})

    ctx = retrieval_context or {}
    source_scope = ctx.get("source_scope") if isinstance(ctx.get("source_scope"), list) else None
    allowed = await _allowed_sources(
        session,
        workspace_id=scope.workspace_id,
        source_scope=source_scope,
        allow_local_only=allow_local_only,
    )
    if not allowed:
        return json.dumps({"status": "no_active_sources_in_scope", "rows": []})

    allowed_ids = [sid for sid, _prov, _name in allowed]
    date_from = _parse_iso_dt(args.date_from)
    date_to = _parse_iso_dt(args.date_to)

    try:
        async with session.begin_nested():
            await session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            if args.mode == "sql" and args.sql_query:
                return await _execute_sql_mode(
                    session,
                    args.sql_query,
                    workspace_id=scope.workspace_id,
                    allowed_source_ids=allowed_ids,
                    limit=args.limit,
                    evidence_list=evidence_list,
                    web_numbering=web_numbering,
                )
            return await _execute_structured_mode(
                session,
                args,
                workspace_id=scope.workspace_id,
                allowed_source_ids=allowed_ids,
                date_from=date_from,
                date_to=date_to,
                evidence_list=evidence_list,
                redis=redis,
                web_numbering=web_numbering,
                available_providers=sorted({str(p) for _i, p, _n in allowed if p}),
            )
    except ValueError as exc:
        return json.dumps({"error": str(exc), "rows": []})
    except Exception as exc:  # noqa: BLE001  # any query failure is returned as a sanitized tool error (exception type only)
        return json.dumps({"error": f"query_execution_failed: {type(exc).__name__}", "rows": []})


CHAT_SQL_LOGIN_ROLE = "umwelt_chat_sql_login"
_READER_POOL_SIZE = 2
_READER_STATEMENT_TIMEOUT_MS = 2000
_reader_engines: dict[str, AsyncEngine] = {}


class ChatSqlReaderUnavailableError(ValueError):
    """Raised when ``CHAT_SQL_READER_DATABASE_URL`` is unset, so SQL mode is disabled (reported to the model)."""


def _reader_engine() -> AsyncEngine:
    """Return the lazily created, bounded engine that logs in directly as ``umwelt_chat_sql_login``.

    The URL comes only from ``CHAT_SQL_READER_DATABASE_URL``; there is deliberately no fallback to the
    application connection, because that would reintroduce a ``RESET ROLE`` path to the app user.
    The engine (and so the URL) is cached for the process lifetime (small pool, no overflow, short pool timeout).

    Raises:
        ChatSqlReaderUnavailableError: the setting is empty.
    """
    if (
        _reader_engines
    ):  # settings are read once per process: the URL is fixed after the first engine is built
        return next(iter(_reader_engines.values()))
    url = Settings().chat_sql_reader_database_url
    if not url:  # not cached, so setting the URL later enables the mode; unset stays fail-closed
        raise ChatSqlReaderUnavailableError(
            "sql mode is disabled: CHAT_SQL_READER_DATABASE_URL is not configured"
        )
    engine = _reader_engines.get(url)
    if engine is None:
        engine = _reader_engines[url] = create_async_engine(
            url,
            pool_size=_READER_POOL_SIZE,
            max_overflow=0,
            pool_timeout=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"server_settings": {"default_transaction_read_only": "on"}},
        )
    return engine


_FENCE_TTL_SECONDS = 10
_UPSERT_FENCE = text(
    "INSERT INTO chat_sql.query_fence (backend_pid, workspace_id, source_ids, expires_at) "
    "VALUES (:pid, :ws, CAST(:src AS uuid[]), clock_timestamp() + make_interval(secs => :ttl)) "
    "ON CONFLICT (backend_pid) DO UPDATE SET workspace_id = EXCLUDED.workspace_id, "
    "source_ids = EXCLUDED.source_ids, expires_at = EXCLUDED.expires_at"
)
_DELETE_FENCE = text("DELETE FROM chat_sql.query_fence WHERE backend_pid = :pid")


async def _run_sql_as_reader(
    clean_sql: str,
    *,
    app_engine: AsyncEngine,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    max_rows: int,
) -> list[Any]:
    """Execute validated model SQL on the dedicated read-only login connection; return fetched row mappings.

    The connection is a separate pool logged in as ``umwelt_chat_sql_login`` (no role membership, no table
    privileges, read-only by default), so the database enforces read-only access to the two ``chat_sql``
    scoped views even if the parser allowlist were bypassed: there is no ``SET ROLE`` path to the app user.
    The views are fenced by ``chat_sql.query_fence``, which the reader cannot touch: ordering is reader
    ``pg_backend_pid()``; then, on a short separate ``app_engine`` transaction that is COMMITTED, an upsert of
    the fence row (10 s expiry) for that pid; then ``SET TRANSACTION READ ONLY``, the statement timeout and
    ``SELECT * FROM (<sql>) LIMIT :max_rows`` on the reader; finally the reader transaction is rolled back and
    the fence row deleted via ``app_engine`` (even on error; the expiry covers a crash). The fence key is the
    reader backend pid, and a reader connection serves one query at a time, so concurrent queries on
    different pooled connections never share a row.

    Raises:
        ChatSqlReaderUnavailableError: the reader DSN is not configured (checked before any I/O).
        TypeError: ``workspace_id``/source ids are not ``uuid.UUID`` (a plain ``str`` is refused so no free
            text ever becomes a fence value).
    """
    if not isinstance(workspace_id, UUID) or not all(
        isinstance(sid, UUID) for sid in allowed_source_ids
    ):
        raise TypeError("workspace_id and every allowed source id must be uuid.UUID instances")
    engine = _reader_engine()
    async with engine.connect() as conn:
        pid: int | None = None
        try:
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            async with (
                app_engine.begin() as app_conn
            ):  # own transaction: the fence must be committed before the reader runs
                await app_conn.execute(
                    _UPSERT_FENCE,
                    {
                        "pid": pid,
                        "ws": workspace_id,
                        "src": list(allowed_source_ids),
                        "ttl": _FENCE_TTL_SECONDS,
                    },
                )
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(
                text(f"SET LOCAL statement_timeout = {_READER_STATEMENT_TIMEOUT_MS}")
            )
            res = await conn.execute(
                text("SELECT * FROM (" + clean_sql + ") AS _bounded_query LIMIT :max_rows"),  # noqa: S608  # clean_sql passed the read-only single-statement guard and runs as the read-only login role
                {"max_rows": max_rows},
            )
            return list(res.mappings().all())
        finally:
            await (
                conn.rollback()
            )  # read-only work: discard the transaction so the pooled connection is clean
            if pid is not None:
                async with app_engine.begin() as app_conn:
                    await app_conn.execute(_DELETE_FENCE, {"pid": pid})


async def _execute_sql_mode(
    session: AsyncSession,
    sql_query: str,
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    limit: int,
    evidence_list: list[EvidenceItem],
    web_numbering: tuple[int, int] | None = None,
) -> str:
    """Validate, run (least-privilege, read-only) and serialise a model-written SELECT.

    Raises:
        ValueError: when the SQL fails the parser allowlist (reported to the model, nothing executed).
    """
    clean_sql = validate_readonly_sql(sql_query)
    mappings = await _run_sql_as_reader(
        clean_sql,
        app_engine=cast(
            AsyncEngine, session.bind
        ),  # separate short app-role transaction; never the caller's
        workspace_id=workspace_id,
        allowed_source_ids=allowed_source_ids,
        max_rows=min(limit, MAX_DB_QUERY_ROWS),
    )
    version_pairs: list[tuple[UUID, UUID]] = []
    for row in mappings:
        doc_id = row.get("document_id") or row.get("id")
        ver_id = row.get("document_version_id")
        if isinstance(doc_id, UUID) and isinstance(ver_id, UUID):
            version_pairs.append((doc_id, ver_id))
    citation_map = await _register_evidence_for_versions(
        session, version_pairs, evidence_list, web_numbering
    )

    serialized_rows: list[dict[str, Any]] = []
    for row in mappings:
        item: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, datetime):
                item[k] = v.isoformat()
            elif isinstance(v, UUID):
                item[k] = str(v)
            else:
                item[k] = v
        ver_id = row.get("document_version_id")
        if isinstance(ver_id, UUID) and ver_id in citation_map:
            item["citation"] = f"[{citation_map[ver_id]}]"
        serialized_rows.append(item)

    return json.dumps(
        {
            "mode": "sql",
            "row_count": len(serialized_rows),
            "rows": serialized_rows,
        },
        ensure_ascii=False,
        default=str,
    )


def _observation_filters(
    args: QueryCollectedDataArgs,
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[Any]:
    """Build the workspace/source-scoped SQLAlchemy filter list for observation queries.

    Pure: builds expressions only, no I/O. The workspace and allowed-source predicates are
    always first so the permission boundary can never be dropped by an optional filter.
    """
    obs_filters: list[Any] = [
        Observation.workspace_id == workspace_id,
        Observation.source_id.in_(allowed_source_ids),
    ]
    if args.current_only and args.aggregation not in (
        "timeseries",
        "avg",
        "min",
        "max",
        "sum",
        "count",
    ):
        obs_filters.append(Observation.is_current.is_(True))
    if args.providers:
        obs_filters.append(
            func.lower(Observation.provider).in_(
                [p.strip().lower() for p in args.providers if p.strip()]
            )
        )
    if args.metrics:
        obs_filters.append(
            func.lower(Observation.metric).in_(
                [m.strip().lower() for m in args.metrics if m.strip()]
            )
        )
    if args.symbols:
        obs_filters.append(
            func.upper(Observation.symbol).in_(
                [s.strip().upper() for s in args.symbols if s.strip()]
            )
        )
    if args.region:
        obs_filters.append(func.upper(Observation.region) == args.region.strip().upper())
    if date_from is not None:
        obs_filters.append(Observation.observed_at >= date_from)
    if date_to is not None:
        obs_filters.append(Observation.observed_at <= date_to)
    return obs_filters


async def _observation_aggregates(
    session: AsyncSession, obs_filters: list[Any], limit: int
) -> list[dict[str, Any]]:
    """Run the grouped observation aggregate query and return JSON-ready aggregate rows.

    Read-only; one SELECT on ``session`` ordered by latest observation, bounded by ``limit``.
    """
    agg_rows = (
        await session.execute(
            select(
                Observation.provider,
                Observation.metric,
                Observation.symbol,
                Observation.unit,
                Observation.currency,
                func.count(Observation.id).label("row_count"),
                func.avg(Observation.value).label("avg_value"),
                func.min(Observation.value).label("min_value"),
                func.max(Observation.value).label("max_value"),
                func.sum(Observation.value).label("sum_value"),
                func.max(Observation.observed_at).label("latest_observed_at"),
            )
            .where(*obs_filters)
            .group_by(
                Observation.provider,
                Observation.metric,
                Observation.symbol,
                Observation.unit,
                Observation.currency,
            )
            .order_by(desc(func.max(Observation.observed_at)))
            .limit(limit)
        )
    ).all()
    return [
        {
            "provider": r.provider,
            "metric": r.metric,
            "symbol": r.symbol,
            "unit": r.unit,
            "currency": r.currency,
            "count": int(r.row_count or 0),
            "avg": float(r.avg_value) if r.avg_value is not None else None,
            "min": float(r.min_value) if r.min_value is not None else None,
            "max": float(r.max_value) if r.max_value is not None else None,
            "sum": float(r.sum_value) if r.sum_value is not None else None,
            "latest_observed_at": r.latest_observed_at.isoformat()
            if r.latest_observed_at
            else None,
        }
        for r in agg_rows
    ]


def _document_filters(
    args: QueryCollectedDataArgs,
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[Any]:
    """Build the workspace/source-scoped filter list (incl. full-text/ILIKE) for document queries.

    Pure expression builder; the workspace and allowed-source predicates are always first.
    """
    doc_filters: list[Any] = [
        Document.workspace_id == workspace_id,
        Document.source_id.in_(allowed_source_ids),
    ]
    if args.providers:
        doc_filters.append(
            func.lower(Source.provider).in_(
                [p.strip().lower() for p in args.providers if p.strip()]
            )
        )
    if date_from is not None:
        doc_filters.append(func.coalesce(Document.observed_at, Document.created_at) >= date_from)
    if date_to is not None:
        doc_filters.append(func.coalesce(Document.observed_at, Document.created_at) <= date_to)
    if args.text_query and args.text_query.strip():
        q = args.text_query.strip()
        ts_query = func.websearch_to_tsquery("simple", q)
        ts_vec = func.to_tsvector(
            "simple",
            func.concat(Document.title, " ", func.coalesce(DocumentVersion.content, "")),
        )
        pattern = f"%{q}%"
        doc_filters.append(
            or_(
                ts_vec.op("@@")(ts_query),
                Document.title.ilike(pattern),
                DocumentVersion.content.ilike(pattern),
            )
        )
    return doc_filters


async def _select_structured_documents(
    session: AsyncSession, doc_filters: list[Any], limit: int
) -> list[Any]:
    """Select the newest matching documents joined to source and current version (one SELECT)."""
    return list(
        (
            await session.execute(
                select(
                    Document.id.label("document_id"),
                    Document.source_id.label("source_id"),
                    Source.provider.label("provider"),
                    Source.name.label("source_name"),
                    Document.title.label("title"),
                    Document.canonical_url.label("url"),
                    Document.published_at.label("published_at"),
                    Document.observed_at.label("observed_at"),
                    DocumentVersion.id.label("document_version_id"),
                    func.left(DocumentVersion.content, 500).label("excerpt"),
                )
                .join(
                    Source,
                    and_(
                        Source.id == Document.source_id,
                        Source.workspace_id == Document.workspace_id,
                    ),
                )
                .join(
                    DocumentVersion,
                    and_(
                        DocumentVersion.document_id == Document.id,
                        DocumentVersion.version_number == Document.current_version,
                    ),
                )
                .where(*doc_filters)
                .order_by(
                    desc(func.coalesce(Document.observed_at, Document.created_at)),
                    desc(Document.id),
                )
                .limit(limit)
            )
        ).all()
    )


async def _hot_cache_snapshots(
    redis: Any, workspace_id: UUID, allowed_source_ids: list[UUID]
) -> list[dict[str, Any]]:
    """Read up to 8 connector hot-cache snapshots from Redis; any failure yields what was read.

    The cache is a best-effort enrichment, so every error is suppressed (never raises).
    """
    hot_snapshots: list[dict[str, Any]] = []
    with suppress(Exception):
        for sid in allowed_source_ids[:8]:
            raw_snap = await redis.get(f"connector:snapshot:{workspace_id}:{sid}")
            if raw_snap:
                snap = json.loads(
                    raw_snap.decode("utf-8") if isinstance(raw_snap, bytes) else str(raw_snap)
                )
                if isinstance(snap, dict):
                    hot_snapshots.append(
                        {
                            "provider": snap.get("provider"),
                            "realtime_tier": snap.get("realtime_tier"),
                            "collected_at": snap.get("collected_at"),
                            "record_count": snap.get("record_count"),
                        }
                    )
    return hot_snapshots


def _serialize_observations(obs_rows: Any, citation_map: dict[UUID, int]) -> list[dict[str, Any]]:
    """Serialise observation rows, attaching ``[n]`` citation markers from ``citation_map``."""
    return [
        {
            "provider": obs.provider,
            "metric": obs.metric,
            "symbol": obs.symbol,
            "region": obs.region,
            "value": obs.value,
            "unit": obs.unit,
            "currency": obs.currency,
            "observed_at": obs.observed_at.isoformat(),
            "is_current": obs.is_current,
            "citation": (
                f"[{citation_map[obs.document_version_id]}]"
                if obs.document_version_id in citation_map
                else None
            ),
        }
        for obs in obs_rows
    ]


def _serialize_documents(
    doc_rows: list[Any], citation_map: dict[UUID, int]
) -> list[dict[str, Any]]:
    """Serialise structured document rows, attaching ``[n]`` citation markers from ``citation_map``."""
    return [
        {
            "provider": d.provider,
            "source_name": d.source_name,
            "title": d.title,
            "url": d.url,
            "published_at": d.published_at.isoformat() if d.published_at else None,
            "observed_at": d.observed_at.isoformat() if d.observed_at else None,
            "excerpt": d.excerpt,
            "citation": (
                f"[{citation_map[d.document_version_id]}]"
                if d.document_version_id in citation_map
                else None
            ),
        }
        for d in doc_rows
    ]


async def _execute_structured_mode(
    session: AsyncSession,
    args: QueryCollectedDataArgs,
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    date_from: datetime | None,
    date_to: datetime | None,
    evidence_list: list[EvidenceItem],
    redis: Any = None,
    available_providers: list[str] | None = None,
    web_numbering: tuple[int, int] | None = None,
) -> str:
    """Execute parameterized SQLAlchemy queries for observations and/or documents.

    Documents are queried too whenever a requested provider is not a measurement provider (event and
    news feeds store documents only). When neither table returns rows, ``available_providers`` lists the
    active providers so the model can retry with a valid id.

    Query order is fixed: optional observation aggregate, observation rows, document rows, then
    evidence registration (which appends to ``evidence_list``), then the best-effort Redis
    hot-cache read. Read-only on ``session``; no commit. Returns the JSON payload string.
    """
    out: dict[str, Any] = {"mode": "structured", "aggregation": args.aggregation}
    version_pairs: list[tuple[UUID, UUID]] = []
    want_obs = args.target in ("observations", "both")
    requested = {p.strip().lower() for p in args.providers or [] if p.strip()}
    want_docs = args.target in ("documents", "both") or bool(
        requested - set(MEASUREMENT_PROVIDER_IDS)
    )

    obs_rows: list[Any] = []
    if want_obs:
        obs_filters = _observation_filters(
            args,
            workspace_id=workspace_id,
            allowed_source_ids=allowed_source_ids,
            date_from=date_from,
            date_to=date_to,
        )
        if args.aggregation in ("avg", "min", "max", "sum", "count"):
            out["observation_aggregates"] = await _observation_aggregates(
                session, obs_filters, args.limit
            )
        obs_rows = list(
            (
                await session.execute(
                    select(Observation)
                    .where(*obs_filters)
                    .order_by(desc(Observation.observed_at), desc(Observation.id))
                    .limit(args.limit)
                )
            )
            .scalars()
            .all()
        )
        version_pairs.extend((obs.document_id, obs.document_version_id) for obs in obs_rows)

    doc_rows_raw: list[Any] = []
    if want_docs:
        doc_filters = _document_filters(
            args,
            workspace_id=workspace_id,
            allowed_source_ids=allowed_source_ids,
            date_from=date_from,
            date_to=date_to,
        )
        doc_rows_raw = await _select_structured_documents(session, doc_filters, args.limit)
        version_pairs.extend((d.document_id, d.document_version_id) for d in doc_rows_raw)

    citation_map = await _register_evidence_for_versions(
        session, version_pairs, evidence_list, web_numbering
    )

    if want_obs:
        out["observations"] = _serialize_observations(obs_rows, citation_map)
    if want_docs:
        out["documents"] = _serialize_documents(doc_rows_raw, citation_map)

    if not obs_rows and not doc_rows_raw and available_providers:
        out["available_providers"] = available_providers

    if redis is not None and callable(getattr(redis, "get", None)):
        hot_snapshots = await _hot_cache_snapshots(redis, workspace_id, allowed_source_ids)
        if hot_snapshots:
            out["hot_cache_snapshots"] = hot_snapshots

    return json.dumps(out, ensure_ascii=False, default=str)
