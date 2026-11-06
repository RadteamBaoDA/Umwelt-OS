"""core.chunking: heading breadcrumbs, structural end-snapping, UTF-8 safety, overlap and argument bounds."""

from __future__ import annotations

import pytest

from core.chunking import ENCODING, _extract_heading_breadcrumbs, _find_natural_cut_char, chunk_text


def test_breadcrumbs_nest_ignore_fences_and_strip_closing_hashes() -> None:
    text = "# A\ntext\n## B ##\n```\n# not a heading\n```\n### C\n## D\n"
    offsets, crumbs = _extract_heading_breadcrumbs(text)
    assert crumbs == ["A", "A > B", "A > B > C", "A > D"]
    assert offsets[0] == 0 and offsets == sorted(offsets)
    assert _extract_heading_breadcrumbs("no headings here") == ([], [])
    # '#' without a space is not a heading; an all-hash title keeps its raw text.
    assert _extract_heading_breadcrumbs("#tag\n") == ([], [])
    assert _extract_heading_breadcrumbs("# ##\n")[1] == ["##"]


def test_breadcrumb_offsets_are_utf8_byte_offsets() -> None:
    text = "é\n# H\n"
    offsets, _ = _extract_heading_breadcrumbs(text)
    assert offsets == [len("é\n".encode())]


def test_natural_cut_priority_heading_paragraph_line_sentence() -> None:
    assert _find_natural_cut_char("short") is None
    base = "x" * 90
    assert _find_natural_cut_char(base + "\n#h" + "y" * 5) == 91
    assert _find_natural_cut_char(base + "\n\nyyyy") == 92
    assert _find_natural_cut_char(base + "\nyyyyy") == 91
    assert _find_natural_cut_char(base + ". yyyy") == 92
    assert _find_natural_cut_char("z" * 100) is None


def test_chunks_cover_text_with_overlap_and_section_metadata() -> None:
    sections = [
        f"# Part {i}\n" + " ".join(f"word{i}_{j}" for j in range(120)) + "\n\n" for i in range(4)
    ]
    text = "".join(sections)
    chunks = chunk_text(text, target_tokens=100, overlap_ratio=0.1)
    assert len(chunks) > 4
    assert all(c.token_count <= 100 and c.content for c in chunks)
    starts = [c.metadata["start_token"] for c in chunks]
    assert starts == sorted(set(starts)) and starts[0] == 0
    assert chunks[0].metadata["section_heading"] == "Part 0"
    assert chunks[-1].metadata["section_heading"] == "Part 3"
    # Overlap: each chunk starts before the previous chunk ended.
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.metadata["start_token"] < prev.metadata["start_token"] + prev.token_count
    assert chunks[-1].content.rstrip().endswith("word3_119")


def test_snaps_chunk_end_to_a_trailing_paragraph_break() -> None:
    para = " ".join(f"w{i}" for i in range(60))
    text = f"{para}\n\n{para}\n\n{para}"
    chunks = chunk_text(text, target_tokens=len(ENCODING.encode(para)) + 8, overlap_ratio=0.0)
    assert chunks[0].content.endswith("\n\n")


def test_multibyte_text_never_splits_a_character() -> None:
    text = "日本語のテキスト😀" * 80
    chunks = chunk_text(text, target_tokens=17, overlap_ratio=0.2)
    assert all("�" not in c.content for c in chunks)
    assert chunks[-1].content.endswith("😀")


def test_empty_text_and_invalid_settings() -> None:
    assert chunk_text("") == []
    for kwargs in ({"target_tokens": 0}, {"overlap_ratio": 1.0}, {"overlap_ratio": -0.1}):
        with pytest.raises(ValueError, match="Invalid chunking"):
            chunk_text("abc", **kwargs)
