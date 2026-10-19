import codecs
import re
from bisect import bisect_right
from dataclasses import dataclass

import tiktoken

ENCODING = tiktoken.get_encoding("cl100k_base")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(?:```|~~~)")
_SENTENCE_ENDINGS = (". ", "! ", "? ")
_TRAILING_SNAP_FRACTION = 0.85


@dataclass(frozen=True)
class ChunkDraft:
    """Immutable text chunk plus its tokenizer count and source-position metadata."""

    content: str
    token_count: int
    metadata: dict[str, object]


def _extract_heading_breadcrumbs(text: str) -> tuple[list[int], list[str]]:
    """Extract UTF-8 byte offsets and active Markdown heading breadcrumbs outside code fences."""
    if "#" not in text:
        return [], []
    byte_offsets: list[int] = []
    breadcrumbs: list[str] = []
    active_headings: dict[int, str] = {}
    in_fence = False
    byte_pos = 0
    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and "#" in line:
            match = _HEADING_RE.match(line.rstrip("\r\n"))
            if match is not None:
                level = len(match.group(1))
                raw_title = match.group(2).strip()
                title = raw_title.rstrip("#").strip() or raw_title
                active_headings[level] = title
                for deeper in [k for k in active_headings if k > level]:
                    del active_headings[deeper]
                breadcrumb = " > ".join(active_headings[k] for k in sorted(active_headings))
                byte_offsets.append(byte_pos)
                breadcrumbs.append(breadcrumb)
        byte_pos += len(line.encode("utf-8"))
    return byte_offsets, breadcrumbs


def _find_natural_cut_char(content: str) -> int | None:
    """Locate the highest-priority structural break in the trailing 15% of a chunk string."""
    if len(content) <= 20:
        return None
    min_char = int(len(content) * _TRAILING_SNAP_FRACTION)
    # Prefer cutting immediately before a new Markdown heading so the heading opens the next chunk.
    heading_idx = content.rfind("\n#", min_char)
    if heading_idx != -1:
        return heading_idx + 1
    para_idx = content.rfind("\n\n", min_char)
    if para_idx != -1:
        return para_idx + 2
    line_idx = content.rfind("\n", min_char)
    if line_idx != -1:
        return line_idx + 1
    best_sentence_idx = max(content.rfind(sep, min_char) for sep in _SENTENCE_ENDINGS)
    if best_sentence_idx != -1:
        return best_sentence_idx + 2
    return None


def _snap_end_to_structure(
    token_bytes: list[bytes],
    boundaries: list[int],
    start: int,
    end: int,
    raw_bytes: bytes,
    content: str,
) -> tuple[int, str]:
    """Snap a non-final chunk end token index to a trailing natural break if one exists."""
    cut_char = _find_natural_cut_char(content)
    if cut_char is None or cut_char <= 0 or cut_char >= len(content):
        return end, content
    target_bytes = len(content[:cut_char].encode("utf-8"))
    current_bytes = len(raw_bytes)
    cand_end = end
    min_token = start + max(1, int((end - start) * 0.80))
    while cand_end > min_token and current_bytes > target_bytes:
        cand_end -= 1
        current_bytes -= len(token_bytes[cand_end])
    snap_pos = bisect_right(boundaries, cand_end) - 1
    if snap_pos < 0:
        return end, content
    snapped_end = boundaries[snap_pos]
    if snapped_end <= start or snapped_end < min_token or snapped_end == end:
        return end, content
    snapped_content = b"".join(token_bytes[start:snapped_end]).decode("utf-8")
    return snapped_end, snapped_content


def chunk_text(text: str, target_tokens: int = 750, overlap_ratio: float = 0.12) -> list[ChunkDraft]:
    """Split text into UTF-8-safe token chunks, retaining the configured overlap and each chunk start offset. Reject invalid settings; empty text returns no chunks."""
    if target_tokens < 1 or not 0 <= overlap_ratio < 1:
        raise ValueError("Invalid chunking settings")
    tokens = ENCODING.encode(text, disallowed_special=())
    if not tokens:
        return []
    token_bytes = [ENCODING.decode_single_token_bytes(token) for token in tokens]
    boundaries = [0]
    heading_offsets, heading_breadcrumbs = _extract_heading_breadcrumbs(text)
    token_byte_offsets: list[int] = [0] if heading_offsets else []
    running_bytes = 0
    # Token boundaries can split one UTF-8 character; retain only offsets where decoding has no pending bytes.
    decoder = codecs.getincrementaldecoder("utf-8")()
    for index, token in enumerate(token_bytes, start=1):
        decoder.decode(token)
        if not decoder.getstate()[0]:
            boundaries.append(index)
        if heading_offsets:
            running_bytes += len(token)
            token_byte_offsets.append(running_bytes)
    overlap = min(target_tokens - 1, int(target_tokens * overlap_ratio))
    chunks: list[ChunkDraft] = []
    start = 0
    while start < len(tokens):
        desired_end = min(start + target_tokens, len(tokens))
        # Snap chunk ends and overlap starts to valid text boundaries; nominal token sizes can shift slightly.
        end_pos = bisect_right(boundaries, desired_end) - 1
        end = (
            boundaries[end_pos]
            if end_pos >= 0 and boundaries[end_pos] > start
            else boundaries[bisect_right(boundaries, start)]
        )
        raw_bytes = b"".join(token_bytes[start:end])
        content = raw_bytes.decode("utf-8")
        if end < len(tokens):
            end, content = _snap_end_to_structure(
                token_bytes, boundaries, start, end, raw_bytes, content,
            )
        metadata: dict[str, object] = {"start_token": start}
        if heading_offsets:
            chunk_end_byte = token_byte_offsets[end]
            heading_pos = bisect_right(heading_offsets, chunk_end_byte - 1) - 1
            if heading_pos >= 0:
                metadata["section_heading"] = heading_breadcrumbs[heading_pos]
        chunks.append(ChunkDraft(content, end - start, metadata))
        if end == len(tokens):
            break
        next_start = start + 1
        overlap_boundary = end - overlap
        overlap_pos = bisect_right(boundaries, overlap_boundary) - 1
        start = (
            boundaries[overlap_pos]
            if overlap_pos >= 0 and overlap_pos >= next_start
            else end
        )
    return chunks
