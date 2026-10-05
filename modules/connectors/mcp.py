"""R08 MCP collection adapter: owner-reviewed MCP tools/resources collected into the ingestion pipeline.

An MCP connection collects only through grants reviewed with ``purpose="collection"`` that are
scoped to exactly one source of type ``mcp`` (the grant is the explicit allowlist of tools and
resources; there are no wildcards). The adapter executes each configured call through
``modules.tools.public.read_collection_capability`` (the reviewed SDK client, admission slots,
egress policy and credential store) and submits the normalized records through the same
``modules.ingestion.public`` receipt API as other connectors. Arguments come from owner-saved
source configuration only; the model, the browser and the request body never name a command,
URL or tool. Disabling the connection or revoking the grant stops new calls at the next fence
check and retains every observation already ingested.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.realtime import commit_with_replay, make_source_change
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import Receipt, ReceiveBatch
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource
from modules.tools import public as tools

logger = logging.getLogger(__name__)

PROVIDER_ID = "mcp"
MAX_CALLS = 10
MAX_RECORDS = 500
MAX_PROVIDER_ID = 512


@dataclass(frozen=True)
class McpCollectionContract:
    """Declare the adapter's record identity, normalization, provenance and window limitations."""

    record_identity: str
    normalization: str
    provenance: tuple[str, ...]
    pagination: str
    history: str


CONTRACT = McpCollectionContract(
    record_identity=(
        "provider_id = mcp:<connection>:<digest of remote capability key and canonical arguments>:<item>, "
        "where <item> is the resource URI, the id of a structuredContent.items[] object, or the text-block "
        "index. Index identities are only stable while the server keeps block order."
    ),
    normalization=(
        "Resources: one record per text content. Tools: one record per structuredContent.items[] object "
        "(canonical JSON), otherwise one per non-empty text block. Binary, image and embedded-resource "
        "blocks are skipped. version is a SHA-256 of the content; observed_at is the collection time "
        "because MCP carries no item timestamp. Content is capped at 1,000,000 characters and 500 records."
    ),
    provenance=(
        "connection_id", "grant_id", "grant_revision", "connection_revision", "capability_kind",
        "remote_capability_key", "descriptor_hash", "arguments_digest",
    ),
    pagination=(
        "Exactly one request per configured call per run. Provider cursors and next-page links are "
        "not followed; a server that paginates is collected one window at a time."
    ),
    history=(
        "Only what the tool or resource returns now; there is no backfill. Every run appends one "
        "observation per record, so unchanged items repeat as observations with an identical version."
    ),
)


class McpCollectionCall(BaseModel):
    """Pin one reviewed collection grant and the fixed arguments the owner saved for it."""

    model_config = ConfigDict(extra="forbid")

    grant_id: UUID
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpCollectionConfig(BaseModel):
    """Owner-saved configuration of an ``mcp`` source: one connection and its allowlisted calls."""

    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    calls: list[McpCollectionCall] = Field(min_length=1, max_length=MAX_CALLS)
    schedule_interval_minutes: int = Field(default=60, ge=15, le=1440)
    timezone: str = Field(default="Asia/Ho_Chi_Minh", max_length=64)

    @field_validator("schedule_interval_minutes")
    @classmethod
    def supported_interval(cls, value: int) -> int:
        """Restrict n8n schedules to the bounded intervals supported by packaged sources."""
        if value not in {15, 30, 60, 360, 1440}:
            raise ValueError("unsupported MCP collection interval")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """Reject unknown timezone names before n8n receives a schedule."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class McpCollectionError(Exception):
    """Report a bounded, secret-free collection failure with a stable machine code."""

    def __init__(self, code: str) -> None:
        """Remember the stable error code stored on the source and returned to the owner."""
        super().__init__(code)
        self.code = code


def configuration(source: ConnectorSource) -> McpCollectionConfig:
    """Parse the detached source configuration into the strict MCP collection contract."""
    return McpCollectionConfig.model_validate(source.configuration or {})


def validate(source: ConnectorSource) -> McpCollectionConfig:
    """Require an active ``mcp`` source with a valid configuration; raise ValueError otherwise."""
    if source.status != "active":
        raise ValueError("Source is not active")
    if source.type != PROVIDER_ID:
        raise ValueError("Source is not an MCP collection source")
    try:
        return configuration(source)
    except ValueError as exc:
        raise ValueError("MCP collection configuration is invalid") from exc


def _canonical(value: object) -> str:
    """Encode JSON deterministically so identities and digests are stable across runs."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str, length: int = 64) -> str:
    """Return a hex SHA-256 prefix of a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def normalize(
    read: tools.McpCollectionRead, arguments: dict[str, Any], observed_at: datetime,
) -> list[dict[str, Any]]:
    """Convert one bounded MCP payload into ingestion records following ``CONTRACT``.

    Pure function: no I/O. Identity includes the arguments digest so two owner calls to the same
    tool never collide, and the content digest becomes the version so ingestion can version changes.
    """
    arguments_digest = _digest(_canonical(arguments))
    scope = _digest(f"{read.remote_key}\n{arguments_digest}", 16)
    prefix = f"mcp:{read.connection_id.hex}:{scope}:"
    candidates: list[tuple[str, str]] = []
    payload = read.payload
    if read.kind == "resource":
        for block in payload.get("contents", []) if isinstance(payload.get("contents"), list) else []:
            if isinstance(block, dict) and isinstance(block.get("text"), str) and isinstance(block.get("uri"), str):
                candidates.append((block["uri"], block["text"]))
    else:
        structured = payload.get("structuredContent")
        items = structured.get("items") if isinstance(structured, dict) else None
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    ident = item.get("id")
                    key = f"id:{ident}" if isinstance(ident, (str, int)) and not isinstance(ident, bool) else f"index:{index}"
                    candidates.append((key, _canonical(item)))
        else:
            for index, block in enumerate(payload.get("content", []) if isinstance(payload.get("content"), list) else []):
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    candidates.append((f"block:{index}", block["text"]))
    provenance = {
        "adapter": PROVIDER_ID, "connection_id": str(read.connection_id), "grant_id": str(read.grant_id),
        "grant_revision": read.grant_revision, "connection_revision": read.connection_revision,
        "capability_kind": read.kind, "remote_capability_key": read.remote_key[:512],
        "descriptor_hash": read.descriptor_hash, "arguments_digest": arguments_digest,
    }
    records: list[dict[str, Any]] = []
    for identity, content in candidates:
        content = content[:1_000_000]
        if not content.strip():
            continue
        provider_id = prefix + identity
        if len(provider_id) > MAX_PROVIDER_ID:
            provider_id = prefix + "h:" + _digest(identity)
        records.append({
            "provider_id": provider_id, "content": content, "observed_at": observed_at.isoformat(),
            "version": _digest(content, 32), "metadata": dict(provenance),
        })
    return records


async def collect(
    runtime: tools.McpRuntime,
    session_factory: async_sessionmaker[AsyncSession],
    source_id: UUID,
    expected_connector_revision: int,
    expected_generation: int,
    owner_id: int = 1,
    collector_token: str | None = None,
    expected_connection_id: UUID | None = None,
) -> dict[str, Any]:
    """Run the source's allowlisted calls once and submit one fenced ingestion batch.

    Invariants: each call re-resolves its grant and exact active provisioning revision, so a
    disabled connection, revoked grant, or changed/disabled source fails closed before any
    outbound request and again before accepting its result. A failed call never blocks others.
    The ingestion credential is minted and revoked here; the batch carries the same revision
    fence checked during collection and at final receipt. A retry re-collects observations.
    """
    async with session_factory() as session:
        source = await sources.get_connector_source(session, source_id)
        if source is None:
            raise LookupError("Source not found")
        try:
            config = validate(source)
        except ValueError as exc:
            raise McpCollectionError("mcp_source_invalid") from exc
        if source.generation != expected_generation:
            raise McpCollectionError("mcp_source_stale")
        if expected_connection_id is not None and config.connection_id != expected_connection_id:
            raise McpCollectionError("mcp_connection_stale")
        if not await _collection_fence(
            session, source, expected_generation,
            expected_connector_revision,
        ):
            raise McpCollectionError("mcp_source_stale")
        cursor_before = await ingestion.get_source_cursor(session, source_id)
    collected_at = datetime.now(UTC)
    records: list[dict[str, Any]] = []
    failed: list[str] = []
    for call in config.calls:
        try:
            async def collector_current() -> bool:
                """Recheck provisioning and optional n8n authority at every MCP request/result fence."""
                async with session_factory() as session:
                    current = await sources.get_connector_source(session, source_id)
                    if current is None or not await _collection_fence(
                        session, current,
                        expected_generation,
                        expected_connector_revision,
                    ):
                        return False
                    return collector_token is None or await ingestion.collector_can_ingest(
                        session, source_id, collector_token, scope="mcp:collect",
                    )

            read = await tools.read_collection_capability(
                runtime, owner_id, connection_id=config.connection_id, grant_id=call.grant_id,
                source_id=source_id, source_generation=source.generation, arguments=call.arguments,
                authorize_extra=collector_current,
            )
            records.extend(normalize(read, call.arguments, collected_at))
        except McpCollectionError:
            raise
        except Exception as exc:
            if not await collector_current():
                raise McpCollectionError(
                    "mcp_collector_revoked" if collector_token is not None else "mcp_source_stale"
                ) from exc
            # Only the exception type is logged: provider text may contain secrets or content.
            logger.warning("MCP collection call failed (%s)", type(exc).__name__)
            failed.append(str(call.grant_id))
    if len(failed) == len(config.calls):
        if not await _fence_current(session_factory, source_id, expected_generation, expected_connector_revision):
            raise McpCollectionError("mcp_source_stale")
        await _record_result(session_factory, source, "mcp_collection_failed")
        raise McpCollectionError("mcp_collection_failed")
    if not records:
        if not await _fence_current(session_factory, source_id, expected_generation, expected_connector_revision):
            raise McpCollectionError("mcp_source_stale")
        await _record_result(session_factory, source, None, no_changes=True)
        return {"status": "no_changes", "run_id": None, "batch_id": None, "failed_calls": failed}
    if len(records) > MAX_RECORDS:
        records = records[:MAX_RECORDS]
    batch = ReceiveBatch(
        source_id=source_id, source_generation=source.generation,
        connector_revision=expected_connector_revision,
        batch_key="mcp:" + _digest(f"{source_id}:{source.generation}:{collected_at.isoformat()}"),
        cursor_before=cursor_before, cursor_after="mcp:" + collected_at.isoformat(),
        records=records,
    )
    token: str | None = None
    try:
        async with session_factory() as session:
            current = await sources.get_connector_source(session, source_id)
            if current is None or not await _collection_fence(
                session, current, expected_generation, expected_connector_revision,
            ):
                raise McpCollectionError("mcp_source_stale")
            token = await ingestion.create_collector_credential(session, source_id)
            receipt: Receipt = await ingestion.receive_connector_batch(session, batch, token)
    finally:
        if token is not None:
            async with session_factory() as session:
                await ingestion.revoke_collector_credential(session, token)
                await session.commit()
    return {
        "status": receipt.status, "run_id": receipt.run_id, "batch_id": receipt.batch_id,
        "failed_calls": failed,
    }


async def _collection_fence(
    session: AsyncSession,
    source: ConnectorSource,
    source_generation: int,
    connector_revision: int,
) -> bool:
    """Require this active MCP source to retain the exact fully applied connector revision."""
    from modules.connectors import provisioning

    return await provisioning.require_collection_fence(
        session, source, source_generation, connector_revision, lock=True,
    )


async def _fence_current(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
) -> bool:
    """Check the current source generation and applied provisioning fence in a fresh session."""
    async with session_factory() as session:
        source = await sources.get_connector_source(session, source_id)
        return source is not None and await _collection_fence(
            session, source, source_generation, connector_revision,
        )


async def _record_result(
    session_factory: async_sessionmaker[AsyncSession], source: ConnectorSource,
    error_code: str | None, *, no_changes: bool = False,
) -> None:
    """Persist a collection outcome on the source when its generation is still current."""
    async with session_factory() as session:
        if await sources.record_collection_result(
            session, source.id, source.generation, datetime.now(UTC), error_code, no_changes=no_changes,
        ):
            current = await sources.lock_source(session, source.id)
            drafts = [make_source_change(current.id, current.generation, current.status)] if current else []
            await commit_with_replay(session, drafts)
