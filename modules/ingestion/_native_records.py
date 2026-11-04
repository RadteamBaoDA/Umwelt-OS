"""Native record identity, dedup and Telegram delivery proof helpers (internal; import through ``public``)."""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from modules.ingestion._scope import _digest
from modules.ingestion.models import IngestionBatch, SourceObservation
from modules.ingestion.schemas import IngestionRecord, TelegramDeliveryProof, TelegramRawDelivery


def validate_telegram_record_delivery(
    record: IngestionRecord,
    provider_metadata: object,
    proof: TelegramDeliveryProof,
    raw: TelegramRawDelivery,
    *,
    allowed_chat_ids: tuple[str, ...] | None = None,
) -> None:
    """Bind mapper identity, timestamps, version, and bot epoch to the raw update.

    Called before receipt/cursor commit and again when normalizing persisted owner
    proof. Telegram's Unix clocks and identity are compared to exact Bot API fields;
    optional scope filtering is enforced only at initial acceptance.
    """
    from modules.knowledge.documents.public import ProviderRecordMetadata

    metadata = ProviderRecordMetadata.model_validate(provider_metadata)
    detail = metadata.telegram
    update = raw.update
    edited_present = "edited_channel_post" in update
    original_present = "channel_post" in update
    message_key = "edited_channel_post" if edited_present else "channel_post"
    message = update.get(message_key)
    chat = message.get("chat") if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(chat, dict) or detail is None:
        raise ValueError("Telegram channel message is missing")
    chat_id_value = chat.get("id")
    message_id_value = message.get("message_id")
    date_value = message.get("date")
    edit_date_value = message.get("edit_date")
    if (
        isinstance(update.get("update_id"), bool)
        or update.get("update_id") != raw.update_id
        or raw.update_id != proof.update_id
        or proof.raw_update_sha256 != raw.raw_update_sha256
        or edited_present == original_present
        or detail.edited_received != edited_present
        or chat.get("type") != "channel"
        or isinstance(chat_id_value, bool) or not isinstance(chat_id_value, int)
        or isinstance(message_id_value, bool) or not isinstance(message_id_value, int) or message_id_value < 0
        or isinstance(date_value, bool) or not isinstance(date_value, int) or date_value < 0
        or (edited_present and (isinstance(edit_date_value, bool) or not isinstance(edit_date_value, int) or edit_date_value < 0))
    ):
        raise ValueError("Telegram raw identity or clock fields are invalid")
    channel_id = str(chat_id_value)
    message_id = str(message_id_value)
    if allowed_chat_ids is not None and channel_id not in allowed_chat_ids:
        raise ValueError("Telegram channel is outside configured source scope")
    try:
        published_at = datetime.fromtimestamp(date_value, UTC)
        edited_at = datetime.fromtimestamp(edit_date_value, UTC) if edited_present and edit_date_value is not None else None
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Telegram raw timestamps are invalid") from exc
    observed_at = edited_at if edited_present else published_at
    assert observed_at is not None
    expected_version = f"telegram:{proof.epoch}:{proof.update_id}:{observed_at.isoformat()}"
    if (
        metadata.provider != "telegram"
        or metadata.identity != record.provider_id
        or record.provider_id != f"telegram:{channel_id}:{message_id}"
        or metadata.provider_version != record.version
        or record.version != expected_version
        or detail.bot_id != proof.bot_id
        or detail.epoch != proof.epoch
        or detail.update_id != proof.update_id
        or detail.channel_id != channel_id
        or detail.message_id != message_id
        or detail.published_at != published_at
        or detail.edited_at != edited_at
        or metadata.provider_modified_at != edited_at
        or record.observed_at != observed_at
        or metadata.timestamp_basis != ("provider_modified" if edited_present else "provider_published")
        or detail.raw_update_sha256 is not None
    ):
        raise ValueError("Telegram mapped record does not match its raw delivery")


def native_record_key(record: IngestionRecord) -> tuple[str, str]:
    """Return the ``(provider_id, record_hash)`` dedup key of a native record.

    The hash covers version, content and metadata but not ``collected_at``. Both the connector runner's
    read-only pre-cap dedup and the in-transaction accept path call this one function so the two keys
    cannot diverge.
    """
    return record.provider_id, _digest({
        "version": record.version, "content": record.content,
        "metadata": record.model_dump(mode="json", exclude={"collected_at"})["metadata"],
    })


async def seen_record_keys(
    session: AsyncSession, *, source_id: UUID, source_generation: int, provider: str | None,
    keys: Iterable[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Return the ``(provider_id, record_hash)`` pairs this source already observed in its current generation.

    Re-polled feeds (RSS re-reads a 24h window every poll) otherwise append identical observations
    that the document layer then drops by ``record_hash`` anyway. Measurement providers are never
    deduplicated: an identical reading at a new ``observed_at`` is a new world measurement and
    ``upsert_from_ingestion_in_uow`` needs its observation row. Scoping to the current generation lets a
    source reset re-ingest everything. Read-only; bounded by the caller's record cap.
    """
    from modules.knowledge.documents.public import MEASUREMENT_PROVIDER_IDS

    wanted = list(set(keys))
    if not wanted or provider in MEASUREMENT_PROVIDER_IDS:
        return set()
    found: set[tuple[str, str]] = set()
    for start in range(0, len(wanted), 500):  # keep the IN list well under the bind-parameter limit
        rows = await session.execute(
            select(SourceObservation.provider_id, SourceObservation.record_hash)
            .join(IngestionBatch, IngestionBatch.id == SourceObservation.batch_id)
            .where(
                SourceObservation.source_id == source_id,
                IngestionBatch.source_generation == source_generation,
                tuple_(SourceObservation.provider_id, SourceObservation.record_hash).in_(wanted[start:start + 500]),
            ))
        found.update((row.provider_id, row.record_hash) for row in rows)
    return found


async def seen_provider_ids(
    session: AsyncSession, *, source_id: UUID, source_generation: int, provider_ids: Iterable[str],
) -> set[str]:
    """Return which ``provider_ids`` this source already observed in its current generation.

    Lets a crawler skip fetching detail pages for postings it has already stored, before any record (and so
    any content hash) exists. Read-only, scoped to the source and generation (a reset re-ingests everything),
    and bounded by the caller's candidate list (chunked under the bind-parameter limit).
    """
    wanted = list(set(provider_ids))
    found: set[str] = set()
    for start in range(0, len(wanted), 500):
        rows = await session.execute(
            select(SourceObservation.provider_id)
            .join(IngestionBatch, IngestionBatch.id == SourceObservation.batch_id)
            .where(
                SourceObservation.source_id == source_id,
                IngestionBatch.source_generation == source_generation,
                SourceObservation.provider_id.in_(wanted[start:start + 500]),
            ))
        found.update(row.provider_id for row in rows)
    return found


def record_key(record: IngestionRecord) -> tuple[str, str]:
    """Return the ``(provider_id, record_hash)`` identity stored for a generic-path record."""
    return record.provider_id, _digest({"version": record.version, "content": record.content, "metadata": record.metadata})


async def drop_seen_records(
    session: AsyncSession, *, source_id: UUID, source_generation: int, provider: str | None,
    records: list[IngestionRecord],
) -> list[IngestionRecord]:
    """Filter out generic-path records whose observation was already stored (see ``seen_record_keys``)."""
    seen = await seen_record_keys(
        session, source_id=source_id, source_generation=source_generation, provider=provider,
        keys=[record_key(record) for record in records])
    return [record for record in records if record_key(record) not in seen] if seen else records
