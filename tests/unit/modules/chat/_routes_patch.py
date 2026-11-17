"""Test helper: patch a ``modules.chat.routes`` global at every module that looks it up.

``modules.chat.routes`` re-exports helpers from the private ``_routes_*`` submodules, so patching only the
facade misses call sites inside a submodule. This sets the attribute on the facade and on each private
submodule that binds the name, matching the former single-module namespace.
"""

from typing import Any

import pytest

from tests.unit.split_patching import patch_split


def patch_routes(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on ``modules.chat.routes`` and every private chat module binding it."""
    patch_split(monkeypatch, "modules.chat", name, value, facade="routes")
