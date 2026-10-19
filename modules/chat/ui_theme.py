"""Assistant-requested theme change: a transient ``ui.theme`` SSE event, never an account write.

Ported from the CopilotKit OpenIntelligentUI ``toggleTheme`` frontend tool (MIT). The browser applies the mode
for the current session only and offers Keep/Revert; this module persists nothing except the replayable stream
event (the owner's stored preference stays untouched until they press Keep).
"""

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

EVENT_UI_THEME = "ui.theme"


class SetThemeArgs(BaseModel):
    """Arguments of the non-terminal ``set_theme`` tool."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["light", "dark", "system"]


async def publish_theme(session: AsyncSession, run_id: UUID, raw_args: str) -> str:
    """Validate ``set_theme`` arguments and append one ``ui.theme`` event; returns the tool-message JSON.

    The caller holds the live-run lock (so ``seq`` allocation cannot race) and commits afterwards. Invalid
    arguments append nothing and come back as ``{"error": ...}`` for the model; this never raises for them.
    """
    try:
        args = SetThemeArgs.model_validate_json(raw_args or "{}")
    except (ValidationError, ValueError) as exc:
        return json.dumps({"error": f"invalid set_theme arguments: {str(exc)[:300]}"})
    from modules.chat.user_input import (
        _append_event,  # deferred: user_input imports tool_specs, which imports this module
    )

    await _append_event(session, run_id, EVENT_UI_THEME, {"mode": args.mode})
    return json.dumps({"status": "requested", "mode": args.mode,
                       "note": "Applied for this browser session; the user decides whether to keep it."})
