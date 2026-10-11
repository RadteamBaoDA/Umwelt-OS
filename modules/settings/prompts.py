"""Editable system-prompt instructions for LLM features; safety/format contracts stay in code."""

from collections.abc import Mapping

MAX_PROMPT_CHARS = 4000

# Only the instruction part is editable. Each call site appends its own fixed contract
# (injection guard, citation rules, schema/marker rules) after the resolved text.
DEFAULT_SYSTEM_PROMPTS: dict[str, str] = {
    "chat": (
        "You are BBD-OS Assistant, a personal intelligence assistant. "
        "Answer the user's inquiry accurately and factually based on the retrieved context below."
    ),
    "brief": "You write a concise personal daily brief (at most 150 words, plain text).",
    "entity_extraction": (
        "Extract explicit entity facts and direct relationships from the supplied document chunks. "
        "Do not infer identity from names. Omit uncertain facts."
    ),
    "timeline_extraction": (
        "Extract at most 30 explicit events supported by the supplied document chunks. "
        "Return no event when the text has no explicit event."
    ),
    "translation": "You are a translation engine. Translate faithfully with a natural tone.",
}


def resolve_prompt(overrides: Mapping[str, str], key: str) -> str:
    """Return the owner's override for ``key`` or the built-in default when unset/blank."""
    return (overrides.get(key) or "").strip() or DEFAULT_SYSTEM_PROMPTS[key]
