"""The API metrics loop samples depth for every worker queue, including the search-build queue."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.api import main as api_main
from core.worker_queues import MODEL_QUEUE
from modules.search.indexing import SEARCH_BUILD_QUEUE


@pytest.mark.parametrize("queue", [SEARCH_BUILD_QUEUE, MODEL_QUEUE])
def test_metrics_loop_samples_search_build_queue(
    monkeypatch: pytest.MonkeyPatch, queue: str
) -> None:
    """Every worker queue (including ``arq:search-build``) is passed to the API queue samplers."""
    captured: dict[str, Any] = {}

    def fake_samplers(_redis: Any, _factory: Any, queues: tuple[str, ...], **_kw: Any) -> list:
        captured["queues"] = queues
        return []

    def fake_spawn(coro: Any, _name: str) -> None:
        coro.close()  # the loops are not under test; discard the coroutine without running it

    monkeypatch.setattr(api_main, "build_api_samplers", fake_samplers)
    monkeypatch.setattr(api_main, "spawn_background", fake_spawn)
    monkeypatch.setattr(api_main, "metrics_enabled", lambda _s: True)
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(outbox_retention_days=30),
            redis=None,
            session_factory=None,
        )
    )
    api_main._start_metrics_tasks(app)  # type: ignore[arg-type]
    assert queue in captured["queues"]
