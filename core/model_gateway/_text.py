"""Pure text/stream helpers of the model gateway: JSON extraction, secret redaction, think-block splitting.

Private to ``core.model_gateway``; ``client`` re-exports every name so existing imports keep working.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from openai import APIStatusError

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _json_instruction(schema: dict[str, Any]) -> dict[str, str]:
    """System message asking for exactly one JSON value matching ``schema`` (the json_schema ``schema`` field if present)."""
    body = schema.get("schema", schema) if isinstance(schema, dict) else schema
    return {
        "role": "system",
        "content": (
            "Reply with exactly one JSON value that validates against this JSON Schema. "
            "No prose, no markdown, no code fences. JSON Schema: "
            + json.dumps(body, separators=(",", ":"))
        ),
    }


def _extract_json(text: str) -> str:
    """Strip reasoning tags and code fences, then cut to the outermost JSON object/array; unparseable text is returned as-is."""
    cleaned = _THINK.sub("", text).strip()
    fenced = _FENCE.match(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            candidate = cleaned[start : end + 1]
            try:
                json.loads(candidate)
            except ValueError:
                continue
            return candidate
    return cleaned


def _with_extracted_json(response: Any) -> Any:
    """Replace the first choice's text content with its extracted JSON; other shapes pass through for caller validation."""
    choices = response.get("choices") if isinstance(response, dict) else None
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        message["content"] = _extract_json(message["content"])
    return response


# "sk-..." / "key-..." tokens, and long unbroken base64/hex runs. Best-effort, not a guarantee.
_SECRET_PATTERNS = re.compile(
    r"(?i:bearer\s+\S+)|\b(?:sk|key|pk|rk)-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9+/]{32,}={0,2}",
)


def _redact_secrets(text: str) -> str:
    """Replace API-key-like tokens, bearer credentials and long base64/hex runs with ``[redacted]``."""
    return _SECRET_PATTERNS.sub("[redacted]", text)


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


class _ThinkSplitter:
    """Incrementally split a leading inline ``<think>...</think>`` block from answer text.

    Only a block at the very start of the content (after optional whitespace) counts as reasoning;
    a tag appearing later is ordinary answer text. Tags split across chunks are held back until they
    can be decided. ``flush`` releases whatever is still held when the stream ends.
    """

    def __init__(self) -> None:
        """Start undecided: the first non-space characters decide whether a think block opens."""
        self._state = "start"
        self._held = ""

    def feed(self, text: str) -> tuple[str, str]:
        """Consume one content chunk and return ``(reasoning, answer)`` text that is safe to emit now."""
        if self._state == "answer":
            return "", text
        self._held += text
        if self._state == "start":
            lead = self._held.lstrip()
            if lead.startswith(_THINK_OPEN):
                self._state, self._held = "think", lead[len(_THINK_OPEN) :]
            elif _THINK_OPEN.startswith(lead):
                return "", ""  # possibly a split opening tag (or only whitespace so far)
            else:
                self._state, answer, self._held = "answer", self._held, ""
                return "", answer
        end = self._held.find(_THINK_CLOSE)
        if end >= 0:
            reasoning, answer = self._held[:end], self._held[end + len(_THINK_CLOSE) :].lstrip("\n")
            self._state, self._held = "answer", ""
            return reasoning, answer
        # Hold back a trailing fragment that could still grow into the closing tag.
        keep = next(
            (
                n
                for n in range(len(_THINK_CLOSE) - 1, 0, -1)
                if self._held.endswith(_THINK_CLOSE[:n])
            ),
            0,
        )
        reasoning, self._held = (
            self._held[: len(self._held) - keep],
            self._held[len(self._held) - keep :],
        )
        return reasoning, ""

    def flush(self) -> tuple[str, str]:
        """Release held text at end of stream: undecided text is answer, an unclosed block is reasoning."""
        held, state = self._held, self._state
        self._held = ""
        return (held, "") if state == "think" else ("", held)


def _split_reasoning(payload: dict[str, Any], splitters: dict[int, _ThinkSplitter]) -> None:
    """Rewrite one decoded chunk in place so reasoning rides ``delta.reasoning_content``, not ``delta.content``.

    Provider-native ``reasoning`` is folded into ``reasoning_content``; inline think blocks in content are
    split out by a per-choice ``_ThinkSplitter``. Answer content therefore never contains think tags.
    """
    for choice in payload.get("choices") or []:
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        reasoning = delta.pop("reasoning", None)
        native = delta.pop("reasoning_content", None)
        parts = [t for t in (native, reasoning) if isinstance(t, str) and t]
        content = delta.get("content")
        if isinstance(content, str):
            think, answer = splitters.setdefault(choice.get("index", 0), _ThinkSplitter()).feed(
                content
            )
            if think:
                parts.append(think)
            if answer:
                delta["content"] = answer
            else:
                delta.pop("content")
        if parts:
            delta["reasoning_content"] = "".join(parts)


def _upstream_reason(exc: APIStatusError) -> str:
    """Return ``HTTP <status>: <upstream message>`` (secret-redacted, capped at 300 chars) for owner-facing errors.

    The SDK message is the provider's own error text; it is redacted before inclusion because some
    providers echo the offending key or Authorization header back.
    """
    text = _redact_secrets(" ".join(str(exc.message or "").split()))
    return f"HTTP {exc.status_code}: {text[:300]}" if text else f"HTTP {exc.status_code}"


@dataclass(slots=True)
class _StreamState:
    """Mutable retry bookkeeping of one streaming call (see ``ModelGateway._on_stream_status_error``)."""

    extra: dict[str, Any]
    attempt: int = 0  # transient-retry budget used (0 or 1); a clamp, effort drop or transient retry each consume it
    effort_dropped: bool = (
        False  # reasoning_effort was dropped; the clamp may still use the retry after it
    )
    emitted: bool = False  # a chunk reached the caller: nothing may be retried any more
    clamped: bool = False  # the max_tokens clamp retry was used
