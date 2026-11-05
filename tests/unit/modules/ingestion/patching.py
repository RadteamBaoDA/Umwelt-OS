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
        f"modules.ingestion.{info.name}" for info in pkgutil.iter_modules(ingestion_package.__path__)
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
            created = stack.enter_context(patch.object(module, name, shared, **kwargs if shared is DEFAULT else {}))
            if shared is DEFAULT:
                shared = created
        if shared is DEFAULT:
            raise AttributeError(name)
        yield shared
