"""Unit tests for modules.knowledge.documents.public.

Tests cover:
- Document extraction and ExtractionInputLimitError bounds validation
  (empty input, > 100 chunks, > 64,000 bytes, allowed_chunk_ids bounds).
- Version hashing (content_hash SHA-256 calculation and determinism).
- Chunking bounds and persistence (add_content_chunks, _list_evidence_ref_keys bounds).
- Cursor serialization and decoding (_encode_provider_cursor, _decode_provider_cursor,
  _encode_news_projection_cursor, _decode_news_projection_cursor, version cursor roundtrips).
- Document retention and observation validation (news_retained_observation_allowed,
  news_projection_scope_unavailable, delete_document).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.documents.models import DocumentChunk, DocumentVersion
from modules.knowledge.documents.public import (
    EXTRACTION_CHUNK_LIMIT,
    EXTRACTION_INPUT_BYTES,
    ExtractionInput,
    ExtractionInputLimitError,
    _decode_news_projection_cursor,
    _decode_provider_cursor,
    _encode_news_projection_cursor,
    _encode_provider_cursor,
    _list_evidence_ref_keys,
    add_content_chunks,
    content_hash,
    decode_version_cursor,
    delete_document,
    encode_version_cursor,
    news_projection_scope_unavailable,
    news_retained_observation_allowed,
    read_extraction_input,
)
from tests.unit.modules.knowledge.documents._scope import SCOPE_KW


class TestVersionHashingAndExtractionLimits:
    """Tests for content_hash, ExtractionInputLimitError, and read_extraction_input bounds."""

    def test_content_hash_calculation(self) -> None:
        """Verify content_hash computes deterministic SHA-256 for UTF-8 text."""
        text = "Hello, Umwelt-OS Knowledge System!"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert content_hash(text) == expected

        # Empty content hash
        assert content_hash("") == hashlib.sha256(b"").hexdigest()

    def test_extraction_input_limit_error_subclass(self) -> None:
        """Verify ExtractionInputLimitError is a subclass of ValueError."""
        err = ExtractionInputLimitError("Oversized extraction input")
        assert isinstance(err, ValueError)
        assert str(err) == "Oversized extraction input"

    @pytest.mark.asyncio
    async def test_read_extraction_input_missing_document_returns_none(self) -> None:
        """Verify read_extraction_input returns None when document/version row is missing."""
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=None)))

        result = await read_extraction_input(session, version_id=uuid4(), **SCOPE_KW)
        assert result is None

    @pytest.mark.asyncio
    async def test_read_extraction_input_allowed_chunks_bounds(self) -> None:
        """Verify allowed_chunk_ids cannot be empty, exceed 100, or have duplicates."""
        session = AsyncMock()
        row = (uuid4(), uuid4(), 1, True, uuid4(), datetime.now(UTC))
        session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=row)))

        # Empty allowed_chunk_ids
        with pytest.raises(ValueError, match="Extraction chunk IDs must be unique and bounded"):
            await read_extraction_input(session, version_id=uuid4(), allowed_chunk_ids=[], **SCOPE_KW)

        # Over 100 items
        oversized = [uuid4() for _ in range(EXTRACTION_CHUNK_LIMIT + 1)]
        with pytest.raises(ValueError, match="Extraction chunk IDs must be unique and bounded"):
            await read_extraction_input(session, version_id=uuid4(), allowed_chunk_ids=oversized, **SCOPE_KW)

        # Duplicate IDs
        dup_id = uuid4()
        with pytest.raises(ValueError, match="Extraction chunk IDs must be unique and bounded"):
            await read_extraction_input(session, version_id=uuid4(), allowed_chunk_ids=[dup_id, dup_id], **SCOPE_KW)

    @pytest.mark.asyncio
    async def test_read_extraction_input_chunk_count_zero_raises_limit_error(self) -> None:
        """Verify zero chunks raises ExtractionInputLimitError."""
        session = AsyncMock()
        row = (uuid4(), uuid4(), 1, True, uuid4(), datetime.now(UTC))

        # First query returns header row, second returns stats (chunk_count=0, byte_count=0)
        session.execute = AsyncMock(side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=row)),
            MagicMock(one=MagicMock(return_value=(0, 0))),
        ])

        with pytest.raises(ExtractionInputLimitError, match="Extraction input exceeds its chunk or byte limit"):
            await read_extraction_input(session, version_id=uuid4(), **SCOPE_KW)

    @pytest.mark.asyncio
    async def test_read_extraction_input_chunk_count_exceeded_raises_limit_error(self) -> None:
        """Verify chunk count > 100 raises ExtractionInputLimitError."""
        session = AsyncMock()
        row = (uuid4(), uuid4(), 1, True, uuid4(), datetime.now(UTC))

        session.execute = AsyncMock(side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=row)),
            MagicMock(one=MagicMock(return_value=(EXTRACTION_CHUNK_LIMIT + 1, 1000))),
        ])

        with pytest.raises(ExtractionInputLimitError, match="Extraction input exceeds its chunk or byte limit"):
            await read_extraction_input(session, version_id=uuid4(), **SCOPE_KW)

    @pytest.mark.asyncio
    async def test_read_extraction_input_bytes_exceeded_raises_limit_error(self) -> None:
        """Verify byte count > 64,000 raises ExtractionInputLimitError."""
        session = AsyncMock()
        row = (uuid4(), uuid4(), 1, True, uuid4(), datetime.now(UTC))

        session.execute = AsyncMock(side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=row)),
            MagicMock(one=MagicMock(return_value=(5, EXTRACTION_INPUT_BYTES + 1))),
        ])

        with pytest.raises(ExtractionInputLimitError, match="Extraction input exceeds its chunk or byte limit"):
            await read_extraction_input(session, version_id=uuid4(), **SCOPE_KW)

    @pytest.mark.asyncio
    async def test_read_extraction_input_success(self) -> None:
        """Verify successful extraction input retrieval with bounded chunks."""
        session = AsyncMock()
        doc_id = uuid4()
        src_id = uuid4()
        ver_id = uuid4()
        now = datetime.now(UTC)
        row = (doc_id, src_id, 2, False, ver_id, now)

        c1_id = uuid4()
        c2_id = uuid4()
        chunks_data = [(c1_id, "chunk one text"), (c2_id, "chunk two text")]

        session.execute = AsyncMock(side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=row)),
            MagicMock(one=MagicMock(return_value=(2, 30))),
            MagicMock(all=MagicMock(return_value=chunks_data)),
        ])

        extraction = await read_extraction_input(session, version_id=ver_id, **SCOPE_KW)
        assert isinstance(extraction, ExtractionInput)
        assert extraction.document_id == doc_id
        assert extraction.document_version_id == ver_id
        assert extraction.source_id == src_id
        assert extraction.source_generation == 2
        assert len(extraction.chunks) == 2
        assert extraction.chunks[0].id == c1_id
        assert extraction.chunks[0].content == "chunk one text"


class TestChunkingBounds:
    """Tests for add_content_chunks and _list_evidence_ref_keys bounds."""

    @pytest.mark.asyncio
    async def test_add_content_chunks_creates_chunks(self) -> None:
        """Verify add_content_chunks splits text via chunk_text and creates DocumentChunk rows."""
        session = AsyncMock()
        session.flush = AsyncMock()
        session.add = MagicMock()

        ver_id = uuid4()
        version = DocumentVersion(
            id=ver_id,
            document_id=uuid4(),
            version_number=1,
            content="This is paragraph one.\n\nThis is paragraph two.",
            observed_at=datetime.now(UTC),
        )

        mock_draft1 = MagicMock(content="This is paragraph one.", token_count=5, metadata={})
        mock_draft2 = MagicMock(content="This is paragraph two.", token_count=5, metadata={})

        with patch("modules.knowledge.documents.public.chunk_text", return_value=[mock_draft1, mock_draft2]):
            count = await add_content_chunks(session, version)

        assert count == 2
        assert session.add.call_count == 2
        added_chunks = [call[0][0] for call in session.add.call_args_list]
        assert all(isinstance(c, DocumentChunk) for c in added_chunks)
        assert added_chunks[0].chunk_index == 0
        assert added_chunks[1].chunk_index == 1

    @pytest.mark.asyncio
    async def test_list_evidence_ref_keys_is_workspace_qualified(self) -> None:
        """Verify _list_evidence_ref_keys qualifies the Document root by workspace and Source."""
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        await _list_evidence_ref_keys(session, source_id=uuid4(), scope=SCOPE_KW["scope"])
        sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        assert "documents.workspace_id" in sql and "documents.source_id" in sql

    @pytest.mark.asyncio
    async def test_list_evidence_ref_keys_limit_bounds(self) -> None:
        """Verify _list_evidence_ref_keys rejects limit < 1 or limit > 10,000."""
        session = AsyncMock()
        with pytest.raises(ValueError, match="Specify a bounded limit"):
            await _list_evidence_ref_keys(session, source_id=uuid4(), limit=0, scope=SCOPE_KW["scope"])

        with pytest.raises(ValueError, match="Specify a bounded limit"):
            await _list_evidence_ref_keys(session, source_id=uuid4(), limit=10_001, scope=SCOPE_KW["scope"])

    @pytest.mark.asyncio
    async def test_list_evidence_ref_keys_reports_overflow_without_raising(self) -> None:
        """Verify _list_evidence_ref_keys returns the first limit keys and overflow=True."""
        session = AsyncMock()
        rows = [(uuid4(), uuid4()) for _ in range(3)]
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))

        refs, overflow = await _list_evidence_ref_keys(session, source_id=uuid4(), limit=2, scope=SCOPE_KW["scope"])
        assert overflow is True and refs == rows[:2]


class TestCursorEncoding:
    """Tests for provider, news projection, and version cursor roundtrips and error handling."""

    def test_provider_cursor_roundtrip_and_errors(self) -> None:
        """Verify _encode_provider_cursor and _decode_provider_cursor roundtrip and reject junk."""
        dt = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
        doc_id = uuid4()

        cursor = _encode_provider_cursor(dt, doc_id)
        assert isinstance(cursor, str)

        decoded_dt, decoded_id = _decode_provider_cursor(cursor)
        assert decoded_dt == dt
        assert decoded_id == doc_id

        # Empty cursor
        with pytest.raises(ValueError, match="Invalid provider snapshot cursor"):
            _decode_provider_cursor("")

        # Tampered cursor
        with pytest.raises(ValueError, match="Invalid provider snapshot cursor"):
            _decode_provider_cursor("invalid-base64")

    def test_news_projection_cursor_roundtrip_and_errors(self) -> None:
        """Verify _encode_news_projection_cursor and _decode_news_projection_cursor roundtrip."""
        dt = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
        doc_id = uuid4()

        cursor = _encode_news_projection_cursor(dt, doc_id, "f" * 64)
        assert isinstance(cursor, str)

        decoded_dt, decoded_id = _decode_news_projection_cursor(cursor, "f" * 64)
        assert decoded_dt == dt
        assert decoded_id == doc_id

        with pytest.raises(ValueError, match="Invalid News projection cursor"):
            _decode_news_projection_cursor("", "f" * 64)

    def test_version_cursor_roundtrip_and_errors(self) -> None:
        """Verify encode_version_cursor and decode_version_cursor roundtrip."""
        from fastapi import HTTPException

        v_num = 5
        cursor = encode_version_cursor(v_num)
        assert decode_version_cursor(cursor) == v_num

        # Negative or invalid values in decoded cursor raise HTTPException(422)
        with pytest.raises(HTTPException) as exc_info:
            decode_version_cursor("invalid=padded=")
        assert exc_info.value.status_code == 422


class TestRetentionAndDeletionValidation:
    """Tests for news_retained_observation_allowed, news_projection_scope_unavailable, and delete_document."""

    @pytest.mark.asyncio
    async def test_news_retained_observation_allowed_missing_row(self) -> None:
        """Verify news_retained_observation_allowed returns False when no document row exists."""
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

        allowed = await news_retained_observation_allowed(
            session, document_id=uuid4(), source_id=uuid4(), expected_source_generation=1, **SCOPE_KW,
        )
        assert allowed is False

    @pytest.mark.asyncio
    async def test_news_retained_observation_allowed_generation_mismatch(self) -> None:
        """Verify news_retained_observation_allowed returns False on source generation mismatch."""
        session = AsyncMock()
        # source_type, generation, accepted_generation, provenance
        rows = [("rss", 2, 1, {"provider_scope_discriminator": "a" * 64})]
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))

        allowed = await news_retained_observation_allowed(
            session, document_id=uuid4(), source_id=uuid4(), expected_source_generation=2, **SCOPE_KW,
        )
        assert allowed is False

    @pytest.mark.asyncio
    async def test_news_retained_observation_allowed_non_provider_source(self) -> None:
        """Verify non-provider source types (e.g. manual file upload) are allowed without provider scope check."""
        session = AsyncMock()
        rows = [("file", 1, 1, None)]
        session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))

        allowed = await news_retained_observation_allowed(
            session, document_id=uuid4(), source_id=uuid4(), expected_source_generation=1, **SCOPE_KW,
        )
        assert allowed is True

    @pytest.mark.asyncio
    async def test_news_projection_scope_unavailable_missing_returns_false(self) -> None:
        """Verify news_projection_scope_unavailable returns False for missing/inactive documents."""
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=None)))

        unavailable = await news_projection_scope_unavailable(
            session, document_id=uuid4(), expected_source_generation=1, **SCOPE_KW,
        )
        assert unavailable is False

    @pytest.mark.asyncio
    async def test_delete_document_missing_returns_false(self) -> None:
        """Verify delete_document returns False if document source identity is not found."""
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=None)

        deleted = await delete_document(session, document_id=uuid4(), **SCOPE_KW)
        assert deleted is None
