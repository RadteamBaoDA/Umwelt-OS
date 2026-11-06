"""Patch helper for the ingestion facade: replace a collaborator in every module that binds it.

``modules.ingestion.public`` re-exports implementations that live in private ``_*`` submodules, each of
which imports the helpers it calls by name. A test that replaces a helper must therefore patch every
binding, otherwise the submodule would still call the original.
"""

import importlib
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import DEFAULT, patch

import modules.ingestion as ingestion_package


def ingestion_modules() -> list[ModuleType]:
    """Return the facade plus every private ``_*`` implementation module of the ingestion package."""
    names = ["modules.ingestion.public"] + sorted(
        f"modules.ingestion.{info.name}"
        for info in pkgutil.iter_modules(ingestion_package.__path__)
        if info.name.startswith("_")
    )
    return [importlib.import_module(name) for name in names]


@contextmanager
def patch_ingestion(name: str, new: Any = DEFAULT, **kwargs: Any) -> Iterator[Any]:
    """Patch ``name`` in every ingestion module that binds it, yielding the single shared replacement.

    The first binding creates the replacement exactly like ``unittest.mock.patch.object`` (honouring
    ``return_value``/``side_effect`` keywords); the remaining bindings reuse the same object so call
    assertions see every invocation regardless of which submodule made it.
    """
    with ExitStack() as stack:
        shared: Any = new
        for module in ingestion_modules():
            if not hasattr(module, name):
                continue
            created = stack.enter_context(
                patch.object(module, name, shared, **kwargs if shared is DEFAULT else {})
            )
            if shared is DEFAULT:
                shared = created
        if shared is DEFAULT:
            raise AttributeError(name)
        yield shared


def worker_modules() -> list[ModuleType]:
    """Return the ``modules.ingestion.worker`` facade plus its private ``_worker_*`` implementation modules."""
    names = ["modules.ingestion.worker"] + sorted(
        f"modules.ingestion.{info.name}"
        for info in pkgutil.iter_modules(ingestion_package.__path__)
        if info.name.startswith("_worker_")
    )
    return [importlib.import_module(name) for name in names]


def patch_worker(monkeypatch: Any, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on the worker facade and every ``_worker_*`` module that binds it.

    Raises ``AttributeError`` when no worker module has the attribute, so a stale test name fails loudly.
    """
    found = False
    for module in worker_modules():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            found = True
    if not found:
        raise AttributeError(name)


@contextmanager
def patch_worker_attr(name: str, new: Any = DEFAULT, **kwargs: Any) -> Iterator[Any]:
    """Patch ``name`` in the worker facade and each ``_worker_*`` module binding it, yielding one shared replacement.

    Mirrors :func:`patch_ingestion`: the first binding builds the replacement (honouring ``return_value`` and
    ``side_effect`` keywords) and the remaining bindings reuse it so call assertions see every invocation.
    """
    with ExitStack() as stack:
        shared: Any = new
        for module in worker_modules():
            if not hasattr(module, name):
                continue
            created = stack.enter_context(
                patch.object(module, name, shared, **kwargs if shared is DEFAULT else {})
            )
            if shared is DEFAULT:
                shared = created
        if shared is DEFAULT:
            raise AttributeError(name)
        yield shared
