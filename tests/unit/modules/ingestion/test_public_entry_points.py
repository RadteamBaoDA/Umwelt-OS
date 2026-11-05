"""Entry-point contract of the ingestion facade; independent of the private helper layout.

These assertions only use names exported by ``modules.ingestion.public`` and hold both before and after the
module split, so they pin that ``publish_event`` and ``retry_run`` kept their
public shape (coroutine functions, parameter names, keyword-only scope arguments).
"""

from __future__ import annotations

import inspect

import pytest

from modules.ingestion import public as ingestion


@pytest.mark.parametrize("name", ["publish_event", "retry_run"])
def test_entry_point_is_exported_coroutine(name: str) -> None:
    """The facade exports each entry point and it stays awaitable."""
    assert name in ingestion.__all__
    assert inspect.iscoroutinefunction(getattr(ingestion, name))


def test_publish_event_signature_is_stable() -> None:
    """``publish_event(session, event, *, scope, multi_workspace_enabled)``."""
    params = inspect.signature(ingestion.publish_event).parameters
    assert list(params) == ["session", "event", "scope", "multi_workspace_enabled"]
    assert params["scope"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["multi_workspace_enabled"].kind is inspect.Parameter.KEYWORD_ONLY


def test_retry_run_signature_is_stable() -> None:
    """``retry_run(session, run_id, requested_stage_key=None, *, scope, multi_workspace_enabled)``."""
    params = inspect.signature(ingestion.retry_run).parameters
    assert list(params) == ["session", "run_id", "requested_stage_key", "scope", "multi_workspace_enabled"]
    assert params["requested_stage_key"].default is None
    assert params["scope"].kind is inspect.Parameter.KEYWORD_ONLY
