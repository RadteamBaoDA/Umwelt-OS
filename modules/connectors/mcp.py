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

from pydantic import BaseModel, ConfigDict, Field
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
    owner_id: int = 1,
) -> dict[str, Any]:
    """Run the source's allowlisted calls once and submit one fenced ingestion batch.

    Invariants: each call re-resolves its grant fence, so a disabled connection or revoked grant
    makes that call fail closed without any outbound request; a failed call never blocks the
    others (it is listed in ``failed_calls``) and total failure records the source error code.
    The batch uses native ingestion (no connector provisioning row), a one-run collector
    credential minted and revoked here, and cursor chaining, so overlapping runs lose with HTTP
    409 instead of interleaving. A retried request re-collects and appends new observations.
    """
    async with session_factory() as session:
        source = await sources.get_connector_source(session, source_id)
        if source is None:
            raise LookupError("Source not found")
        try:
            config = validate(source)
        except ValueError as exc:
            raise McpCollectionError("mcp_source_invalid") from exc
        cursor_before = await ingestion.get_source_cursor(session, source_id)
    collected_at = datetime.now(UTC)
    records: list[dict[str, Any]] = []
    failed: list[str] = []
    for call in config.calls:
        try:
            read = await tools.read_collection_capability(
                runtime, owner_id, connection_id=config.connection_id, grant_id=call.grant_id,
                source_id=source_id, source_generation=source.generation, arguments=call.arguments,
            )
            records.extend(normalize(read, call.arguments, collected_at))
        except Exception as exc:
            # Only the exception type is logged: provider text may contain secrets or content.
            logger.warning("MCP collection call failed (%s)", type(exc).__name__)
            failed.append(str(call.grant_id))
    if len(failed) == len(config.calls):
        await _record_result(session_factory, source, "mcp_collection_failed")
        raise McpCollectionError("mcp_collection_failed")
    if not records:
        await _record_result(session_factory, source, None, no_changes=True)
        return {"status": "no_changes", "run_id": None, "batch_id": None, "failed_calls": failed}
    if len(records) > MAX_RECORDS:
        records = records[:MAX_RECORDS]
    batch = ReceiveBatch(
        source_id=source_id, source_generation=source.generation, connector_revision=None,
        batch_key="mcp:" + _digest(f"{source_id}:{source.generation}:{collected_at.isoformat()}"),
        cursor_before=cursor_before, cursor_after="mcp:" + collected_at.isoformat(),
        records=records,
    )
    token: str | None = None
    try:
        async with session_factory() as session:
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
