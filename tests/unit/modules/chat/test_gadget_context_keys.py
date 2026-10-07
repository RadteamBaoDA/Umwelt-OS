"""resolve_gadget_context must not let clients smuggle server-reserved keys."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from modules.chat.public import resolve_gadget_context


@pytest.mark.asyncio
async def test_non_selection_context_drops_private_and_selected_only_keys() -> None:
    """Client-sent _web_search, _chat_privacy_fence and selected_only are stripped."""
    context: dict[str, Any] = {
        "kind": "page",
        "page": "news",
        "_web_search": {"allowed": True},
        "_chat_privacy_fence": {"x": 1},
        "selected_only": True,
    }
    result = await resolve_gadget_context(AsyncMock(), context)
    assert result == {"kind": "page", "page": "news"}
