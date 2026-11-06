"""Test helper: patch a ``modules.knowledge.documents.worker`` global at every module that looks it up.

``worker`` re-exports helpers from the private ``_worker_*`` submodules, so patching only the facade misses
call sites inside a submodule. ``patch_worker`` mirrors ``patch.object(worker, name, ...)`` (including the
auto-created mock when no replacement is given and the ``start``/``stop`` patcher API) but patches the facade
and each submodule binding the name.
"""

import inspect
from types import TracebackType
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock

from modules.knowledge.documents import worker
from tests.unit.split_patching import patch_split_attr


class patch_worker:  # mirrors the lowercase unittest.mock.patch API
    """Context manager / patcher replacing ``name`` on the worker facade and its private submodules.

    Without ``new`` a ``MagicMock`` (``AsyncMock`` for coroutine functions) built from ``mock_kwargs`` is used,
    matching ``unittest.mock.patch.object`` defaults. ``__enter__``/``start`` yield the replacement.
    """

    def __init__(self, name: str, new: Any = DEFAULT, **mock_kwargs: Any) -> None:
        """Record the target name and build the replacement now so ``start`` is cheap and repeatable."""
        if new is DEFAULT:
            factory = AsyncMock if inspect.iscoroutinefunction(getattr(worker, name)) else MagicMock
            new = factory(**mock_kwargs)
        self._name = name
        self._new = new
        self._cm: Any = None

    def start(self) -> Any:
        """Apply the patch and return the replacement."""
        self._cm = patch_split_attr(
            "modules.knowledge.documents", self._name, self._new, facade="worker"
        )
        return self._cm.__enter__()

    def stop(self) -> None:
        """Undo the patch applied by :meth:`start`."""
        self._cm.__exit__(None, None, None)

    def __enter__(self) -> Any:
        """Start the patch for a ``with`` block."""
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the patch when the ``with`` block ends."""
        self.stop()
