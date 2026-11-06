"""Patch helper for ``modules.chat.public``, a facade over the private ``modules.chat._public_*`` modules.

A helper used inside a moved implementation is looked up in the submodule that calls it, so a test replacing it
must patch every ``_public_*`` module (and the facade) that binds the name. Only those modules are touched; the
worker modules that also import some of the same names keep their originals.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import modules.chat as chat_package
from tests.unit.split_patching import private_submodules


def chat_public_modules() -> list[Any]:
    """Return ``modules.chat.public`` followed by every private ``_public_*`` implementation module."""
    return [
        importlib.import_module("modules.chat.public"),
        *private_submodules(chat_package, "public"),
    ]


def patch_chat_public(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on the chat public facade and each ``_public_*`` module that binds it.

    Raises ``AttributeError`` when no module binds ``name`` so a stale test name fails loudly.
    """
    found = False
    for module in chat_public_modules():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            found = True
    if not found:
        raise AttributeError(name)
