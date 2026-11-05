"""Pure metadata normalization and the provider metadata extractor registry (internal).

``_normalize_common_metadata`` turns bounded raw record metadata into the provider-independent document fields.
Provider-specific provenance (typed ``provider_record`` envelopes, Telegram delivery proof, structured
measurements) is produced by a :class:`ProviderMetadataExtractor` looked up by the source provider, so adding a
provider is a registration instead of another branch in the normalization worker.

Nothing here performs I/O; every function is deterministic for its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

from modules.ingestion._native_records import validate_telegram_record_delivery
from modules.ingestion.schemas import IngestionRecord, TelegramDeliveryProof, TelegramRawDelivery
from modules.knowledge.documents.public import (
    MEASUREMENT_PROVIDER_IDS,
    NEWS_PROVIDER_IDS,
    ProviderRecordMetadata,
    TelegramDocumentOrder,
    WorldDataMeasurement,
)

# Raw-metadata keys each typed provider may contribute to ``source_fields`` (the explicit allowlist).
PROVIDER_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "youtube": ("author", "summary", "title", "tags", "published_at", "provider_updated_at"),
    "arxiv": (
        "author",
        "authors",
        "categories",
        "tags",
        "summary",
        "title",
        "published_at",
        "provider_updated_at",
    ),
    "huggingface": ("author", "tags", "pipeline_tag", "created_at", "last_modified"),
    "bluesky": ("author", "canonical_url", "created_at"),
    "reddit": ("title", "subreddit", "author", "canonical_url", "created_at"),
    "mastodon": ("author", "canonical_url", "created_at"),
    "x": ("author_id", "canonical_url", "created_at", "language"),
    "google_mail": ("thread_id", "history_id", "subject", "from", "to", "date_header"),
    "google_calendar": (
        "calendar_id",
        "event_id",
        "status",
        "start",
        "end",
        "start_timezone",
        "end_timezone",
        "recurrence",
        "html_link",
    ),
    "google_drive": ("name", "mime_type", "modified_time", "web_view_link", "md5_checksum"),
    "notion": ("title", "canonical_url"),
    "slack": ("channel_id", "message_ts", "thread_ts", "user_id"),
    "health": ("patient_id", "observation_id", "value", "unit", "observed_at_source"),
    "home_assistant": (
        "entity_id",
        "state",
        "observed_at_source",
        "unit_of_measurement",
        "friendly_name",
        "latitude",
        "longitude",
    ),
    "github_releases": (
        "node_id",
        "name",
        "body",
        "html_url",
        "tag_name",
        "draft",
        "prerelease",
        "author",
        "created_at",
        "published_at",
    ),
    "github": ("record_type", "node_id", "html_url"),
    "telegram": (),
    **dict.fromkeys(
        (*MEASUREMENT_PROVIDER_IDS, *NEWS_PROVIDER_IDS), ()
    ),  # envelope source_fields only
}

# Native providers whose accepted records must always carry a typed ``provider_record`` envelope.
ENVELOPE_REQUIRED_PROVIDERS = (
    "youtube",
    "arxiv",
    "huggingface",
    "bluesky",
    "reddit",
    "mastodon",
    "x",
    "github_releases",
    "github",
    *NEWS_PROVIDER_IDS,
)


@dataclass(frozen=True)
class CommonMetadata:
    """Provider-independent document fields derived from raw record metadata."""

    title: str
    canonical_url: str | None
    published_at: datetime | None
    content_type: str | None
    safe_metadata: dict[str, object]


def _bounded_title(raw_metadata: dict[str, Any], provider_id: str) -> str:
    """Return the trimmed title (500 chars) or the provider id when the title is absent or blank."""
    title_value = raw_metadata.get("title")
    if isinstance(title_value, str) and title_value.strip():
        return title_value.strip()[:500]
    return provider_id[:500]


def _http_url(raw_metadata: dict[str, Any]) -> str | None:
    """Return ``canonical_url`` (else ``url``) when it is an http(s) URL without credentials, capped at 2048."""
    raw_url = raw_metadata.get("canonical_url", raw_metadata.get("url"))
    if not isinstance(raw_url, str):
        return None
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
    ):
        return raw_url[:2048]
    return None


def _parse_published_at(raw_metadata: dict[str, Any]) -> datetime | None:
    """Parse ``published_at`` as an ISO instant in UTC; naive values are UTC and malformed values are ``None``."""
    published_value = raw_metadata.get("published_at")
    if not (isinstance(published_value, str) and published_value):
        return None
    try:
        # ``Z`` is rewritten explicitly so the parse is identical on every supported interpreter.
        published_at = datetime.fromisoformat(published_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return published_at.astimezone(UTC)


def _safe_metadata(raw_metadata: dict[str, Any]) -> dict[str, object]:
    """Keep only allowlisted descriptive fields, truncated to their limits (strings, tag/category lists)."""
    safe: dict[str, object] = {}
    for key, limit in (
        ("author", 500),
        ("language", 32),
        ("summary", 10_000),
        ("description", 10_000),
    ):
        value = raw_metadata.get(key)
        if isinstance(value, str):
            safe[key] = value[:limit]
    for key in ("tags", "categories"):
        value = raw_metadata.get(key)
        if isinstance(value, str):
            safe[key] = value[:2_000]
        elif isinstance(value, list):
            safe[key] = [item[:500] for item in value[:100] if isinstance(item, str)]
    return safe


def _normalize_common_metadata(raw_metadata: dict[str, Any], provider_id: str) -> CommonMetadata:
    """Derive title, canonical URL, publication time, content type and safe metadata from raw metadata.

    Pure and total: malformed or oversized values are dropped or truncated, never raised. The title falls back
    to the provider id; only credential-free http(s) URLs survive; ``content_type`` is capped at 64 characters.
    """
    content_type = raw_metadata.get("content_type")
    return CommonMetadata(
        title=_bounded_title(raw_metadata, provider_id),
        canonical_url=_http_url(raw_metadata),
        published_at=_parse_published_at(raw_metadata),
        content_type=content_type[:64] if isinstance(content_type, str) else None,
        safe_metadata=_safe_metadata(raw_metadata),
    )


@dataclass(frozen=True)
class ExtractionInput:
    """Everything an extractor may read about one accepted record (no session, no locks)."""

    record: IngestionRecord
    observed_at: datetime
    source_provider: str | None
    telegram_envelope: object | None


@dataclass(frozen=True)
class ProviderExtraction:
    """Typed provider provenance (and trusted Telegram ordering) extracted from one record."""

    provider_record: ProviderRecordMetadata | None = None
    telegram_order: TelegramDocumentOrder | None = None


class ProviderMetadataExtractor(Protocol):
    """Strategy deriving typed provider provenance from an accepted record for one or more providers.

    Implementations raise ``ValueError``/``TypeError`` for any record that violates the provider's contract;
    the worker records that as an invalid record without entering the Documents owner.
    """

    providers: ClassVar[tuple[str, ...]]

    def extract(self, item: ExtractionInput) -> ProviderExtraction:
        """Return the provider provenance for ``item`` or raise on a contract violation."""
        ...


class TypedEnvelopeExtractor:
    """Default extractor: validates a typed ``provider_record`` envelope and merges allowlisted raw fields.

    Subclasses choose whether the envelope is mandatory (``requires_envelope``), what happens without one
    (``_extract_untyped``) and what extra proof the typed record needs (``_finish_typed``).
    """

    providers: ClassVar[tuple[str, ...]] = ()
    requires_envelope: ClassVar[bool] = False

    def extract(self, item: ExtractionInput) -> ProviderExtraction:
        """Dispatch on the presence of the typed envelope."""
        provider_raw = item.record.metadata.get("provider_record")
        if provider_raw is None:
            return self._extract_untyped(item)
        return self._finish_typed(item, self._merged_typed_record(item, provider_raw))

    def _extract_untyped(self, item: ExtractionInput) -> ProviderExtraction:
        """Handle a record without an envelope: mandatory providers reject it, others carry no provenance."""
        if self.requires_envelope:
            raise ValueError("Native provider record metadata is missing")
        return ProviderExtraction()

    @staticmethod
    def _merged_typed_record(item: ExtractionInput, provider_raw: object) -> ProviderRecordMetadata:
        """Validate the envelope against the accepted record and merge its provider's allowlisted raw fields."""
        record = item.record
        provider_record = ProviderRecordMetadata.model_validate(provider_raw)
        if (
            provider_record.identity != record.provider_id
            or provider_record.provider_version != record.version
            or provider_record.provider != item.source_provider
        ):
            raise ValueError("Typed provider provenance does not match the accepted source record")
        source_fields = dict(provider_record.source_fields)
        for key in PROVIDER_FIELD_KEYS[provider_record.provider]:
            if key in record.metadata and record.metadata[key] is not None:
                source_fields[key] = record.metadata[key]
        merged = provider_record.model_dump(mode="json")
        merged["source_fields"] = source_fields
        return ProviderRecordMetadata.model_validate(merged)

    def _finish_typed(
        self, item: ExtractionInput, provider_record: ProviderRecordMetadata
    ) -> ProviderExtraction:
        """Provider-specific proof; only Telegram may carry a delivery proof envelope."""
        if item.telegram_envelope is not None:
            raise ValueError("Telegram delivery proof is not valid for this provider")
        return ProviderExtraction(provider_record=provider_record)


class PassthroughExtractor(TypedEnvelopeExtractor):
    """Providers without a typed envelope contract (for example RSS): no provenance unless one is supplied."""


class EnvelopeRequiredExtractor(TypedEnvelopeExtractor):
    """Native providers whose accepted records must always carry the typed envelope."""

    providers: ClassVar[tuple[str, ...]] = ENVELOPE_REQUIRED_PROVIDERS
    requires_envelope: ClassVar[bool] = True


class MeasurementExtractor(TypedEnvelopeExtractor):
    """Structured world-data providers: typed envelope, or the legacy ``world_data`` compatibility form."""

    providers: ClassVar[tuple[str, ...]] = tuple(MEASUREMENT_PROVIDER_IDS)

    def _extract_untyped(self, item: ExtractionInput) -> ProviderExtraction:
        """Accept an already-persisted legacy row after its identity/hash checks (public ingress needs the envelope)."""
        record = item.record
        measurement = WorldDataMeasurement.model_validate(record.metadata.get("world_data"))
        if measurement.provider != item.source_provider:
            raise ValueError("Structured measurement does not match configured source provider")
        return ProviderExtraction(
            provider_record=ProviderRecordMetadata(
                provider=measurement.provider,
                identity=record.provider_id,
                provider_version=record.version,
                timestamp_basis="collection",
                coverage="returned_snapshot",
                content_truncated=False,
                world_data=measurement,
            )
        )


class TelegramExtractor(TypedEnvelopeExtractor):
    """Telegram: re-verify the persisted delivery proof and expose the trusted update ordering."""

    providers: ClassVar[tuple[str, ...]] = ("telegram",)
    requires_envelope: ClassVar[bool] = True

    def _finish_typed(
        self, item: ExtractionInput, provider_record: ProviderRecordMetadata
    ) -> ProviderExtraction:
        """Bind the record to its persisted raw delivery, selection clock and bot/epoch/update identity."""
        detail = provider_record.telegram
        envelope = item.telegram_envelope
        if not isinstance(envelope, dict) or detail is None:
            raise ValueError("Persisted Telegram raw delivery proof is missing")
        proof = TelegramDeliveryProof.model_validate(envelope.get("proof"))
        raw = TelegramRawDelivery.model_validate(envelope.get("raw_update"))
        validate_telegram_record_delivery(item.record, provider_record, proof, raw)
        if item.observed_at != item.record.observed_at:
            raise ValueError(
                "Persisted observation selection clock differs from verified Telegram clock"
            )
        merged = provider_record.model_dump(mode="json")
        merged["telegram"]["raw_update_sha256"] = proof.raw_update_sha256
        return ProviderExtraction(
            provider_record=ProviderRecordMetadata.model_validate(merged),
            telegram_order=TelegramDocumentOrder(
                bot_id=detail.bot_id, epoch=detail.epoch, update_id=detail.update_id
            ),
        )


_REGISTRY: dict[str, ProviderMetadataExtractor] = {}
_DEFAULT_EXTRACTOR: ProviderMetadataExtractor = PassthroughExtractor()


def register_metadata_extractor(extractor: ProviderMetadataExtractor) -> None:
    """Register ``extractor`` for each provider id it declares (last registration wins).

    Adding a provider to normalization is a registration; the worker's row pipeline is not edited.
    """
    for provider in extractor.providers:
        _REGISTRY[provider] = extractor


def metadata_extractor_for(provider: str | None) -> ProviderMetadataExtractor:
    """Return the extractor registered for ``provider``, or the pass-through default."""
    return _REGISTRY.get(provider or "", _DEFAULT_EXTRACTOR)


for _extractor in (EnvelopeRequiredExtractor(), MeasurementExtractor(), TelegramExtractor()):
    register_metadata_extractor(_extractor)
