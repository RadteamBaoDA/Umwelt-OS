import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modules.connectors.github.schemas import GitHubSegmentProof

MAX_NATIVE_COLLECTION_BYTES = 10 * 1024 * 1024
MAX_TELEGRAM_CURSOR_BYTES = 16_384


def _require_aware_utc(value: datetime, field: str) -> datetime:
    """Require an aware instant and normalize it for stable persisted ordering."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


class IngestionRecord(BaseModel):
    """Validate one provider record and require a timezone-aware observation time."""
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=1_000_000)
    observed_at: datetime
    version: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime | None = None

    @field_validator("observed_at", "collected_at")
    @classmethod
    def require_aware_observation_time(cls, value: datetime | None, info: Any) -> datetime | None:
        """Normalize supplied observation and collection clocks to UTC instants."""
        return _require_aware_utc(value, info.field_name) if value is not None else value

    @field_validator("metadata")
    @classmethod
    def bounded_record_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Enforce the existing 64 KiB JSON metadata bound for every provider record."""
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 65_536:
            raise ValueError("record metadata exceeds 64 KiB")
        return value

class ReceiveBatch(BaseModel):
    """Validate a source-generation-fenced batch with bounded records and cursors."""
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    source_generation: int = Field(ge=1)
    connector_revision: int | None = Field(default=None, ge=1)
    batch_key: str = Field(min_length=1, max_length=255)
    cursor_before: str | None = Field(default=None, max_length=4096)
    cursor_after: str | None = Field(default=None, max_length=4096)
    records: list[IngestionRecord] = Field(min_length=1, max_length=500)

    @field_validator("cursor_before", "cursor_after")
    @classmethod
    def bound_cursor_bytes(cls, value: str | None) -> str | None:
        """Apply the public cursor UTF-8 byte limit rather than a character count."""
        if value is not None and len(value.encode("utf-8")) > 4096:
            raise ValueError("Cursor exceeds 4096 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def limit_serialized_payload(self) -> "ReceiveBatch":
        """Reject oversized batches and untrusted attempts to supply provider proof."""
        if any({"provider_record", "world_data", "_owner_telegram_proof", "_native_telegram"} & record.metadata.keys() for record in self.records):
            raise ValueError("provider_record metadata is reserved for trusted normalization")
        payload = json.dumps(self.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
        if len(payload.encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("Batch payload exceeds 10 MiB")
        return self


class Receipt(BaseModel):
    """Report durable batch and run identities with the run's current status."""
    batch_id: UUID
    run_id: UUID
    status: Literal["queued", "running", "succeeded", "needs_ocr", "failed"]


class RetryRunRequest(BaseModel):
    """Select the failed ingestion stage to retry."""
    model_config = ConfigDict(extra="forbid")
    stage_key: str = Field(min_length=1, max_length=128)


class CrawlReceipt(BaseModel):
    """Return the durable ingestion run created for a crawl request."""
    run_id: UUID


class EventDelivery(BaseModel):
    """Expose an outbox event's delivery status and payload."""
    id: UUID
    status: str
    payload: dict[str, Any]


class CollectorCredentialRead(BaseModel):
    """Return a newly issued collector token with its owning source ID."""
    source_id: UUID
    token: str


class StageRead(BaseModel):
    """Expose one ingestion stage's retry state and processing counts."""
    stage_key: str
    status: Literal["pending", "queued", "running", "retrying", "succeeded", "failed"]
    attempts: int
    error_code: str | None
    result_count: int | None = None
    normalized_count: int = 0
    duplicate_count: int = 0
    selected_current_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    pending_count: int = 0
    updated_at: datetime


class RunRead(BaseModel):
    """Serialize an ingestion run and the statuses of its stages."""
    run_id: UUID
    source_id: UUID
    status: Literal["queued", "running", "succeeded", "needs_ocr", "failed"]
    stages: list[StageRead]
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class SourceIngestionRead(BaseModel):
    """Return the current run and a page of ingestion history for a source."""
    current_run: RunRead | None
    items: list[RunRead]
    next_cursor: str | None


class ConnectorCollectionLease(BaseModel):
    """Detached source/revision reservation used across native provider I/O."""
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    token: UUID
    cursor_before: str | None = Field(default=None, max_length=16_384)
    expires_at: datetime

    @field_validator("cursor_before")
    @classmethod
    def bounded_reservation_cursor(cls, value: str | None) -> str | None:
        """Bound detached native reservation cursors by encoded UTF-8 size."""
        if value is not None and len(value.encode("utf-8")) > 16_384:
            raise ValueError("Cursor exceeds 16384 UTF-8 bytes")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        """Normalize lease expiry to UTC before comparing ownership."""
        return _require_aware_utc(value, "expires_at")


class TelegramDeliveryProof(BaseModel):
    """Bind one accepted Telegram update to its native bot and stream epoch."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    bot_id: str = Field(pattern=r"^[0-9]{1,20}$")
    epoch: int = Field(ge=1)
    update_id: int = Field(ge=0, le=2**63 - 1)
    raw_update_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TelegramPageDelivery(BaseModel):
    """Retain a bounded delivery hash ledger for exact Telegram replay checks."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    update_id: int = Field(ge=0, le=2**63 - 1)
    raw_update_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TelegramRawDelivery(BaseModel):
    """Carry one bounded Bot API update and its deterministic canonical digest."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    update_id: int = Field(ge=0, le=2**63 - 1)
    raw_update_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    update: dict[str, Any]

    @model_validator(mode="after")
    def validate_digest_and_bound(self) -> "TelegramRawDelivery":
        """Bind digest to canonical bounded bytes before owner classification."""
        encoded = json.dumps(self.update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_NATIVE_COLLECTION_BYTES or sha256(encoded).hexdigest() != self.raw_update_sha256:
            raise ValueError("Telegram update digest or size is invalid")
        return self


class TelegramCursor(BaseModel):
    """Persist Telegram stream epoch and the last durable delivery ledger."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["telegram_getupdates_v1"]
    bot_id: str = Field(pattern=r"^[0-9]{1,20}$")
    epoch: int = Field(ge=1)
    last_update_id: int = Field(ge=0, le=2**63 - 1)
    last_nonduplicate_received_at: datetime
    last_page_deliveries: list[TelegramPageDelivery] = Field(min_length=1, max_length=100)

    @field_validator("last_nonduplicate_received_at")
    @classmethod
    def aware_cursor_time(cls, value: datetime) -> datetime:
        """Normalize the durable nonduplicate receipt clock to UTC."""
        return _require_aware_utc(value, "last_nonduplicate_received_at")

    @model_validator(mode="after")
    def validate_ledger(self) -> "TelegramCursor":
        """Require the retained suffix to be strictly increasing and end at the cursor."""
        ids = [delivery.update_id for delivery in self.last_page_deliveries]
        if ids != sorted(set(ids)) or ids[-1] != self.last_update_id:
            raise ValueError("Telegram cursor ledger must be ordered and end at last_update_id")
        self.ensure_encoded_bound()
        return self

    def ensure_encoded_bound(self) -> None:
        """Enforce the UTF-8 cap on the exact persisted JSON cursor representation."""
        raw = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_TELEGRAM_CURSOR_BYTES:
            raise ValueError("Telegram cursor exceeds 16384 bytes")


class TelegramProbeClassification(BaseModel):
    """Describe the owner-classified cursor transition and replay set."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    epoch: int | None = Field(default=None, ge=1)
    delivery_proofs: tuple[TelegramDeliveryProof, ...] = Field(max_length=100)
    replay_update_ids: tuple[int, ...] = Field(max_length=100)
    cursor_after: TelegramCursor | None
    conflict_code: Literal["telegram_stream_conflict"] | None = None


class NativeCollectionBatch(BaseModel):
    """Carry owner-reserved provider output and immutable Telegram or GitHub collection proof."""
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    lease_token: UUID
    cursor_before: str | None = Field(default=None, max_length=16_384)
    cursor_after: str | None = Field(default=None, max_length=16_384)
    records: list[IngestionRecord] = Field(max_length=500)
    telegram_deliveries: tuple[TelegramDeliveryProof, ...] = Field(max_length=100)
    telegram_raw_deliveries: tuple[TelegramRawDelivery, ...] = Field(max_length=100)
    github_segment: GitHubSegmentProof | None = None
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"]
    collected_at: datetime

    @field_validator("cursor_before", "cursor_after")
    @classmethod
    def bounded_native_cursors(cls, value: str | None) -> str | None:
        """Bound native cursor values by UTF-8 bytes, including encoded Telegram JSON."""
        if value is not None and len(value.encode("utf-8")) > 16_384:
            raise ValueError("Cursor exceeds 16384 UTF-8 bytes")
        return value

    @field_validator("collected_at")
    @classmethod
    def aware_native_collection_time(cls, value: datetime) -> datetime:
        """Normalize collection time without including it in stable record identity."""
        return _require_aware_utc(value, "collected_at")

    @model_validator(mode="after")
    def bounded_native_payload(self) -> "NativeCollectionBatch":
        """Reject native transport payloads above the frozen 10 MiB bound."""
        encoded = json.dumps(self.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_NATIVE_COLLECTION_BYTES:
            raise ValueError("Native collection payload exceeds 10 MiB")
        return self


class NativeCollectionReceipt(BaseModel):
    """Return the immutable batch/run outcome and private persisted cursor."""
    model_config = ConfigDict(extra="forbid")
    batch_id: UUID | None
    run_id: UUID | None
    status: Literal["succeeded", "queued", "no_changes"]
    received_update_count: int = Field(ge=0, le=500)
    record_count: int = Field(ge=0, le=500)
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"]
    next_eligible_at: datetime | None = None
    cursor_after: str | None = Field(default=None, max_length=16_384)


def classify_telegram_probe(
    cursor: TelegramCursor | None,
    updates: tuple[TelegramRawDelivery, ...],
    *,
    received_at: datetime,
    verified_bot_id: str,
) -> TelegramProbeClassification:
    """Classify a fetched page into replay, epoch advance, or an explicit conflict.

    Exact duplicates within the retained 24-hour window keep the epoch and are
    marked as replay; expired deliveries begin a new local epoch. Unseen in-order
    suffixes extend a fresh durable ledger, while ambiguous mixed pages fail closed.
    """
    received_at = _require_aware_utc(received_at, "received_at")
    if not verified_bot_id.isdecimal() or len(verified_bot_id) > 20:
        raise ValueError("Verified Telegram bot identity is invalid")
    if cursor is not None and cursor.bot_id != verified_bot_id:
        return TelegramProbeClassification(
            epoch=cursor.epoch, delivery_proofs=(), replay_update_ids=(), cursor_after=cursor,
            conflict_code="telegram_stream_conflict",
        )
    if not updates:
        return TelegramProbeClassification(
            epoch=cursor.epoch if cursor else None,
            delivery_proofs=(), replay_update_ids=(), cursor_after=cursor,
        )
    page_ids = [item.update_id for item in updates]
    if page_ids != sorted(set(page_ids)):
        return TelegramProbeClassification(
            epoch=cursor.epoch if cursor else None,
            delivery_proofs=(), replay_update_ids=(), cursor_after=cursor,
            conflict_code="telegram_stream_conflict",
        )
    for item in updates:
        update_id = item.update.get("update_id")
        canonical = json.dumps(item.update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if (
            isinstance(update_id, bool) or update_id != item.update_id
            or sha256(canonical).hexdigest() != item.raw_update_sha256
        ):
            return TelegramProbeClassification(
                epoch=cursor.epoch if cursor else None,
                delivery_proofs=(), replay_update_ids=(), cursor_after=cursor,
                conflict_code="telegram_stream_conflict",
            )
    if cursor is None:
        epoch = 1
        replay_ids: list[int] = []
        ledger = [TelegramPageDelivery(
            update_id=item.update_id, raw_update_sha256=item.raw_update_sha256
        ) for item in updates]
    else:
        retained = {item.update_id: item.raw_update_sha256 for item in cursor.last_page_deliveries}
        known = [item for item in updates if retained.get(item.update_id) == item.raw_update_sha256]
        changed_known = [item for item in updates if item.update_id in retained and retained[item.update_id] != item.raw_update_sha256]
        unknown = [item for item in updates if item.update_id not in retained]
        replay_ids = [item.update_id for item in known]
        age = received_at - cursor.last_nonduplicate_received_at
        fresh = timedelta(0) <= age < timedelta(hours=24)
        has_mixed_unknown_lower = bool(known and any(item.update_id <= cursor.last_update_id for item in unknown))
        if has_mixed_unknown_lower or (known and changed_known):
            return TelegramProbeClassification(
                epoch=cursor.epoch, delivery_proofs=(), replay_update_ids=(), cursor_after=cursor,
                conflict_code="telegram_stream_conflict",
            )
        if not unknown and not changed_known and fresh:
            replay_ids = page_ids
            return TelegramProbeClassification(
            epoch=cursor.epoch,
                delivery_proofs=tuple(TelegramDeliveryProof(
                    bot_id=cursor.bot_id, epoch=cursor.epoch,
                    update_id=item.update_id, raw_update_sha256=item.raw_update_sha256,
                ) for item in updates),
                replay_update_ids=tuple(replay_ids), cursor_after=cursor,
            )
        all_new_higher = bool(unknown) and all(item.update_id > cursor.last_update_id for item in unknown) and not changed_known
        if all_new_higher and fresh:
            epoch = cursor.epoch
            ledger = [*cursor.last_page_deliveries, *(TelegramPageDelivery(
                update_id=item.update_id, raw_update_sha256=item.raw_update_sha256
            ) for item in unknown)][-100:]
        elif known and unknown and fresh:
            # Retained known deliveries may prefix a newly delivered suffix; only that suffix is new.
            if any(item.update_id <= cursor.last_update_id for item in unknown):
                return TelegramProbeClassification(
                    epoch=cursor.epoch, delivery_proofs=(), replay_update_ids=(), cursor_after=cursor,
                    conflict_code="telegram_stream_conflict",
                )
            epoch = cursor.epoch
            ledger = [*cursor.last_page_deliveries, *(TelegramPageDelivery(
                update_id=item.update_id, raw_update_sha256=item.raw_update_sha256
            ) for item in unknown)][-100:]
        else:
            # Reset after retention expiry or changed/older IDs starts a new local ordinal epoch.
            epoch = cursor.epoch + 1
            replay_ids = []
            ledger = [TelegramPageDelivery(update_id=item.update_id, raw_update_sha256=item.raw_update_sha256) for item in updates]
    bot_id = verified_bot_id
    cursor_after = TelegramCursor(
        kind="telegram_getupdates_v1", bot_id=bot_id, epoch=epoch,
        last_update_id=ledger[-1].update_id,
        last_nonduplicate_received_at=received_at,
        last_page_deliveries=ledger,
    )
    return TelegramProbeClassification(
        epoch=epoch,
        delivery_proofs=tuple(TelegramDeliveryProof(
            bot_id=bot_id, epoch=epoch, update_id=item.update_id,
            raw_update_sha256=item.raw_update_sha256,
        ) for item in updates),
        replay_update_ids=tuple(replay_ids), cursor_after=cursor_after,
    )
