"""Unit tests for knowledge documents schemas, byte boundaries, validators, and cursors."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.pagination import decode_cursor, encode_cursor
from modules.knowledge.documents.schemas import (
    MAX_CONTENT_BYTES,
    MAX_METADATA_BYTES,
    ContentUpdate,
    DocumentCreate,
    DocumentList,
    DocumentPatch,
    DocumentRead,
    ProviderRecordMetadata,
    ProviderSnapshotRequest,
    ProviderTelegramMedia,
    ProviderTelegramMetadata,
    VersionList,
    VersionRead,
    validate_content,
    validate_metadata,
)


class TestByteBoundariesAndValidators:
    """Test raw content and metadata byte limits and JSON sanitization."""

    def test_validate_content_within_1mib(self) -> None:
        content_1mib = "x" * MAX_CONTENT_BYTES
        assert validate_content(content_1mib) == content_1mib

    def test_validate_content_exceeds_1mib_raises(self) -> None:
        content_over = "x" * (MAX_CONTENT_BYTES + 1)
        with pytest.raises(ValueError, match="content exceeds 1 MiB"):
            validate_content(content_over)

    def test_validate_content_multibyte_boundary(self) -> None:
        # Each '€' character is 3 bytes in UTF-8
        # MAX_CONTENT_BYTES // 3 fits
        num_chars = MAX_CONTENT_BYTES // 3
        valid_multibyte = "€" * num_chars
        assert validate_content(valid_multibyte) == valid_multibyte

        # Exceeds 1 MiB in encoded bytes
        over_multibyte = "€" * (num_chars + 1)
        with pytest.raises(ValueError, match="content exceeds 1 MiB"):
            validate_content(over_multibyte)

    def test_validate_metadata_within_64kib(self) -> None:
        small_meta = {"key": "value", "list": [1, 2, 3]}
        assert validate_metadata(small_meta) == small_meta

    def test_validate_metadata_exceeds_64kib_raises(self) -> None:
        large_meta = {"key": "x" * (MAX_METADATA_BYTES + 1)}
        with pytest.raises(ValueError, match="metadata exceeds 64 KiB"):
            validate_metadata(large_meta)

    def test_validate_metadata_rejects_nan_and_infinity(self) -> None:
        with pytest.raises(ValueError):
            validate_metadata({"val": float("nan")})
        with pytest.raises(ValueError):
            validate_metadata({"val": float("inf")})
        with pytest.raises(ValueError):
            validate_metadata({"val": float("-inf")})


class TestDocumentCreateAndPatch:
    """Test DocumentCreate, DocumentPatch, and ContentUpdate schemas."""

    def test_document_create_valid(self) -> None:
        source_id = uuid4()
        doc = DocumentCreate(
            source_id=source_id,
            title="Overview of AI",
            content="# Overview\nContent here",
            external_id="ext-1234",
            metadata={"category": "tech"},
        )
        assert doc.source_id == source_id
        assert doc.title == "Overview of AI"
        assert doc.content == "# Overview\nContent here"
        assert doc.external_id == "ext-1234"
        assert doc.metadata == {"category": "tech"}

    def test_document_create_title_boundaries(self) -> None:
        source_id = uuid4()
        # Min length: 1
        assert DocumentCreate(source_id=source_id, title="A", content="text").title == "A"
        # Max length: 500
        assert DocumentCreate(source_id=source_id, title="x" * 500, content="text").title == "x" * 500

        with pytest.raises(ValidationError):
            DocumentCreate(source_id=source_id, title="", content="text")
        with pytest.raises(ValidationError):
            DocumentCreate(source_id=source_id, title="x" * 501, content="text")

    def test_document_create_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            DocumentCreate(
                source_id=uuid4(),
                title="Title",
                content="text",
                extra_field="disallowed",  # type: ignore[call-arg]
            )

    def test_document_create_content_bound_enforced(self) -> None:
        with pytest.raises(ValidationError, match="content exceeds 1 MiB"):
            DocumentCreate(
                source_id=uuid4(),
                title="Title",
                content="x" * (MAX_CONTENT_BYTES + 1),
            )

    def test_document_patch_fields_and_bounds(self) -> None:
        # Empty patch
        patch = DocumentPatch()
        assert patch.title is None
        assert patch.metadata is None

        # Valid updates
        patch = DocumentPatch(title="Updated", metadata={"new": 1})
        assert patch.title == "Updated"
        assert patch.metadata == {"new": 1}

        # Extra forbidden
        with pytest.raises(ValidationError):
            DocumentPatch(title="Updated", extra_prop=1)  # type: ignore[call-arg]

        # Metadata bound in patch
        with pytest.raises(ValidationError, match="metadata exceeds 64 KiB"):
            DocumentPatch(metadata={"data": "x" * (MAX_METADATA_BYTES + 1)})

    def test_content_update_version_and_bounds(self) -> None:
        cu = ContentUpdate(expected_version=1, content="Updated version content")
        assert cu.expected_version == 1
        assert cu.content == "Updated version content"

        with pytest.raises(ValidationError):
            ContentUpdate(expected_version=0, content="bad version")

        with pytest.raises(ValidationError, match="content exceeds 1 MiB"):
            ContentUpdate(expected_version=2, content="x" * (MAX_CONTENT_BYTES + 1))


class TestDocumentReadAndVersionRead:
    """Test serialization and reading of DocumentRead, VersionRead, and list models."""

    def test_document_read_serialization(self) -> None:
        now = datetime.now(UTC)
        doc_id = uuid4()
        src_id = uuid4()
        data = {
            "id": doc_id,
            "source_id": src_id,
            "external_id": "ext-1",
            "title": "Document Title",
            "content_type": "text/markdown",
            "mime_type": "text/markdown",
            "raw_uri": "s3://bucket/key",
            "canonical_url": "https://example.com/doc",
            "author": "Author Name",
            "metadata": {"tags": ["a", "b"]},
            "current_version": 2,
            "content_hash": "a" * 64,
            "extraction_status": "succeeded",
            "published_at": now,
            "observed_at": now,
            "language": "en",
            "created_at": now,
            "updated_at": now,
        }
        read = DocumentRead.model_validate(data)
        assert read.id == doc_id
        assert read.source_id == src_id
        assert read.current_version == 2
        assert read.content_hash == "a" * 64

    def test_version_read_and_version_list(self) -> None:
        now = datetime.now(UTC)
        ver_id = uuid4()
        doc_id = uuid4()
        v_read = VersionRead(
            id=ver_id,
            document_id=doc_id,
            version_number=1,
            content="Historical text",
            content_hash="h" * 64,
            observed_at=now,
            created_at=now,
        )
        assert v_read.version_number == 1
        assert v_read.content == "Historical text"

        v_list = VersionList(items=[v_read], next_cursor="next_cursor_123")
        assert len(v_list.items) == 1
        assert v_list.next_cursor == "next_cursor_123"

    def test_document_list_with_cursor(self) -> None:
        d_list = DocumentList(items=[], next_cursor=None)
        assert d_list.items == []
        assert d_list.next_cursor is None


class TestProviderMetadataAndTelegram:
    """Test ProviderRecordMetadata, ProviderTelegramMetadata, and TelegramDocumentOrder."""

    def test_telegram_media_model(self) -> None:
        media = ProviderTelegramMedia(
            kind="photo",
            caption="Caption text",
            count=1,
            file_id="tg_file_abc",
        )
        assert media.kind == "photo"
        assert media.count == 1
        # count out of bounds
        with pytest.raises(ValidationError):
            ProviderTelegramMedia(kind="photo", count=0)
        with pytest.raises(ValidationError):
            ProviderTelegramMedia(kind="photo", count=101)

    def test_telegram_metadata_aware_times(self) -> None:
        now_aware = datetime.now(UTC)
        tg_meta = ProviderTelegramMetadata(
            bot_id="123456",
            channel_id="-1001234567890",
            message_id="789",
            epoch=1,
            update_id=10,
            edited_received=False,
            published_at=now_aware,
        )
        assert tg_meta.bot_id == "123456"
        assert tg_meta.update_id == 10

        # Naive datetime rejected
        naive_now = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="Telegram timestamps must include a timezone"):
            ProviderTelegramMetadata(
                bot_id="123456",
                channel_id="-1001234567890",
                message_id="789",
                epoch=1,
                update_id=10,
                edited_received=False,
                published_at=naive_now,
            )

    def test_provider_record_metadata_github_releases_url_validation(self) -> None:
        meta = ProviderRecordMetadata(
            provider="github_releases",
            identity="repo/releases/1",
            timestamp_basis="provider_published",
            coverage="returned_snapshot",
            content_truncated=False,
            source_fields={
                "name": "Release 1.0",
                "tag_name": "v1.0",
                "html_url": "https://github.com/owner/repo/releases/tag/v1.0",
                "draft": False,
                "prerelease": False,
            },
        )
        assert meta.provider == "github_releases"

        # Invalid html_url: not github.com
        with pytest.raises(ValidationError, match="Provider release URL must target github.com"):
            ProviderRecordMetadata(
                provider="github_releases",
                identity="repo/releases/1",
                timestamp_basis="provider_published",
                coverage="returned_snapshot",
                content_truncated=False,
                source_fields={
                    "html_url": "https://gitlab.com/owner/repo",
                },
            )

        # Invalid html_url: http instead of https
        with pytest.raises(ValidationError, match="Provider release URL must target github.com"):
            ProviderRecordMetadata(
                provider="github_releases",
                identity="repo/releases/1",
                timestamp_basis="provider_published",
                coverage="returned_snapshot",
                content_truncated=False,
                source_fields={
                    "html_url": "http://github.com/owner/repo",
                },
            )

    def test_provider_telegram_must_match_provider_type(self) -> None:
        now_aware = datetime.now(UTC)
        tg_detail = ProviderTelegramMetadata(
            bot_id="123",
            channel_id="-100123",
            message_id="456",
            epoch=1,
            update_id=1,
            edited_received=False,
            published_at=now_aware,
        )

        # telegram provider without telegram block raises
        with pytest.raises(ValidationError, match="Telegram detail must match provider"):
            ProviderRecordMetadata(
                provider="telegram",
                identity="tg-1",
                timestamp_basis="provider_published",
                coverage="returned_snapshot",
                content_truncated=False,
                telegram=None,
            )

        # non-telegram provider with telegram block raises
        with pytest.raises(ValidationError, match="Telegram detail must match provider"):
            ProviderRecordMetadata(
                provider="youtube",
                identity="yt-1",
                timestamp_basis="provider_published",
                coverage="returned_snapshot",
                content_truncated=False,
                telegram=tg_detail,
            )

    def test_provider_snapshot_request_unique_ids(self) -> None:
        id1, id2 = uuid4(), uuid4()
        req = ProviderSnapshotRequest(version_ids=[id1, id2])
        assert req.version_ids == [id1, id2]

        with pytest.raises(ValidationError, match="version_ids must be unique"):
            ProviderSnapshotRequest(version_ids=[id1, id1])


class TestPaginationCursors:
    """Test keyset pagination encoding and decoding for document listings."""

    def test_cursor_roundtrip_utc(self) -> None:
        ts = datetime(2026, 5, 20, 15, 30, 45, 123456, tzinfo=UTC)
        ident = uuid4()
        cursor_str = encode_cursor(ts, ident)
        assert isinstance(cursor_str, str)
        decoded_ts, decoded_ident = decode_cursor(cursor_str)
        assert decoded_ts == ts
        assert decoded_ident == ident

    def test_cursor_ordering_invariants(self) -> None:
        now = datetime.now(UTC)
        id1, id2 = uuid4(), uuid4()
        c1 = encode_cursor(now, id1)
        c2 = encode_cursor(now, id2)
        assert c1 != c2
        _, dec_id1 = decode_cursor(c1)
        _, dec_id2 = decode_cursor(c2)
        assert dec_id1 == id1
        assert dec_id2 == id2
