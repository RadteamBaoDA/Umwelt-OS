"""Backend Generative UI subsystem for Umwelt-OS chat turns (model-driven presentation tools)."""

from modules.chat.generative_ui.prompt import (
    CANONICAL_SANDBOX_PARAM_ORDER,
    SYSTEM_PROMPT,
    SandboxedUiArgs,
    build_generative_ui_system_prompt,
)
from modules.chat.generative_ui.skill_backend import list_skills, load_skill
from modules.chat.generative_ui.streaming import (
    ToolCallAccumulator,
    fallback_generative_ui,
    finalize_generative_ui,
    skill_tool_result,
    streaming_snapshot,
)
from modules.chat.generative_ui.tool_specs import (
    TERMINAL_UI_TOOLS,
    TOOL_READ_SKILL,
    build_chat_tool_specs,
    parse_ui_tool_args,
)

__all__ = [
    "CANONICAL_SANDBOX_PARAM_ORDER",
    "SYSTEM_PROMPT",
    "TERMINAL_UI_TOOLS",
    "TOOL_READ_SKILL",
    "SandboxedUiArgs",
    "ToolCallAccumulator",
    "build_chat_tool_specs",
    "build_generative_ui_system_prompt",
    "fallback_generative_ui",
    "finalize_generative_ui",
    "list_skills",
    "load_skill",
    "parse_ui_tool_args",
    "skill_tool_result",
    "streaming_snapshot",
]
