import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

MAX_CONTENT_BYTES = 1_048_576
MAX_METADATA_BYTES = 65_536
NEWS_PROVIDER_IDS = ("bbc_world", "vnexpress_business", "hn_top", "gdelt_economy")
MEASUREMENT_PROVIDER_IDS = (
    "alpha_vantage", "open_meteo", "world_bank", "frankfurter", "ecb", "binance",
    "alternative_me", "usgs", "coinpaprika", "coingecko",
)
PROVIDER_IDS = (
    "youtube", "arxiv", "huggingface", "github_releases", "github", "telegram",
    *MEASUREMENT_PROVIDER_IDS, *NEWS_PROVIDER_IDS,
)
# Bounded per-provider evidence keys retained beside the typed measurement (no arbitrary response mirroring).
MEASUREMENT_PROVIDER_FIELDS = {
    "alpha_vantage": {"date", "symbol", "open", "high", "low", "close", "volume"},
    "open_meteo": {"time", "timezone", "utc_offset_seconds", "latitude", "longitude"},
    "world_bank": {"country", "indicator", "period", "unit", "decimal_value"},
    "frankfurter": {"date", "base", "quote", "decimal_value", "providers"},
    "ecb": {"date", "base", "quote", "decimal_value"},
    "binance": {"symbol", "base", "quote", "decimal_value"},
    "alternative_me": {"timestamp", "value_classification", "decimal_value"},
    "usgs": {"place", "type", "status", "magType", "depth_km", "latitude", "longitude", "decimal_mag", "event_time", "updated"},
    "coinpaprika": {"id", "symbol", "last_updated", "quote", "decimal_value"},
    "coingecko": {"coin_id", "quote", "decimal_value", "last_updated_at"},
}


_FEED_FIELDS = {"title", "summary", "canonical_url", "guid", "published_at", "provider_updated_at", "publisher"}


class WorldDataMeasurement(BaseModel):
    """Preserve one provider-declared numeric value with its original measurement semantics."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal[
        "alpha_vantage", "open_meteo", "world_bank", "frankfurter", "ecb", "binance",
        "alternative_me", "usgs", "coinpaprika", "coingecko",
    ]
    metric: str = Field(min_length=1, max_length=80)
    value: float | None
    unit: str = Field(min_length=1, max_length=64)
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    timezone: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    published_at: datetime | None = None
    quality: Literal["provider_reported", "forecast", "reference", "missing"]
    missing_reason: str | None = Field(default=None, max_length=64)
    provider_fields: dict[str, str | float | int | None] = Field(default_factory=dict, max_length=12)

    @field_validator("value")
    @classmethod
    def finite_measurement(cls, value: float | None) -> float | None:
        """Reject NaN and infinity before values reach PostgreSQL or a renderer."""
        import math

        if value is not None and not math.isfinite(value):
            raise ValueError("Measurement value must be finite")
        return value

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "WorldDataMeasurement":
        """Keep retained source-row evidence inside each provider's declared field set."""
        if self.provider_fields.keys() - MEASUREMENT_PROVIDER_FIELDS[self.provider]:
            raise ValueError("Structured provider row contains undeclared fields")
        if (self.value is None) != (self.quality == "missing") or (self.value is None) != (self.missing_reason is not None):
            raise ValueError("Missing values must carry an explicit missing reason and quality")
        return self


class DocumentDeletionRead(BaseModel):
    """Expose durable document-deletion progress without revealing storage paths."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    record_status: Literal["deleted"]
    graph_status: Literal["tombstoned"]
    raw_status: Literal["queued", "not_present", "retained_shared", "succeeded", "failed"]
    evidence_scope_status: Literal["capturing", "captured", "unavailable"]
    copied_status: Literal["queued", "running", "succeeded", "failed"]
    chat_status: Literal["queued", "running", "succeeded", "failed"]
    chat_error_code: str | None = Field(default=None, max_length=64)
    memory_status: Literal["queued", "running", "succeeded", "failed"]
    memory_error_code: str | None = Field(default=None, max_length=64)
    memory_unresolved_count: int = Field(ge=0)
    memory_cache_pending: bool
    agent_status: Literal["queued", "running", "succeeded", "failed"]
    agent_error_code: str | None = Field(default=None, max_length=64)
    agent_unresolved_count: int = Field(ge=0)
    agent_waiting_for_lease: bool
    materialization_status: Literal["queued", "running", "succeeded", "failed"]
    materialization_error_code: str | None = Field(default=None, max_length=64)
    materialization_unresolved_count: int = Field(ge=0)
    brief_status: Literal["queued", "running", "succeeded", "failed"]
    brief_error_code: str | None = Field(default=None, max_length=64)
    brief_unresolved_count: int = Field(ge=0)
    immediate_access_revoked: Literal[True] = True
    error_code: str | None = Field(default=None, max_length=64)
    copied_error_code: str | None = Field(default=None, max_length=64)


class ProviderTelegramMedia(BaseModel):
    """Describe safe media presence without requesting or storing binary data."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["photo", "video", "audio", "voice", "document", "animation", "sticker", "other"]
    caption: str | None = Field(default=None, max_length=4096)
    count: int = Field(ge=1, le=100)
    file_id: str | None = Field(default=None, max_length=512)


class ProviderTelegramMetadata(BaseModel):
    """Validate the bounded Telegram identity and presentation snapshot."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    bot_id: str = Field(pattern=r"^[0-9]{1,20}$")
    channel_id: str = Field(pattern=r"^-?[1-9][0-9]{0,19}$")
    message_id: str = Field(pattern=r"^[0-9]{1,20}$")
    thread_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    reply_to_message_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    channel_label: str | None = Field(default=None, max_length=255)
    channel_username: str | None = Field(default=None, max_length=64)
    epoch: int = Field(ge=1)
    update_id: int = Field(ge=0, le=2**63 - 1)
    raw_update_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    edited_received: bool
    published_at: datetime
    edited_at: datetime | None = None
    media: list[ProviderTelegramMedia] = Field(default_factory=list, max_length=20)

    @field_validator("published_at", "edited_at")
    @classmethod
    def aware_telegram_time(cls, value: datetime | None) -> datetime | None:
        """Require aware Telegram source timestamps and normalize them to UTC."""
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Telegram timestamps must include a timezone")
        return value.astimezone(UTC)


class ProviderRecordMetadata(BaseModel):
    """Retain typed, bounded provider fields separately from generic metadata."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal[
        "youtube", "arxiv", "huggingface", "github_releases", "github", "telegram",
        "alpha_vantage", "open_meteo", "world_bank", "frankfurter", "ecb", "binance",
        "alternative_me", "usgs", "coinpaprika", "coingecko",
        "bbc_world", "vnexpress_business", "hn_top", "gdelt_economy",
    ]
    identity: str = Field(min_length=1, max_length=512)
    provider_version: str | None = Field(default=None, max_length=255)
    timestamp_basis: Literal["provider_modified", "provider_published", "collection"]
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"]
    content_truncated: bool
    provider_modified_at: datetime | None = None
    license_label: str | None = Field(default=None, max_length=255)
    source_fields: dict[str, Any] = Field(default_factory=dict)
    telegram: ProviderTelegramMetadata | None = None
    world_data: WorldDataMeasurement | None = None

    @field_validator("provider_modified_at")
    @classmethod
    def aware_provider_modified_time(cls, value: datetime | None) -> datetime | None:
        """Normalize a genuine provider modification clock without inventing one."""
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("provider_modified_at must include a timezone")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def validate_provider_snapshot_fields(self) -> "ProviderRecordMetadata":
        """Enforce provider-specific allowlists and require Telegram's typed identity block."""
        if (self.provider == "telegram") != (self.telegram is not None):
            raise ValueError("Telegram detail must match provider")
        if self.provider in MEASUREMENT_PROVIDER_IDS:
            if self.world_data is None or self.world_data.provider != self.provider:
                raise ValueError("World-data measurement must match provider")
        elif self.world_data is not None:
            raise ValueError("World-data metadata is reserved for structured providers")
        allowed = {
            "youtube": {"author", "summary", "title", "tags", "published_at", "provider_updated_at"},
            "arxiv": {"author", "authors", "categories", "tags", "summary", "title", "published_at", "provider_updated_at"},
            "huggingface": {"author", "tags", "pipeline_tag", "created_at", "last_modified"},
            "github_releases": {"node_id", "name", "body", "html_url", "tag_name", "draft", "prerelease", "author", "created_at", "published_at"},
            "github": {"record_type", "node_id", "html_url"},
            "telegram": set(),
            **{provider: set() for provider in MEASUREMENT_PROVIDER_IDS},
            "bbc_world": _FEED_FIELDS, "vnexpress_business": _FEED_FIELDS,
            "hn_top": {"title", "text", "url", "by", "time", "id", "type", "deleted", "dead"},
            "gdelt_economy": {"title", "url", "seendate", "domain", "language", "sourcecountry"},
        }[self.provider]
        if self.source_fields.keys() - allowed:
            raise ValueError("Provider snapshot contains unsupported source fields")
        string_limits = {
            "author": 1000, "summary": 4000, "title": 500, "pipeline_tag": 128,
            "node_id": 256, "name": 500, "body": 4000, "html_url": 2048,
            "tag_name": 255, "record_type": 16, "last_modified": 64, "created_at": 64,
            "published_at": 64, "provider_updated_at": 64,
            "canonical_url": 2048, "guid": 512, "publisher": 200, "text": 4000, "url": 2048, "by": 128,
            "type": 32, "seendate": 32, "domain": 255, "language": 64, "sourcecountry": 64,
        }
        for key, value in self.source_fields.items():
            if key in {"tags", "categories", "authors"}:
                if not isinstance(value, list) or len(value) > 100 or any(
                    not isinstance(item, str) or len(item) > 255 for item in value
                ):
                    raise ValueError(f"Provider {key} must be a bounded string list")
            elif key in {"draft", "prerelease", "deleted", "dead"}:
                if not isinstance(value, bool):
                    raise ValueError(f"Provider {key} must be boolean")
            elif key in {"time", "id"}:
                if type(value) is not int or value < 0:
                    raise ValueError(f"Provider {key} must be a non-negative integer")
            elif key in {"canonical_url", "url"}:
                from urllib.parse import urlsplit

                parsed_link = urlsplit(value) if isinstance(value, str) and len(value) <= 2048 else None
                if parsed_link is None or parsed_link.scheme != "https" or not parsed_link.hostname or parsed_link.username or parsed_link.password:
                    raise ValueError(f"Provider {key} must be an HTTPS link without credentials")
            elif key == "html_url":
                if not isinstance(value, str) or len(value) > 2048:
                    raise ValueError("Provider release URL must use HTTPS")
                from urllib.parse import urlsplit

                parsed_url = urlsplit(value)
                if parsed_url.scheme != "https" or parsed_url.netloc != "github.com" or parsed_url.username or parsed_url.password:
                    raise ValueError("Provider release URL must target github.com")
            elif key in string_limits:
                if not isinstance(value, str) or len(value) > string_limits[key]:
                    raise ValueError(f"Provider {key} must be a bounded string")
                if key in {"created_at", "published_at", "last_modified", "provider_updated_at"}:
                    try:
                        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
                    except ValueError as exc:
                        raise ValueError(f"Provider {key} must be an ISO timestamp") from exc
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError(f"Provider {key} must include a timezone")
            else:
                raise ValueError("Provider snapshot field is unsupported")
        return self


def validate_content(value: str) -> str:
    """Reject content whose UTF-8 representation exceeds the 1 MiB request bound."""
    if len(value.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("content exceeds 1 MiB")
    return value


def validate_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Reject non-finite or non-JSON metadata and enforce the 64 KiB encoded limit."""
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds 64 KiB")
    return value


class DocumentCreate(BaseModel):
    """Validate a source-backed document and its initial content and metadata."""
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    title: str = Field(min_length=1, max_length=500)
    content: str
    external_id: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        """Apply the document content byte limit before persistence."""
        return validate_content(value)

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Apply the shared JSON metadata size and finiteness checks."""
        return validate_metadata(value)


class TelegramDocumentOrder(BaseModel):
    """Carry trusted Telegram order from owner-validated delivery proof."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    bot_id: str = Field(pattern=r"^[0-9]{1,20}$")
    epoch: int = Field(ge=1)
    update_id: int = Field(ge=0, le=2**63 - 1)


class NormalizedDocumentInput(BaseModel):
    """Validate normalized content, immutable provenance, and optional trusted Telegram order.

    Collector-normalized payloads are bounded and source-generation fenced. Telegram
    ordering must agree with the retained typed delivery digest, bot, epoch, and
    update ID; callers cannot create that ordering through public document inputs.
    """
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    expected_source_generation: int = Field(ge=1)
    observation_id: UUID
    provider_id: str = Field(min_length=1, max_length=512)
    provider_version: str | None = Field(default=None, max_length=255)
    accepted_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_version: int = Field(ge=1)
    observed_at: datetime
    received_at: datetime | None = None
    collected_at: datetime | None = None
    title: str = Field(min_length=1, max_length=500)
    canonical_url: str | None = None
    published_at: datetime | None = None
    content_type: str | None = Field(default=None, max_length=64)
    content: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    telegram_order: TelegramDocumentOrder | None = None

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        """Apply the content byte limit to normalized collector content."""
        return validate_content(value)

    @field_validator("observed_at", "received_at", "collected_at", "published_at")
    @classmethod
    def require_aware_times(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware timestamps and normalize supplied values to UTC."""
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("normalized timestamps must include a timezone")
            return value.astimezone(UTC)
        return value

    @field_validator("provenance")
    @classmethod
    def bounded_provenance(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Allow only declared provenance fields and enforce the metadata bound."""
        allowed = {"title", "canonical_url", "published_at", "content_type", "author", "language", "metadata", "provider_record", "provider_scope_discriminator"}
        if value.keys() - allowed:
            raise ValueError("provenance contains unsupported fields")
        if "provider_record" in value:
            value["provider_record"] = ProviderRecordMetadata.model_validate(value["provider_record"]).model_dump(mode="json")
        return validate_metadata(value)

    @model_validator(mode="after")
    def verify_telegram_order_provenance(self) -> "NormalizedDocumentInput":
        """Require the document ordering key to match its retained Telegram proof."""
        raw = self.provenance.get("provider_record")
        if raw is None:
            if self.telegram_order is not None:
                raise ValueError("Telegram order requires typed provider provenance")
            return self
        metadata = ProviderRecordMetadata.model_validate(raw)
        detail = metadata.telegram
        if metadata.provider == "telegram":
            if self.telegram_order is None or detail is None or (
                self.telegram_order.bot_id != detail.bot_id
                or self.telegram_order.epoch != detail.epoch
                or self.telegram_order.update_id != detail.update_id
                or detail.raw_update_sha256 is None
            ):
                raise ValueError("Telegram order must match the immutable delivery proof")
        elif self.telegram_order is not None:
            raise ValueError("Telegram order is only valid for Telegram provenance")
        return self


class NormalizedDocumentKeyState(BaseModel):
    """Detached exact-key comparison state, including identity absence and tombstones.

    No field proves a retained lock or grants permission. UUIDs and key spelling are
    strict; an absent identity cannot carry a pointer or deletion timestamp.
    """
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    external_id: str = Field(min_length=1, max_length=512)
    document_id: UUID | None
    normalized_identity_id: UUID | None
    identity_document_id: UUID | None
    tombstoned_at: datetime | None

    @model_validator(mode="after")
    def validate_identity_shape(self) -> "NormalizedDocumentKeyState":
        """Reject fabricated absent-identity pointers and naive deletion timestamps."""
        if self.normalized_identity_id is None and (
            self.identity_document_id is not None or self.tombstoned_at is not None
        ):
            raise ValueError("Absent normalized identity cannot carry retained fields")
        if self.tombstoned_at is not None and (
            self.tombstoned_at.tzinfo is None or self.tombstoned_at.utcoffset() is None
        ):
            raise ValueError("Normalized tombstone must be timezone aware")
        return self


class NormalizedDocumentPreparation(BaseModel):
    """Immutable bounded comparisons for one transaction's prepared Source keys.

    Only Documents produces successor identity state after its exact local writes;
    callers discard it on commit/rollback and retain actual ordered locks separately.
    """
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    workspace_id: UUID
    source_id: UUID
    source_generation: int = Field(ge=1)
    keys: tuple[NormalizedDocumentKeyState, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_unique_keys(self) -> "NormalizedDocumentPreparation":
        """Require one comparison per exact external key; empty preparation grants no writes."""
        if len({key.external_id for key in self.keys}) != len(self.keys):
            raise ValueError("Normalized preparation keys must be unique")
        return self


class NormalizedDocumentResult(BaseModel):
    """Report normalization disposition, selected revision, and generated chunks."""
    disposition: Literal["normalized", "duplicate", "tombstoned"]
    document_id: UUID | None
    document_version_id: UUID | None
    version_number: int | None
    created_version: bool
    selected_current: bool
    chunk_count: int


class ProviderDocumentSnapshotRead(BaseModel):
    """Expose immutable provider provenance for one exact document version."""
    model_config = ConfigDict(extra="forbid")
    document_id: UUID
    document_version_id: UUID
    version_number: int = Field(ge=1)
    source_id: UUID
    source_status: Literal["active", "paused"]
    provider_id: str = Field(max_length=512)
    provider_version: str | None = Field(default=None, max_length=255)
    title: str = Field(max_length=500)
    canonical_url: str | None
    published_at: datetime | None
    observed_at: datetime
    received_at: datetime | None
    collected_at: datetime | None
    excerpt: str = Field(max_length=1000)
    provider_metadata: ProviderRecordMetadata | None
    metadata_is_version_snapshot: bool


class ObservationExportEvidenceCandidate(BaseModel):
    """Identify a retained observation's accepted document and provider provenance."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    observation_id: UUID
    source_id: UUID
    accepted_source_generation: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=64)
    provider_scope_discriminator: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_id: str = Field(min_length=1, max_length=512)
    document_id: UUID
    document_version_id: UUID


class ObservationExportEvidenceRead(BaseModel):
    """Confirm exact retained document evidence and captured current source generation."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    observation_id: UUID
    source_id: UUID
    current_source_generation: int = Field(ge=1)
    accepted_source_generation: int = Field(ge=1)
    document_id: UUID
    document_version_id: UUID
    document_version_number: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=64)
    provider_scope_discriminator: str = Field(pattern=r"^[0-9a-f]{64}$")


class TimelineExportEvidenceCandidate(BaseModel):
    """Identify one exact event evidence reference and its accepted source generation."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_id: UUID
    source_id: UUID
    accepted_source_generation: int = Field(ge=1)
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID


class TimelineExportEvidenceRead(BaseModel):
    """Confirm retained exact event evidence and separate accepted/current source generations."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_id: UUID
    source_id: UUID
    accepted_source_generation: int = Field(ge=1)
    current_source_generation: int = Field(ge=1)
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID


class ProviderDocumentSnapshotList(BaseModel):
    """Return a bounded current-version owner snapshot page."""
    model_config = ConfigDict(extra="forbid")
    items: list[ProviderDocumentSnapshotRead]
    next_cursor: str | None


class GadgetTelegramMediaRead(BaseModel):
    """Expose Telegram media kind and caption without provider file identifiers."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["photo", "video", "audio", "voice", "document", "animation", "sticker", "other"]
    caption: str | None = Field(default=None, max_length=4096)
    count: int = Field(ge=1, le=100)


class GadgetTelegramRecordRead(BaseModel):
    """Expose channel and message display metadata without bot or delivery internals."""
    model_config = ConfigDict(extra="forbid")
    channel_id: str
    message_id: str
    thread_id: str | None
    reply_to_message_id: str | None
    channel_label: str | None
    channel_username: str | None
    edited_received: bool
    published_at: datetime
    edited_at: datetime | None
    media: list[GadgetTelegramMediaRead] = Field(max_length=20)


class GadgetProviderMetadataRead(BaseModel):
    """Expose only typed provider fields needed to render owner dashboard records."""
    model_config = ConfigDict(extra="forbid")
    provider: Literal["youtube", "arxiv", "huggingface", "github_releases", "github", "telegram"]
    source_fields: dict[str, Any]
    telegram: GadgetTelegramRecordRead | None = None


class GadgetDocumentProjectionRead(BaseModel):
    """Bounded current-version document data for source-scoped dashboard consumers."""
    model_config = ConfigDict(extra="forbid")
    document_id: UUID
    document_version_id: UUID
    version_number: int = Field(ge=1)
    source_id: UUID
    title: str = Field(max_length=500)
    canonical_url: str | None
    published_at: datetime | None
    observed_at: datetime
    excerpt: str = Field(max_length=2000)
    provider_metadata: GadgetProviderMetadataRead | None
    metadata_is_version_snapshot: bool
    read_at: datetime | None
    bookmarked_at: datetime | None
    dismissed_at: datetime | None = None


class GadgetDocumentSelectionFence(BaseModel):
    """Bind one exact selected version to its accepted source generation and provider scope."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    document_id: UUID
    document_version_id: UUID
    source_id: UUID
    source_generation: int = Field(ge=0)
    source_type: str = Field(min_length=1, max_length=64)
    provider: str | None = Field(default=None, max_length=64)
    local_only: bool
    scope_discriminator: str | None = Field(default=None, min_length=64, max_length=64)


class GadgetHighlightProjectionPage(BaseModel):
    """Return one bounded current-version scan page and its durable immutable-version cursor."""
    model_config = ConfigDict(extra="forbid")
    items: list[GadgetDocumentProjectionRead] = Field(max_length=100)
    selection_fences: list[GadgetDocumentSelectionFence] = Field(max_length=100)
    cursor_created_at: datetime | None
    cursor_version_id: UUID | None
    has_more: bool


class GadgetDocumentInteractionPatch(BaseModel):
    """Set durable read/bookmark state for a selected current version."""
    model_config = ConfigDict(extra="forbid")
    read: StrictBool | None = None
    bookmarked: StrictBool | None = None
    dismissed: StrictBool | None = None

    @model_validator(mode="after")
    def require_interaction_change(self) -> "GadgetDocumentInteractionPatch":
        """Require at least one state field to avoid ambiguous empty writes."""
        if self.read is None and self.bookmarked is None and self.dismissed is None:
            raise ValueError("At least one interaction state is required")
        return self


class GadgetDocumentInteractionRead(BaseModel):
    """Return durable interaction timestamps for one exact version."""
    model_config = ConfigDict(extra="forbid")
    document_version_id: UUID
    read_at: datetime | None
    bookmarked_at: datetime | None
    dismissed_at: datetime | None = None


class GadgetDocumentProjectionList(BaseModel):
    """Page a bounded set of current document projections for owner dashboard use."""
    model_config = ConfigDict(extra="forbid")
    items: list[GadgetDocumentProjectionRead]
    next_cursor: str | None


class ProviderSnapshotRequest(BaseModel):
    """Validate an exact-version owner snapshot request."""
    model_config = ConfigDict(extra="forbid")
    version_ids: list[UUID] = Field(min_length=1, max_length=100)

    @field_validator("version_ids")
    @classmethod
    def unique_version_ids(cls, value: list[UUID]) -> list[UUID]:
        """Reject repeated version IDs so result order remains unambiguous."""
        if len(value) != len(set(value)):
            raise ValueError("version_ids must be unique")
        return value


class DocumentPatch(BaseModel):
    """Validate mutable document metadata fields while leaving versions immutable."""
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Bound replacement metadata and preserve a missing or null update."""
        return validate_metadata(value) if value is not None else value


class ContentUpdate(BaseModel):
    """Request a new immutable content version using its expected current number."""
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    content: str

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        """Apply the document content byte limit to the new version."""
        return validate_content(value)


class DocumentRead(BaseModel):
    """Serialize a document's current metadata, provenance, and version state."""
    id: UUID
    source_id: UUID
    external_id: str | None
    title: str
    content_type: str | None
    mime_type: str | None
    raw_uri: str | None
    canonical_url: str | None
    author: str | None
    metadata: dict[str, Any]
    current_version: int
    content_hash: str
    extraction_status: str
    published_at: datetime | None
    observed_at: datetime | None
    language: str | None
    created_at: datetime
    updated_at: datetime


class VersionRead(BaseModel):
    """Serialize immutable content and identity fields for one document version."""
    id: UUID
    document_id: UUID
    version_number: int
    content: str
    content_hash: str
    observed_at: datetime
    created_at: datetime


class VersionList(BaseModel):
    """Return document versions with an optional continuation cursor."""
    items: list[VersionRead]
    next_cursor: str | None


class DocumentExportProvenance(BaseModel):
    """Expose version-scoped collection provenance without raw provider payloads or digests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=512)
    provider_version: str | None = Field(default=None, max_length=255)
    normalization_version: int = Field(ge=1)
    accepted_source_generation: int = Field(ge=1)
    observed_at: datetime
    received_at: datetime | None = None
    collected_at: datetime | None = None
    selection_observed_at: datetime
    title: str = Field(max_length=500)
    canonical_url: str | None = Field(default=None, max_length=2048)
    published_at: datetime | None = None
    content_type: str | None = Field(default=None, max_length=64)


class DocumentExportRead(BaseModel):
    """Expose the safe owner-visible identity and current-revision marker of a document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: Literal["document"] = "document"
    id: UUID
    source_id: UUID
    source_status: Literal["active", "paused", "archived"]
    current_source_generation: int = Field(ge=1)
    current_version: int = Field(ge=1)
    current_version_accepted_generation: int | None = Field(default=None, ge=1)
    title: str = Field(max_length=500)
    content_type: str | None = Field(default=None, max_length=64)
    mime_type: str | None = Field(default=None, max_length=255)
    canonical_url: str | None = Field(default=None, max_length=2048)
    created_at: datetime
    updated_at: datetime


class DocumentVersionExportRead(BaseModel):
    """Expose one retained immutable revision with safe provenance and live source fences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_kind: Literal["version"] = "version"
    id: UUID
    document_id: UUID
    source_id: UUID
    source_status: Literal["active", "paused", "archived"]
    current_source_generation: int = Field(ge=1)
    version_number: int = Field(ge=1)
    is_current_version: bool
    content: str
    observed_at: datetime
    created_at: datetime
    provenance: DocumentExportProvenance | None = None
    # Owner interaction state (read/saved/hidden) for this exact version.
    read_at: datetime | None = None
    bookmarked_at: datetime | None = None
    dismissed_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def bounded_export_content(cls, value: str) -> str:
        """Keep any one version payload within the document owner's established byte bound."""
        if len(value.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("Document version exceeds the export content bound")
        return value


class DocumentExportFence(BaseModel):
    """Bind a document or immutable revision to live identity and source-generation state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    document_created_at: datetime
    document_updated_at: datetime
    source_id: UUID
    source_status: Literal["active", "paused", "archived"]
    current_source_generation: int = Field(ge=1)
    document_current_version: int | None = Field(default=None, ge=1)
    version_id: UUID | None = None
    version_number: int | None = Field(default=None, ge=1)
    version_created_at: datetime | None = None
    # This digest is a validation-only fence and is excluded from exported record DTOs.
    version_content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DocumentExportPage(BaseModel):
    """Return one bounded owner-authorized document or version export page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: int = Field(ge=1)
    record_kind: Literal["documents", "versions"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[DocumentExportRead | DocumentVersionExportRead] = Field(max_length=100)
    fences: list[DocumentExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: None = None


class DocumentExportFenceValidation(BaseModel):
    """Report whether captured document/source fences remain valid at finalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    reason: Literal[
        "valid", "owner_unavailable", "snapshot_count_changed", "record_changed",
        "source_generation_changed", "evidence_unavailable",
    ]
    observed_snapshot_count: int = Field(ge=0)


class EvidenceReferenceRead(BaseModel):
    """Expose exact versioned chunk provenance and excerpt for evidence review."""
    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    source_id: UUID
    title: str
    canonical_url: str | None
    metadata_is_version_snapshot: bool
    observed_at: datetime
    excerpt: str


class CitationTargetRead(BaseModel):
    """Resolve an owner-visible citation chunk to its exact immutable version and excerpt."""

    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    title: str
    excerpt: str
    observed_at: datetime


class MemberDocumentRead(BaseModel):
    """Member view of a shared Document: no Source, storage URI, external id or free-form metadata."""
    id: UUID
    title: str
    content_type: str | None
    mime_type: str | None
    canonical_url: str | None
    author: str | None
    current_version: int
    content_hash: str
    extraction_status: str
    published_at: datetime | None
    observed_at: datetime | None
    language: str | None
    has_raw: bool
    created_at: datetime
    updated_at: datetime


class MemberDocumentList(BaseModel):
    """Bounded member page of shared Documents."""
    items: list[MemberDocumentRead]
    next_cursor: str | None


class DocumentList(BaseModel):
    """Return a bounded document page and its optional continuation cursor."""
    items: list[DocumentRead]
    next_cursor: str | None


class DocumentCleanupJobIdentity(BaseModel):
    """Exact retained Document cleanup authority tuple; all epochs are captured at creation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: UUID
    workspace_id: UUID
    actor_user_id: StrictInt = Field(gt=0)
    membership_revision: StrictInt = Field(gt=0)
    configuration_revision: StrictInt = Field(gt=0)
    source_id: UUID
    source_generation: StrictInt = Field(gt=0)
    document_id: UUID


class DocumentCleanupPreparationLimitError(Exception):
    """A cleanup closure owner exceeded its atomic bound; the message is the owner code only."""

    OWNER_CODES = frozenset({
        "documents", "observations", "entities", "relationships", "timeline", "temporal", "ingestion",
    })

    def __init__(self, owner_code: str) -> None:
        if owner_code not in self.OWNER_CODES:
            raise ValueError("Unknown cleanup owner code")
        super().__init__(owner_code)
        self.owner_code = owner_code
