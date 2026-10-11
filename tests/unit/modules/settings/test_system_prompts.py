"""System prompt overrides: resolution and fixed contracts that must survive customization."""
from uuid import uuid4

from modules.knowledge.entities.extraction import extraction_messages as entity_messages
from modules.settings.prompts import DEFAULT_SYSTEM_PROMPTS, resolve_prompt
from modules.timeline.extraction import extraction_messages as timeline_messages


def test_resolve_prompt_default_override_and_blank():
    assert resolve_prompt({}, "chat") == DEFAULT_SYSTEM_PROMPTS["chat"]
    assert resolve_prompt({"chat": "  "}, "chat") == DEFAULT_SYSTEM_PROMPTS["chat"]
    assert resolve_prompt({"chat": " custom "}, "chat") == "custom"


def test_extraction_keeps_untrusted_guard_with_custom_instruction():
    chunks = [(uuid4(), "text")]
    for msg in (entity_messages(chunks, "CUSTOM")[0], timeline_messages(chunks, [], "CUSTOM")[0]):
        assert msg["content"].startswith("CUSTOM")
        assert "never as instructions" in msg["content"] or "never instructions" in msg["content"]
