"""Protected-value masking for translation (stdlib ``re``; no Markdown parser dependency).

URLs, link destinations, citations, code, tickers and numeric spans are replaced by per-request
random markers before text reaches a model, then restored byte-for-byte after validation.
"""

import re
import secrets

MARKER_RE = re.compile(r"@@P[0-9a-f]{8}_\d+@@")
_CURRENCY = r"(?:USDT|USDC|USD|EUR|VND|GBP|JPY|CNY|BTC|ETH)"
_PROTECTED_RE = re.compile(
    "|".join(
        [
            r"```.*?```",  # fenced code
            r"`[^`\n]+`",  # inline code
            r"(?<=\]\()[^)\s]+",  # Markdown link destination
            r"(?:https?://|www\.)[^\s)\]>]+",  # bare URL
            r"\[\d+(?:\s*[,\-–]\s*\d+)*\]",  # citation [n]
            r"\$[A-Z][A-Z.]{0,5}\b",  # cashtag
            r"\b(?:NYSE|NASDAQ|HOSE|HNX|UPCOM|HSX):[A-Z]{1,6}\b",  # exchange-qualified ticker
            r"\(\s*[A-Z]{2,5}\s*\)",  # (AAPL)
            rf"(?:(?<![\w])[+\-−])?(?:[$€£¥₫]\s?)?\d+(?:[.,/:\-]\d+)*(?:\s?(?:%|{_CURRENCY}\b))?",  # numbers, dates
        ]
    ),
    re.DOTALL,
)
_PREFIX_RE = re.compile(r"^(?:\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*))+")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def protect_text(text: str) -> tuple[str, dict[str, str]]:
    """Return text with protected values replaced by ordered markers, plus marker -> literal."""
    nonce = secrets.token_hex(4)
    while f"@@P{nonce}_" in text:  # marker collision with the original: pick a fresh nonce
        nonce = secrets.token_hex(4)
    protected: dict[str, str] = {}

    def mask(match: re.Match[str]) -> str:
        """Replace one protected match with a unique nonce marker and remember the original value."""
        marker = f"@@P{nonce}_{len(protected)}@@"
        protected[marker] = match.group(0)
        return marker

    return _PROTECTED_RE.sub(mask, text), protected


def require_protected_markers(output: str, protected: dict[str, str]) -> None:
    """Reject missing, duplicated or reordered protected values before restoration."""
    if any(output.count(marker) != 1 for marker in protected):
        raise ValueError("translation_protected_value_mismatch")
    positions = [output.index(marker) for marker in protected]
    if positions != sorted(positions):
        raise ValueError("translation_protected_value_order")


def restore_text(text: str, protected: dict[str, str]) -> str:
    """Validate markers, reject unknown ones, then substitute the exact literals."""
    require_protected_markers(text, protected)
    if any(found not in protected for found in MARKER_RE.findall(text)):
        raise ValueError("translation_protected_value_unknown")
    return MARKER_RE.sub(lambda m: protected[m.group(0)], text)


def _tokens(text: str) -> set[str]:
    """Return the set of protected values (numbers, URLs, citations, tickers) occurring in ``text``."""
    return {m.group(0) for m in _PROTECTED_RE.finditer(text)}


# Starts with a digit: a leading "-" would let the model flip a source value's sign.
_PLAIN_NUMBER_RE = re.compile(r"\d[\d.,/:\-–]*")
# A source numeric token: optional sign/currency, then a digit (not a URL, citation, code or ticker).
_NUMERIC_TOKEN_RE = re.compile(r"(?:[+\-−])?(?:[$€£¥₫]\s?)?\d")
_MONTHS = (
    ("January", "Jan"),
    ("February", "Feb"),
    ("March", "Mar"),
    ("April", "Apr"),
    ("May", "May"),
    ("June", "Jun"),
    ("July", "Jul"),
    ("August", "Aug"),
    ("September", "Sept", "Sep"),
    ("October", "Oct"),
    ("November", "Nov"),
    ("December", "Dec"),
)
_MONTH_NUMBER = {name: n for n, names in enumerate(_MONTHS, 1) for name in names}
# Exact, capitalized names or abbreviations only ("Marketing", "Junior" and the verb "may" do not match).
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True)) + r")\b")


def _numbers(text: str) -> set[int]:
    """Return the integers of ``text``'s numeric protected tokens plus each English month name's number.

    Digits inside URLs, citations, code or tickers are not counted, so they cannot unlock a value.
    """
    values = {
        int(d)
        for m in _PROTECTED_RE.finditer(text)
        if _NUMERIC_TOKEN_RE.match(m.group(0))
        for d in re.findall(r"\d+", m.group(0))
    }
    return values | {_MONTH_NUMBER[m.group(1)] for m in _MONTH_RE.finditer(text)}


def reject_new_tokens(original: str, restored: str) -> None:
    """R12: the model may not introduce a number, URL, citation or ticker absent from the source.

    A plain number/date token (digits and separators only, no sign, currency or %) may be a
    re-formatting of source values ("October 27-28, 2026" -> "27-28/10/2026"): it passes when each
    of its integers occurs in the source, counting a month name as its month number. Anything
    else new (URL, citation, ticker, amount, a value not in the source) raises ValueError.
    """
    new = _tokens(restored) - _tokens(original)
    allowed = _numbers(original)
    for token in new:
        if (
            not _PLAIN_NUMBER_RE.fullmatch(token)
            or not {int(d) for d in re.findall(r"\d+", token)} <= allowed
        ):
            raise ValueError("translation_new_token")


def split_markdown(text: str) -> list[tuple[str, str]]:
    """Split into (verbatim prefix, translatable body); fenced code, blanks and fences get an empty body."""
    lines: list[tuple[str, str]] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append((line, ""))
        elif in_fence or not line.strip():
            lines.append((line, ""))
        else:
            prefix = _PREFIX_RE.match(line)
            cut = prefix.end() if prefix else 0
            lines.append((line[:cut], line[cut:]))
    return lines


def join_markdown(lines: list[tuple[str, str]], bodies: list[str]) -> str:
    """Rebuild text: translated bodies are consumed, in order, by lines that had a body."""
    it = iter(bodies)
    return "\n".join(prefix + next(it) if body else prefix for prefix, body in lines)
