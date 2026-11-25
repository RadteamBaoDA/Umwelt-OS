"""Deterministic numeric grounding check of a chat answer against the data its turn actually retrieved.

No model call and no I/O. The check collects the numbers present in the turn's ``query_collected_data``
results, the evidence/web text and the user's own message, then looks for each number the answer text and
its table/chart widgets state. It only reports: the answer is never rewritten.

A precise number (relative precision 1% or better) that is not in the data but equals the difference, sum,
mean or percentage change of two data values is reported separately as ``derived``, not as unverified.
A number that is not in the data but appeared in an earlier assistant answer, next to a correction cue
("earlier", "đính chính"), is ``restated``: the model is retracting it. Without the cue it stays unverified.
A number that is in the data but only in a row other than the one(s) its sentence cites with ``[n]`` is
``misattributed`` (a citation-level check; the meaning of the figure within its row is not judged).

Known limits (a number that cannot be matched is reported as unverified even when it is right): other
derivations (averages of many rows, rankings, rounded or low-precision results, counts by complement such
as "all but 2"), unit conversions, spelled-out numbers; bare integers 0-10 and standalone years are not judged at all, and ambiguous
separators ("1.234") match either reading.
"""

import bisect
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from modules.chat.db_query import TOOL_QUERY_COLLECTED_DATA

# Widget source label for a non-sample table/chart whose numbers are not in the retrieved data.
UNVERIFIED_SOURCE = "Unverified figures (not found in retrieved data)"
MAX_EXAMPLES = 10
_COUNTED_LISTS = ("observations", "documents", "rows", "observation_aggregates")
_MAX_KNOWN = 200_000  # bound on remembered values (tool results are already row-capped)
_MAX_TEXT = 100_000  # bound on one scanned text
_MAX_DERIVATION_BASE = 60  # numeric tool values tried pairwise for derived figures (60*59/2 pairs)
_DERIVED_MAX_REL_TOL = 0.01  # only numbers stated to 1% precision or better may count as derived
_MAX_ROWS = 200  # cited tool rows remembered for the per-citation check
_ROW_CITE = re.compile(r"\[\d+\]")
# A sentence (or the one before it) that retracts an earlier answer. Misses fail safe: no cue = unverified.
_CORRECTION = re.compile(
    r"earlier|previous|before|correct|retract|mistake|wrong|not based|should not have|"
    r"trước|đính chính|sửa lại|nhầm|sai|không dựa|không có trong dữ liệu",
    re.IGNORECASE,
)
_SKIP_KEYS = frozenset(
    {"citation", "citation_rule", "hint", "citations", "id", "url", "canonical_url", "timezone"}
)
_SKIP_KEY_SUFFIXES = ("_id", "_at")  # identifiers and timestamps: their digits are not data claims
_MAX_ZEROS = 15  # caps 10**zeros so a long digit run cannot overflow float pow
_SUFFIX = {
    "%": 1.0,
    "k": 1e3,
    "thousand": 1e3,
    "nghìn": 1e3,
    "ngàn": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "triệu": 1e6,
    "tr": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "tỷ": 1e9,
}
_NUM_RE = re.compile(
    r"(?<![\w.,])(?P<body>\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)"
    r"(?:\s?(?P<suf>%|k|mn|m|bn|b|million|billion|thousand|triệu|tỷ|nghìn|ngàn|tr)(?!\w))?",
    re.IGNORECASE,
)
# Real month names only (a prefix match would swallow "decreased", "market", "separate"). Bare "may" is
# also a verb, so it counts as a month only with a year or a day range after it ("May 2026", "May 20-24").
_MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r"|may(?=\s+(?:\d{4}|\d{1,2}\s*[–\-]|\d{1,2},\s*\d{4})))\.?(?![a-z])"
)
# Spans removed before number extraction: they hold digits that are not claims about data.
_COMMON_SKIPS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:https?://|www\.)[^\s<>)\]]+",  # URLs: ids, dates and ports in a link are not claims
        r"\[\d+(?:\s*[,\-–]\s*\d+)*\]",  # citation markers
        r"\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
        # An optional leading day range ("17–24/11", "17–24/11/2026") is part of the date, not a stray number.
        r"(?:\b\d{1,2}\s*[–\-]\s*)?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}",
        r"\b(?:\d{1,2}\s*[–\-]\s*)?\d{1,2}/\d{1,2}(?![\d/])",  # day/month without a year: "17/11"
        r"\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]m)?",
        # Day-first before month-first so "16 November 2026" goes whole instead of leaving "16";
        # an optional leading day range takes "1–14 November" whole too.
        rf"\b(?:\d{{1,2}}\s*(?:[–\-]|and|or|to)\s*)?\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}(?:\s+\d{{4}})?",
        # "Nov 16, 2026" / "November 2026": (?!\d) stops a 4-digit year splitting into day "20" + "26".
        # The day may be a range ("November 20–24, 2026"); the range end must not run into a longer digit run.
        rf"\b{_MONTHS}\s+(?:\d{{1,2}}(?:\s*[–\-]\s*\d{{1,2}})?(?!\d)(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?|\d{{4}})",
        r"(?:ngày|tháng|năm|quý)\s+\d{1,4}(?:\s*[/-]\s*\d{1,4})?",
        r"\b\d+(?:st|nd|rd|th)\b",
        # Last, after the time/date forms ("21:34 ngày 17/11" must not lose "34 ngày"): elapsed spans such
        # as "35 hours old" or "90 ngày" are worked out from timestamps or the query window, not read from
        # a row. ponytail: also skips a duration that IS a data value.
        # Grouped or decimal counts ("19,375 minutes", "13.5 days") are one span; the lookbehind keeps the
        # match from starting in the middle of a longer number.
        r"(?<![\d.,])\d+(?:[.,]\d+)*[\s-]+(?:minutes?|hours?|days?|weeks?|months?|years?|phút|giờ|ngày|tuần|tháng|năm)\b",
    )
]
_PROSE_SKIPS = [
    re.compile(r"```.*?(?:```|$)", re.DOTALL),  # fenced code
    re.compile(r"`[^`\n]*`"),  # inline code
    re.compile(r"^[ \t]*\d+[.)][ \t]", re.MULTILINE),  # ordered-list markers
]
# "10.82,106.63" / "10,82,106,63": a latitude,longitude label, which _NUM_RE reads as one unparseable token.
_COORD_RE = re.compile(r"(?<![\w.,])(-?\d{1,3}[.,]\d+)\s*,\s*(-?\d{1,3}[.,]\d+)(?![\w.,])")
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
# snake_case identifier carrying digits ("temperature_2m", "wind_speed_10m", "pm25"): prose says "2 m".
_IDENT_RE = re.compile(r"(?<![\w])[a-z][a-z_]*\d[a-z0-9_]*(?![\w])")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_MARKER = re.compile(r"\[\d+\]")


@dataclass(frozen=True)
class _Number:
    """One stated number: its text and the ``(value, tolerance)`` readings it may have."""

    raw: str
    readings: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class GroundingReport:
    """Outcome of the grounding check for one answer (counts and short examples only, never content).

    ``unverified`` lists every unmatched number's text (answer first, then widgets); ``bad_widgets`` holds
    the indexes (into the checked widget list) of non-sample table/chart parts with an unmatched value.
    """

    unverified: tuple[str, ...]
    partially_uncited: bool
    bad_widgets: frozenset[int]
    derived: tuple[str, ...] = ()
    restated: tuple[str, ...] = ()
    misattributed: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        """Number of unverified numbers."""
        return len(self.unverified)

    @property
    def examples(self) -> list[str]:
        """Up to ``MAX_EXAMPLES`` distinct unverified number strings, in order of appearance."""
        return list(dict.fromkeys(self.unverified))[:MAX_EXAMPLES]

    def summary(self) -> dict[str, Any]:
        """Counts and examples as stored in message metadata and the done event.

        ``count``/``examples`` are unverified numbers only; ``derived``, ``restated`` and ``misattributed``
        are counts of the separately reported groups.
        """
        return {
            "count": self.count,
            "examples": self.examples,
            "derived": len(self.derived),
            "restated": len(self.restated),
            "misattributed": len(self.misattributed),
        }


def _parse_body(body: str) -> list[tuple[float, int]]:
    """Read a digit string with en-US/vi-VN separators as ``(value, decimals)`` candidates.

    Both separators present: the last one is the decimal point. One separator repeated: thousands. One
    separator once followed by exactly three digits ("25.400", "1,234") is ambiguous, so both the thousands
    and the decimal reading are returned. Unparseable input yields an empty list.
    """
    body = re.sub(r"[   ]", "", body)
    dots, commas = body.count("."), body.count(",")
    try:
        if dots and commas:
            dec = "." if body.rfind(".") > body.rfind(",") else ","
            thou = "," if dec == "." else "."
            return [
                (float(body.replace(thou, "").replace(dec, ".")), len(body) - body.rfind(dec) - 1)
            ]
        sep = "." if dots else "," if commas else ""
        if not sep:
            return [(float(body), 0)]
        thousands = float(body.replace(sep, ""))
        last = body.rsplit(sep, 1)[1]
        if body.count(sep) > 1:
            return [(thousands, 0)]
        decimal = float(body.replace(sep, "."))
        return [(thousands, 0), (decimal, len(last))] if len(last) == 3 else [(decimal, len(last))]
    except ValueError:
        return []


def _readings(body: str, suffix: str | None) -> tuple[tuple[float, float], ...]:
    """All finite ``(value, tolerance)`` readings of a number token.

    Tolerance is half a unit of the last shown digit (integers with trailing zeros are treated as rounded,
    capped at 5% of the value) with a 0.1% relative floor. A magnitude suffix adds the multiplied reading
    next to the raw one; ``%`` adds the fraction reading (25.4% may be stored as 0.254). Readings that
    overflow to inf/nan are dropped, so an absurd digit run yields no reading instead of raising.
    """
    out: list[tuple[float, float]] = []
    for value, decimals in _parse_body(body):
        if decimals:
            half = 0.5 * 10.0**-decimals
        else:
            digits = re.sub(r"\D", "", body)
            # Zeros are capped: 10**zeros overflows for very long digit runs (the cap only widens tolerance).
            zeros = min(len(digits) - len(digits.rstrip("0")), _MAX_ZEROS) if len(digits) > 1 else 0
            half = min(0.5 * 10.0**zeros, 0.05 * value) if zeros else 0.5
        tol = max(half, 1e-3 * value)
        if not _finite(value, tol):
            continue
        out.append((value, tol))
        mult = _SUFFIX.get((suffix or "").lower())
        if suffix == "%":
            out.append((value / 100, tol / 100))
        elif mult:
            scaled = (value * mult, max(half * mult, 1e-3 * value * mult))
            if _finite(*scaled):
                out.append(scaled)
    return tuple(out)


def _finite(*values: float) -> bool:
    """True when every float is finite (``inf``/``nan`` readings are never matchable)."""
    return all(math.isfinite(v) for v in values)


def strip_prose(text: str) -> str:
    """Blank out code spans and ordered-list markers (must run on the whole text, before sentence splitting)."""
    text = text[:_MAX_TEXT]
    for pattern in _PROSE_SKIPS:
        text = pattern.sub(" ", text)
    return text


def _strip(text: str, *, prose: bool) -> str:
    """Blank out the spans that are not data claims (citations, dates, times, ordinals; prose: code, list markers)."""
    text = strip_prose(text) if prose else text[:_MAX_TEXT]
    for pattern in _COMMON_SKIPS:
        text = pattern.sub(" ", text)
    return text


def extract_numbers(text: str, *, prose: bool = True) -> list[_Number]:
    """Numbers a text states, skipping citation markers, dates/times, ordinals, code and trivial values.

    Skipped as non-claims: URLs, bare integers 0-10 and standalone integers 1900-2100 (years). ``prose=False``
    (table cells) keeps the date/time skips but not the code/list-marker ones.
    """
    found: list[_Number] = []
    for m in _NUM_RE.finditer(_strip(text, prose=prose)):
        body, suffix = m.group("body"), m.group("suf")
        if suffix is None and re.fullmatch(r"\d{1,4}", body):
            value = int(body)
            if value <= 10 or (1900 <= value <= 2100 and len(body) == 4):
                continue
        readings = _readings(body, suffix)
        if readings:
            found.append(_Number(m.group(0).strip(), readings))
    return found


def _is_skipped_key(key: Any) -> bool:
    """True for mapping keys whose values are never data claims (citation, identifier, timestamp, URL)."""
    if not isinstance(key, str):
        return False
    return key in _SKIP_KEYS or key.endswith(_SKIP_KEY_SUFFIXES)


class KnownNumbers:
    """Sorted set of magnitudes seen in the turn's data; ``contains`` is a tolerance lookup."""

    def __init__(self, *, track_rows: bool = True) -> None:
        """Start empty; ``track_rows=False`` for a per-row instance (rows never nest)."""
        self._values: list[float] = []
        self._sorted = True
        self._data: list[float] = []  # first numeric tool values, the operands of derived figures
        self._track_rows = track_rows
        # Numbers of each cited tool row, keyed by its marker ("[1]"); bounded by ``_MAX_ROWS``.
        self.rows: dict[str, KnownNumbers] = {}
        # Numbers of earlier assistant answers in the conversation (restated-figure check).
        self.prior = KnownNumbers(track_rows=False) if track_rows else None

    def add_value(self, value: float) -> None:
        """Remember one magnitude (sign ignored); silently bounded by ``_MAX_KNOWN``."""
        if len(self._values) < _MAX_KNOWN:
            self._values.append(abs(value))
            self._sorted = False

    def add_text(self, text: str) -> None:
        """Remember every number in free text (no skips: dates/ids only add harmless known values).

        A ``lat,lon`` coordinate label also adds both halves (en-US "10.82" and vi-VN "10,82" forms).
        """
        for coord in _COORD_RE.finditer(text[:_MAX_TEXT]):
            for half in coord.groups():
                self.add_value(float(half.replace(",", ".")))
        for m in _NUM_RE.finditer(text[:_MAX_TEXT]):
            for value, _tol in _readings(m.group("body"), m.group("suf")):
                self.add_value(value)

    def add_json(self, node: Any, *, depth: int = 0) -> None:
        """Remember numeric leaves and numbers inside string leaves of a decoded tool result.

        Keys that hold identifiers, timestamps, URLs or citation bookkeeping (``_SKIP_KEYS``, ``*_id``,
        ``*_at``) are skipped entirely, so a UUID tail like ``...-000000000047`` or a ``10:30`` time cannot
        make a made-up 47 or 30 look verified. Other string leaves get the same date/time/code stripping as
        prose before their numbers are read. The length of each row list (``_COUNTED_LISTS``) is also
        remembered, so "15 rows" verifies against a 15-row result.
        """
        if depth > 8:
            return
        if isinstance(node, bool):
            return
        if isinstance(node, int | float):
            self.add_value(float(node))
            if len(self._data) < _MAX_DERIVATION_BASE and math.isfinite(node):
                self._data.append(float(node))
        elif isinstance(node, str):
            self.add_text(_strip(node, prose=False))
        elif isinstance(node, Mapping):
            self._add_row(node, depth)
            # A row list's length is a stated fact ("15 rows"); the model counts what the tool returned.
            for key in _COUNTED_LISTS:
                if isinstance(node.get(key), list):
                    self.add_value(float(len(node[key])))
            for key, child in node.items():
                if not _is_skipped_key(key):
                    self.add_json(child, depth=depth + 1)
        elif isinstance(node, list):
            for child in node:
                self.add_json(child, depth=depth + 1)

    def _add_row(self, node: Mapping[str, Any], depth: int) -> None:
        """Remember a tool row's own numbers under its ``citation`` marker (no-op for any other mapping).

        Bounded by ``_MAX_ROWS``; several rows with the same marker (an aggregate and an observation of
        one version) share one entry.
        """
        marker = node.get("citation")
        if not (self._track_rows and isinstance(marker, str) and _ROW_CITE.fullmatch(marker)):
            return
        if marker not in self.rows and len(self.rows) >= _MAX_ROWS:
            return
        row = self.rows.setdefault(marker, KnownNumbers(track_rows=False))
        for key, child in node.items():
            if not _is_skipped_key(key):
                row.add_json(child, depth=depth + 1)

    def misattributed(self, number: _Number, cited: Sequence[str]) -> bool:
        """True when ``number`` is in some other cited row but in none of the rows ``cited`` points to.

        Only decided when every marker in ``cited`` is a known tool row (an evidence or web marker has no
        row to compare, so the sentence is left alone).
        """
        if not cited or any(c not in self.rows for c in cited):
            return False
        if any(self.rows[c].contains(number) for c in cited):
            return False
        return any(row.contains(number) for key, row in self.rows.items() if key not in cited)

    def contains(self, number: _Number) -> bool:
        """True when any reading of ``number`` lies within its tolerance of a known magnitude."""
        if not self._sorted:
            self._values.sort()
            self._sorted = True
        for value, tol in number.readings:
            i = bisect.bisect_left(self._values, value - tol * 1.0001)
            if i < len(self._values) and self._values[i] <= value + tol * 1.0001:
                return True
        return False

    def derivable(self, number: _Number) -> bool:
        """True when a precise ``number`` equals |a-b|, a+b, (a+b)/2 or the % change between two data values.

        Pure, bounded (``_MAX_DERIVATION_BASE`` operands). Low-precision numbers ("14", "1.4") never qualify:
        with many operand pairs a loose tolerance would match nearly anything.
        """
        data = self._data
        for value, tol in number.readings:
            if value == 0 or tol / value > _DERIVED_MAX_REL_TOL:
                continue
            lo, hi = value - tol * 1.0001, value + tol * 1.0001
            for i, a in enumerate(data):
                for b in data[i + 1 :]:
                    diff = abs(a - b)
                    candidates = [diff, abs(a + b), abs(a + b) / 2]
                    candidates += [diff / abs(x) * 100 for x in (a, b) if x]
                    if any(lo <= c <= hi for c in candidates):
                        return True
        return False

    def __len__(self) -> int:
        """Number of remembered values."""
        return len(self._values)


def query_result_contents(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Raw ``query_collected_data`` tool-result strings of the turn's transcript (pure).

    Matches role=tool messages to the assistant tool calls of that tool by ``tool_call_id``.
    """
    ids = {
        tc.get("id")
        for m in messages
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls") or ()
        if tc.get("function", {}).get("name") == TOOL_QUERY_COLLECTED_DATA
    }
    return [
        str(m.get("content") or "")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") in ids
    ]


def query_call_arguments(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Raw JSON argument strings of the turn's ``query_collected_data`` tool calls (pure, in call order)."""
    return [
        str(tc.get("function", {}).get("arguments") or "")
        for m in messages
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls") or ()
        if tc.get("function", {}).get("name") == TOOL_QUERY_COLLECTED_DATA
    ]


def _add_identifier_digits(known: KnownNumbers, raw: str) -> None:
    """Add each digit run inside a snake_case identifier of ``raw`` to ``known`` (``temperature_2m`` -> 2).

    Mutates ``known`` only; scans at most ``_MAX_TEXT`` characters. Prose writes such units apart ("2 m").
    """
    for ident in _IDENT_RE.findall(raw[:_MAX_TEXT]):
        for digits in re.findall(r"\d+", ident):
            known.add_value(float(digits))


_MAX_COMPLEMENT = 3  # "the other N" is accepted for N = K - 1 .. K - 3 only


def _collect_counts(node: Any, out: set[int], depth: int = 0) -> None:
    """Add to ``out`` the row-list lengths (``_COUNTED_LISTS``) and ``aggregate_total_count`` of a decoded result.

    Pure apart from mutating ``out``; depth-bounded like ``KnownNumbers.add_json``. Arbitrary numeric
    fields are never counts, so a value like 15.0 in a ``magnitude`` column does not qualify.
    """
    if depth > 8:
        return
    if isinstance(node, Mapping):
        for key in _COUNTED_LISTS:
            if isinstance(node.get(key), list):
                out.add(len(node[key]))
        total = node.get("aggregate_total_count")
        if isinstance(total, int) and not isinstance(total, bool):
            out.add(total)
        for child in node.values():
            _collect_counts(child, out, depth + 1)
    elif isinstance(node, list):
        for child in node:
            _collect_counts(child, out, depth + 1)


def _add_tool_args(known: KnownNumbers, raw: str) -> None:
    """Add the numbers in one tool call's JSON arguments (filters, bounds, limits, dates) to ``known``.

    Parsed with the same id/timestamp key skipping as tool results, through a scratch instance so the
    arguments never become derivation operands of ``known``. Invalid JSON is read as plain text.
    """
    scratch = KnownNumbers(track_rows=False)
    try:
        scratch.add_json(json.loads(raw[:_MAX_TEXT]))
    except ValueError:
        scratch.add_text(raw)
    for value in scratch._values:
        known.add_value(value)


def build_known(
    tool_results: Iterable[str],
    texts: Iterable[str],
    user_text: str,
    prior_answers: Iterable[str] = (),
    *,
    tool_args: Iterable[str] = (),
    counts: Iterable[int] = (),
) -> KnownNumbers:
    """Collect known numbers from tool-result JSON, evidence/web texts and the user's message.

    Each ISO date (``YYYY-MM-DD``) in a tool result adds its year, month and day as known numbers; each digit
    run inside a lower-case snake_case identifier (``temperature_2m`` -> 2) is added the same way.
    ``prior_answers`` (earlier assistant messages) fill ``known.prior`` only: they never verify a number,
    they just let ``check_grounding`` tell a retracted old figure from a new unverified one.

    ``tool_args`` are the raw JSON arguments of the turn's own query-tool calls: their numbers (bounds,
    limits, dates) are what the answer restates, so they count as known. ``counts`` are extra known
    item counts (e.g. evidence totals). Every count K (those plus row-list lengths and
    ``aggregate_total_count`` in the tool results) also makes K-1..K-3 known ("the other 13" of 15).
    """
    known = KnownNumbers()
    count_set = {int(c) for c in counts}
    for raw_args in tool_args:
        _add_tool_args(known, raw_args)
    if known.prior is not None:
        for answer in prior_answers:
            known.prior.add_text(_strip(answer, prose=True))
    for raw in tool_results:
        # JSON walking strips ISO dates, but "May 20" in an answer is a claim read from one: keep each part.
        for part in _ISO_DATE_RE.finditer(raw[:_MAX_TEXT]):
            for value in part.groups():
                known.add_value(float(value))
        _add_identifier_digits(known, raw)
        try:
            decoded = json.loads(raw)
        except ValueError:
            known.add_text(raw)
        else:
            known.add_json(decoded)
            _collect_counts(decoded, count_set)
    for total in count_set:
        for k in range(1, _MAX_COMPLEMENT + 1):
            if total - k >= 1:
                known.add_value(float(total - k))
    for text in texts:
        known.add_text(text)
        _add_identifier_digits(known, text)
    known.add_text(user_text)
    return known


def _widget_numbers(part: Mapping[str, Any]) -> tuple[list[_Number], bool]:
    """Numbers stated by a table/chart part and whether it is a non-sample one (other parts: none)."""
    table, chart = part.get("table"), part.get("chart")
    nums: list[_Number] = []
    if isinstance(table, Mapping):
        for row in table.get("rows") or []:
            for cell in row if isinstance(row, list) else []:
                nums.extend(extract_numbers(str(cell), prose=False))
        return nums, table.get("is_sample") is not True
    if isinstance(chart, Mapping):
        x_key = chart.get("x_key")
        for point in chart.get("data") or []:
            for key, value in point.items() if isinstance(point, Mapping) else []:
                if (
                    key != x_key
                    and not isinstance(value, bool)
                    and isinstance(value, int | float | str)
                ):
                    nums.extend(extract_numbers(str(value), prose=False))
        return nums, chart.get("is_sample") is not True
    return nums, False


def check_grounding(
    answer: str, widgets: Sequence[Mapping[str, Any]], known: KnownNumbers, *, has_citation: bool
) -> GroundingReport:
    """Compare the answer's and widgets' numbers with ``known`` (pure).

    ``partially_uncited`` needs ``has_citation`` (the answer kept at least one valid citation) and at least
    one sentence with an unverified number but no ``[n]`` marker. Answer numbers explained as a derivation of
    two data values go to ``derived`` (widget cells must match the data directly). A number from an earlier
    answer next to a correction cue in its sentence or the one before goes to ``restated``. A number that is
    in the data but only in another row than the ones its sentence cites goes to ``misattributed``. A widget
    is "bad" when it is non-sample and holds at least one unverified value.
    """
    unverified: list[str] = []
    derived: list[str] = []
    restated: list[str] = []
    misattributed: list[str] = []
    partial = False
    previous = ""
    for sentence in _SENTENCES.split(strip_prose(answer)):
        missing: list[str] = []
        cited = _ROW_CITE.findall(sentence)
        for n in extract_numbers(sentence, prose=False):
            if known.contains(n):
                if known.misattributed(n, cited):
                    misattributed.append(n.raw)
            elif known.derivable(n):
                derived.append(n.raw)
            elif (
                known.prior is not None
                and known.prior.contains(n)
                and _CORRECTION.search(f"{previous} {sentence}")
            ):
                restated.append(n.raw)
            else:
                missing.append(n.raw)
        unverified.extend(missing)
        partial = partial or bool(missing and not _MARKER.search(sentence))
        previous = sentence
    bad: set[int] = set()
    for index, part in enumerate(widgets):
        nums, checked = _widget_numbers(part)
        missing = [n.raw for n in nums if not known.contains(n)] if checked else []
        if missing:
            bad.add(index)
            unverified.extend(missing)
    return GroundingReport(
        tuple(unverified),
        has_citation and partial,
        frozenset(bad),
        tuple(derived),
        tuple(restated),
        tuple(misattributed),
    )


def relabel_widgets(widgets: list[dict[str, Any]], report: GroundingReport) -> list[dict[str, Any]]:
    """Copy of ``widgets`` where each part flagged in ``report`` carries ``UNVERIFIED_SOURCE`` as its source."""
    out: list[dict[str, Any]] = []
    for index, part in enumerate(widgets):
        if index in report.bad_widgets:
            slot = "table" if isinstance(part.get("table"), dict) else "chart"
            part = {**part, slot: {**part[slot], "source": UNVERIFIED_SOURCE}}
        out.append(part)
    return out
