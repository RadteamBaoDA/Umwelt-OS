"""Patch helpers for the Documents package, whose implementation is split across private modules.

``modules.knowledge.documents.public`` is a facade that re-exports names bound in the private
``_*`` implementation modules. Intra-package calls resolve a name in the module that *uses* it,
so a test that stubs a collaborator must replace the binding in every module that holds it (the
facade included). These helpers do that in one step so tests keep asserting the same behaviour
without hard-coding which private module owns which function.
"""

import contextlib
import importlib
import pkgutil
from types import ModuleType
from typing import Any
from unittest.mock import patch

import modules.knowledge.documents as _package
import modules.knowledge.documents.public as _facade

_UNSET: Any = object()


def documents_modules(name: str) -> list[ModuleType]:
    """Return the facade and every private Documents module that binds ``name``.

    Raises:
        AttributeError: no Documents module defines or imports ``name``.
    """
    found = [_facade] if hasattr(_facade, name) else []
    for info in pkgutil.iter_modules(_package.__path__):
        if not info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_package.__name__}.{info.name}")
        if name in vars(module):
            found.append(module)
    if not found:
        raise AttributeError(f"no Documents module binds {name!r}")
    return found


class patch_documents:
    """Replace ``name`` in every Documents module that binds it (context manager or start/stop).

    ``value`` omitted behaves like ``unittest.mock.patch`` (a mock configured by ``kwargs``),
    shared by all the patched bindings so call assertions see every call.
    """

    def __init__(self, name: str, value: Any = _UNSET, **kwargs: Any) -> None:
        """Remember the target name, replacement and mock options without patching yet."""
        self._name = name
        self._value = value
        self._kwargs = kwargs
        self._stack: contextlib.ExitStack | None = None

    def start(self) -> Any:
        """Apply the patch to every binding and return the replacement object."""
        modules_ = documents_modules(self._name)
        stack = contextlib.ExitStack()
        value = self._value
        if value is _UNSET:
            value = stack.enter_context(patch.object(modules_[0], self._name, **self._kwargs))
            modules_ = modules_[1:]
        for module in modules_:
            stack.enter_context(patch.object(module, self._name, value))
        self._stack = stack
        return value

    def stop(self) -> None:
        """Restore every patched binding."""
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    def __enter__(self) -> Any:
        """Start patching for a ``with`` block."""
        return self.start()

    def __exit__(self, *exc: object) -> None:
        """Stop patching when the ``with`` block ends."""
        self.stop()


def monkeypatch_documents(monkeypatch: Any, name: str, value: Any) -> None:
    """``monkeypatch.setattr`` ``name`` in every Documents module that binds it."""
    for module in documents_modules(name):
        monkeypatch.setattr(module, name, value)
