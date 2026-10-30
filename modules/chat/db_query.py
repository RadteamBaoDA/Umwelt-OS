"""Structured & Hybrid Safe Text-to-SQL database query tool for Chat over collected datasources.

Enables the chat model to query collected ``observations`` and ``documents`` in PostgreSQL
(and Redis hot snapshots) without requiring vector embeddings (``pgvector``). Enforces workspace
isolation, ``AccessFence`` verification, ``local_only`` egress protection, a 2000ms statement
timeout, and automatic ``EvidenceItem`` registration for grounded citations.
"""

from __future__ import annotations

import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, Scope
from modules.chat.schemas import DB_QUERY_KEY, GADGET_SCOPE_KEY, EvidenceItem
from modules.knowledge.documents.models import Document, DocumentChunk, DocumentVersion
from modules.knowledge.observations.models import Observation
from modules.sources.models import Source

TOOL_QUERY_COLLECTED_DATA = "query_collected_data"
MAX_DB_QUERY_ROWS = 50
MAX_SQL_LENGTH = 2000

_FORBIDDEN_SQL_TOKENS = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|copy|"
    r"execute|call|do|set|reset|show|lock|vacuum|analyze|explain|listen|notify|"
    r"prepare|deallocate|begin|commit|rollback|savepoint|into|pg_sleep|pg_read_file|"
    r"pg_ls_dir|pg_stat|information_schema|pg_catalog|dblink|lo_import|lo_export|"
    r"owner|auth_sessions|users|workspaces|workspace_memberships|conversations|"
    r"messages|response_runs|stream_events|connector_rest_credentials|"
    r"connector_oauth_grants|collector_credentials|"
    r"observations|documents|document_versions|document_chunks|sources"
    r")\b",
    re.IGNORECASE,
)
_FORBIDDEN_PREFIX_RE = re.compile(r"\b(pg_|connector_)", re.IGNORECASE)
_ALLOWED_CTE_RE = re.compile(r"\b(scoped_observations|scoped_documents)\b", re.IGNORECASE)
_FOR_LOCKING_RE = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", re.IGNORECASE)

_AUTO_DB_KEYWORDS_RE = re.compile(
    r"\b("
    r"sql|database|datasource|query|observation|observations|metric|metrics|timeseries|"
    r"price|prices|giá|tỷ\s*giá|lạm\s*phát|gdp|cpi|ppi|unemployment|fed|ecb|world\s*bank|"
    r"binance|bitcoin|btc|eth|sol|crypto|coinpaprika|coingecko|frankfurter|"
    r"earthquake|earthquakes|động\s*đất|seismic|magnitude|usgs|emsc|bmkg|tsunami|sóng\s*thần|"
    r"weather|thời\s*tiết|temperature|nhiệt\s*độ|wind|rain|humidity|openaq|air\s*quality|"
    r"alert|alerts|cảnh\s*báo|gdacs|nws|noaa|jma|iss|nasa|cisa|cve|nvd|"
    r"average|avg|trung\s*bình|maximum|minimum|highest|lowest|cao\s*nhất|thấp\s*nhất|"
    r"tổng\s*hợp|thống\s*kê|so\s*sánh|biến\s*động|xu\s*hướng"
    r")\b",
    re.IGNORECASE,
)


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
    aggregation: Literal["none", "latest", "avg", "min", "max", "sum", "count", "timeseries"] = "none"
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
                "language, content_excerpt, document_version_id)."
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
                        "description": "Which collected tables to query in structured mode.",
                    },
                    "providers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional provider IDs (e.g. ['binance', 'usgs', 'world_bank', 'vnexpress_business']).",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional metric names (e.g. ['price', 'magnitude', 'temperature_2m']).",
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
                        "enum": ["none", "latest", "avg", "min", "max", "sum", "count", "timeseries"],
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


def should_auto_enable_db_query(
    prompt: str, retrieval_context: dict[str, Any] | None,
) -> bool:
    """Determine whether the database query tool should be enabled for this chat turn.

    Args:
        prompt: User message text.
        retrieval_context: Stored ``ResponseRun.retrieval_context`` dictionary.

    Returns:
        True when explicitly requested, bound to a dashboard gadget, or triggered by data keywords.
    """
    ctx = retrieval_context or {}
    db_flag = ctx.get(DB_QUERY_KEY)
    if db_flag is True or (isinstance(db_flag, dict) and db_flag.get("requested") is True):
        return True
    if ctx.get("kind") == "gadget" or isinstance(ctx.get(GADGET_SCOPE_KEY), dict):
        return True
    return bool(_AUTO_DB_KEYWORDS_RE.search(prompt or ""))


def validate_readonly_sql(sql: str) -> str:
    """Validate a model-generated SQL string to guarantee read-only SELECT execution on scoped CTEs.

    Args:
        sql: Raw SQL string supplied by the model in ``mode='sql'``.

    Returns:
        Cleaned single-statement SELECT query string without trailing semicolon.

    Raises:
        ValueError: If the SQL is empty, not a SELECT/WITH query, contains comments/semicolons/DDL/DML,
            or fails to reference ``scoped_observations`` or ``scoped_documents``.
    """
    cleaned = (sql or "").strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if not cleaned or len(cleaned) > MAX_SQL_LENGTH:
        raise ValueError("SQL query must be between 1 and 2000 characters")
    if ";" in cleaned or "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
        raise ValueError("SQL query must be a single statement without comments or semicolons")
    if not re.match(r"^\s*(select|with)\b", cleaned, re.IGNORECASE) or not re.search(r"\bselect\b", cleaned, re.IGNORECASE):
        raise ValueError("SQL query must begin with SELECT or WITH ... SELECT")
    if _FORBIDDEN_SQL_TOKENS.search(cleaned) or _FORBIDDEN_PREFIX_RE.search(cleaned):
        raise ValueError("SQL query contains a forbidden keyword or system identifier")
    if _FOR_LOCKING_RE.search(cleaned):
        raise ValueError("SQL locking clauses are not allowed")
    if not _ALLOWED_CTE_RE.search(cleaned):
        raise ValueError("SQL query must reference scoped_observations or scoped_documents")
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
        Source.id, Source.provider, Source.name,
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


async def _register_evidence_for_versions(
    session: AsyncSession,
    version_pairs: list[tuple[UUID, UUID]],
    existing_evidence: list[EvidenceItem],
) -> dict[UUID, int]:
    """Load chunk_index=0 for returned document versions and append new ``EvidenceItem``s.

    Args:
        session: Active asynchronous database session.
        version_pairs: Pairs of ``(document_id, document_version_id)`` from query rows.
        existing_evidence: Mutable list of ``EvidenceItem``s on the current ``AnswerContext``.

    Returns:
        Mapping of ``document_version_id`` to 1-based citation index ``[n]`` in ``existing_evidence``.
    """
    by_version: dict[UUID, int] = {
        item.document_version_id: idx + 1 for idx, item in enumerate(existing_evidence)
    }
    needed = [
        ver_id for _doc_id, ver_id in version_pairs if ver_id not in by_version
    ][:12]
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
        by_version[row.document_version_id] = len(existing_evidence)
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

    Returns:
        Compact JSON string summarizing the query results and their ``[n]`` citation indices.
    """
    current_fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_fence != expected_access_fence:
        return json.dumps({"error": "workspace_access_changed", "rows": []})

    try:
        args = QueryCollectedDataArgs.model_validate(raw_arguments or {})
    except Exception as exc:  # noqa: BLE001
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
            )
    except ValueError as exc:
        return json.dumps({"error": str(exc), "rows": []})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"query_execution_failed: {type(exc).__name__}", "rows": []})


async def _execute_sql_mode(
    session: AsyncSession,
    sql_query: str,
    *,
    workspace_id: UUID,
    allowed_source_ids: list[UUID],
    limit: int,
    evidence_list: list[EvidenceItem],
) -> str:
    """Run a validated read-only SELECT query inside pre-fenced CTE views."""
    clean_sql = validate_readonly_sql(sql_query)
    wrapped = text(
        """
        WITH scoped_observations AS (
            SELECT
                o.id,
                o.source_id,
                o.provider,
                o.metric,
                o.symbol,
                o.region,
                o.value,
                o.unit,
                o.currency,
                o.observed_at,
                o.published_at,
                o.quality,
                o.is_current,
                o.document_id,
                o.document_version_id
            FROM observations o
            WHERE o.workspace_id = :workspace_id
              AND o.source_id = ANY(:source_ids)
        ),
        scoped_documents AS (
            SELECT
                d.id,
                d.source_id,
                s.provider,
                d.title,
                d.canonical_url,
                d.published_at,
                d.observed_at,
                d.language,
                LEFT(dv.content, 600) AS content_excerpt,
                dv.id AS document_version_id
            FROM documents d
            JOIN sources s ON s.id = d.source_id AND s.workspace_id = d.workspace_id
            JOIN document_versions dv ON dv.document_id = d.id AND dv.version_number = d.current_version
            WHERE d.workspace_id = :workspace_id
              AND d.source_id = ANY(:source_ids)
        )
        SELECT * FROM (
        """
        + clean_sql
        + """
        ) AS _bounded_query
        LIMIT :max_rows
        """
    )
    res = await session.execute(
        wrapped,
        {
            "workspace_id": workspace_id,
            "source_ids": allowed_source_ids,
            "max_rows": min(limit, MAX_DB_QUERY_ROWS),
        },
    )
    mappings = res.mappings().all()
    version_pairs: list[tuple[UUID, UUID]] = []
    for row in mappings:
        doc_id = row.get("document_id") or row.get("id")
        ver_id = row.get("document_version_id")
        if isinstance(doc_id, UUID) and isinstance(ver_id, UUID):
            version_pairs.append((doc_id, ver_id))
    citation_map = await _register_evidence_for_versions(session, version_pairs, evidence_list)

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
) -> str:
    """Execute parameterized SQLAlchemy queries for observations and/or documents."""
    out: dict[str, Any] = {"mode": "structured", "aggregation": args.aggregation}
    version_pairs: list[tuple[UUID, UUID]] = []

    if args.target in ("observations", "both"):
        obs_filters = [
            Observation.workspace_id == workspace_id,
            Observation.source_id.in_(allowed_source_ids),
        ]
        if args.current_only and args.aggregation not in ("timeseries", "avg", "min", "max", "sum", "count"):
            obs_filters.append(Observation.is_current.is_(True))
        if args.providers:
            obs_filters.append(
                func.lower(Observation.provider).in_([p.strip().lower() for p in args.providers if p.strip()])
            )
        if args.metrics:
            obs_filters.append(
                func.lower(Observation.metric).in_([m.strip().lower() for m in args.metrics if m.strip()])
            )
        if args.symbols:
            obs_filters.append(
                func.upper(Observation.symbol).in_([s.strip().upper() for s in args.symbols if s.strip()])
            )
        if args.region:
            obs_filters.append(func.upper(Observation.region) == args.region.strip().upper())
        if date_from is not None:
            obs_filters.append(Observation.observed_at >= date_from)
        if date_to is not None:
            obs_filters.append(Observation.observed_at <= date_to)

        if args.aggregation in ("avg", "min", "max", "sum", "count"):
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
                    .limit(args.limit)
                )
            ).all()
            out["observation_aggregates"] = [
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
                    "latest_observed_at": r.latest_observed_at.isoformat() if r.latest_observed_at else None,
                }
                for r in agg_rows
            ]

        obs_rows = (
            await session.execute(
                select(Observation)
                .where(*obs_filters)
                .order_by(desc(Observation.observed_at), desc(Observation.id))
                .limit(args.limit)
            )
        ).scalars().all()
        for obs in obs_rows:
            version_pairs.append((obs.document_id, obs.document_version_id))

    doc_rows_raw: list[Any] = []
    if args.target in ("documents", "both"):
        doc_filters = [
            Document.workspace_id == workspace_id,
            Document.source_id.in_(allowed_source_ids),
        ]
        if args.providers:
            doc_filters.append(
                func.lower(Source.provider).in_([p.strip().lower() for p in args.providers if p.strip()])
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

        doc_rows_raw = list(
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
                    .join(Source, and_(Source.id == Document.source_id, Source.workspace_id == Document.workspace_id))
                    .join(
                        DocumentVersion,
                        and_(
                            DocumentVersion.document_id == Document.id,
                            DocumentVersion.version_number == Document.current_version,
                        ),
                    )
                    .where(*doc_filters)
                    .order_by(desc(func.coalesce(Document.observed_at, Document.created_at)), desc(Document.id))
                    .limit(args.limit)
                )
            ).all()
        )
        for d in doc_rows_raw:
            version_pairs.append((d.document_id, d.document_version_id))

    citation_map = await _register_evidence_for_versions(session, version_pairs, evidence_list)

    if args.target in ("observations", "both"):
        out["observations"] = [
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
    if args.target in ("documents", "both"):
        out["documents"] = [
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
            for d in doc_rows_raw
        ]

    if redis is not None and callable(getattr(redis, "get", None)):
        hot_snapshots: list[dict[str, Any]] = []
        with suppress(Exception):
            for sid in allowed_source_ids[:8]:
                raw_snap = await redis.get(f"connector:snapshot:{workspace_id}:{sid}")
                if raw_snap:
                    snap = json.loads(raw_snap.decode("utf-8") if isinstance(raw_snap, bytes) else str(raw_snap))
                    if isinstance(snap, dict):
                        hot_snapshots.append(
                            {
                                "provider": snap.get("provider"),
                                "realtime_tier": snap.get("realtime_tier"),
                                "collected_at": snap.get("collected_at"),
                                "record_count": snap.get("record_count"),
                            }
                        )
        if hot_snapshots:
            out["hot_cache_snapshots"] = hot_snapshots

    return json.dumps(out, ensure_ascii=False, default=str)
