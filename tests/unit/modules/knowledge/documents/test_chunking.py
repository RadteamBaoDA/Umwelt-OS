"""Unit tests for token chunking, overlap calculation, UTF-8 boundaries, and token bounds."""

from dataclasses import FrozenInstanceError

import pytest

from core.chunking import ENCODING, ChunkDraft, chunk_text


class TestChunkTextValidationAndBounds:
    """Test parameter validation, boundaries, and empty input handling for chunk_text."""

    def test_invalid_target_tokens_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid chunking settings"):
            chunk_text("Some text", target_tokens=0)
        with pytest.raises(ValueError, match="Invalid chunking settings"):
            chunk_text("Some text", target_tokens=-10)

    @pytest.mark.parametrize("bad_ratio", [-0.5, -0.01, 1.0, 1.2, 5.0])
    def test_invalid_overlap_ratio_rejected(self, bad_ratio: float) -> None:
        with pytest.raises(ValueError, match="Invalid chunking settings"):
            chunk_text("Some text", target_tokens=100, overlap_ratio=bad_ratio)

    def test_valid_boundary_overlap_ratios(self) -> None:
        # 0.0 is valid (no overlap)
        chunks = chunk_text("Hello world", target_tokens=10, overlap_ratio=0.0)
        assert len(chunks) == 1

        # 0.99 is valid (< 1.0)
        chunks_high = chunk_text("Hello world", target_tokens=10, overlap_ratio=0.99)
        assert len(chunks_high) == 1

    def test_empty_string_returns_no_chunks(self) -> None:
        assert chunk_text("") == []

    def test_single_token_text(self) -> None:
        chunks = chunk_text("Hello", target_tokens=10, overlap_ratio=0.1)
        assert len(chunks) == 1
        assert chunks[0].content == "Hello"
        assert chunks[0].token_count == 1
        assert chunks[0].metadata == {"start_token": 0}

    def test_text_below_target_tokens_returns_single_chunk(self) -> None:
        text = "This is a short sentence that easily fits within the target tokens."
        tokens = ENCODING.encode(text)
        chunks = chunk_text(text, target_tokens=len(tokens) + 10)
        assert len(chunks) == 1
        assert chunks[0].content == text
        assert chunks[0].token_count == len(tokens)
        assert chunks[0].metadata == {"start_token": 0}

    def test_text_exactly_target_tokens(self) -> None:
        text = "One two three four five six seven eight nine ten"
        tokens = ENCODING.encode(text)
        chunks = chunk_text(text, target_tokens=len(tokens), overlap_ratio=0.2)
        assert len(chunks) == 1
        assert chunks[0].content == text
        assert chunks[0].token_count == len(tokens)


class TestChunkTextOverlapAndContinuity:
    """Test overlap calculation, chunk progression, and coverage of long texts."""

    def test_zero_overlap_produces_non_overlapping_chunks(self) -> None:
        text = "apple banana orange grape pear peach plum melon cherry lemon kiwi mango pineapple strawberry blueberry " * 10
        chunks = chunk_text(text, target_tokens=20, overlap_ratio=0.0)
        assert len(chunks) > 1

        # Check start_token progression
        for i in range(len(chunks) - 1):
            current_chunk = chunks[i]
            next_chunk = chunks[i + 1]
            current_start = current_chunk.metadata["start_token"]
            next_start = next_chunk.metadata["start_token"]
            assert isinstance(current_start, int)
            assert isinstance(next_start, int)
            # With 0 overlap, next_start should be >= current_start + current_chunk.token_count
            assert next_start >= current_start + current_chunk.token_count

    def test_positive_overlap_preserves_overlap_content(self) -> None:
        words = [f"word{i}" for i in range(200)]
        text = " ".join(words)
        target_tokens = 30
        overlap_ratio = 0.2  # 6 tokens overlap
        chunks = chunk_text(text, target_tokens=target_tokens, overlap_ratio=overlap_ratio)
        assert len(chunks) > 2

        # Verify next start token is before the end of the previous chunk (i.e. overlapping)
        for i in range(len(chunks) - 1):
            c1 = chunks[i]
            c2 = chunks[i + 1]
            s1 = c1.metadata["start_token"]
            s2 = c2.metadata["start_token"]
            assert isinstance(s1, int)
            assert isinstance(s2, int)
            assert s2 > s1  # monotonic forward progress
            assert s2 < s1 + c1.token_count  # overlapping

    def test_chunks_cover_entire_tokenized_text(self) -> None:
        text = "Paragraph test for complete coverage. " * 50
        tokens = ENCODING.encode(text)
        chunks = chunk_text(text, target_tokens=40, overlap_ratio=0.15)
        assert len(chunks) > 1

        # The last chunk should finish at the end of tokens
        last_chunk = chunks[-1]
        last_start = last_chunk.metadata["start_token"]
        assert isinstance(last_start, int)
        assert last_start + last_chunk.token_count == len(tokens)

    def test_chunk_token_count_respects_target_bounds(self) -> None:
        text = "Machine learning and artificial intelligence systems require robust data ingestion pipelines. " * 30
        target = 25
        chunks = chunk_text(text, target_tokens=target, overlap_ratio=0.1)
        for chunk in chunks:
            assert 0 < chunk.token_count <= target + 2  # Allows slight boundary snap tolerances


class TestUtf8BoundaryHandling:
    """Test boundary handling for multi-byte UTF-8 sequences (Vietnamese, emojis, CJK)."""

    def test_multibyte_vietnamese_text(self) -> None:
        text = (
            "Hệ thống trí tuệ nhân tạo cá nhân hóa có khả năng xử lý tài liệu, "
            "phân tích dữ liệu ngữ nghĩa, và hỗ trợ tự động hóa các tác vụ hàng ngày. "
            "Đảm bảo an toàn thông tin và tính toàn vẹn dữ liệu cho người dùng. "
        ) * 10
        chunks = chunk_text(text, target_tokens=30, overlap_ratio=0.1)
        assert len(chunks) > 1
        for chunk in chunks:
            # Must decode cleanly as valid UTF-8 without errors or unicode replacement chars
            assert isinstance(chunk.content, str)
            assert "\ufffd" not in chunk.content
            # Re-encoding to UTF-8 must succeed
            chunk.content.encode("utf-8")

    def test_multibyte_emojis_and_symbols(self) -> None:
        text = "🤖 🚀 🌟 💡 🎯 🧩 🔍 📈 🔒 🛠️ " * 30
        chunks = chunk_text(text, target_tokens=15, overlap_ratio=0.2)
        assert len(chunks) > 1
        for chunk in chunks:
            assert "\ufffd" not in chunk.content
            assert len(chunk.content) > 0

    def test_multibyte_cjk_characters(self) -> None:
        text = "人工智能与知识管理平台。多语言自然语言处理与向量检索系统。" * 20
        chunks = chunk_text(text, target_tokens=20, overlap_ratio=0.1)
        assert len(chunks) > 1
        for chunk in chunks:
            assert "\ufffd" not in chunk.content
            assert len(chunk.content) > 0


class TestChunkDraftImmutability:
    """Test ChunkDraft dataclass immutability and representation."""

    def test_chunk_draft_is_frozen(self) -> None:
        chunk = ChunkDraft(content="Sample text", token_count=2, metadata={"start_token": 0})
        assert chunk.content == "Sample text"
        assert chunk.token_count == 2
        assert chunk.metadata == {"start_token": 0}

        with pytest.raises(FrozenInstanceError):
            chunk.content = "Modified"  # type: ignore[misc]

        with pytest.raises(FrozenInstanceError):
            chunk.token_count = 10  # type: ignore[misc]
