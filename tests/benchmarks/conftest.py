"""Benchmark micro-tests are opt-in: they are skipped unless ``RUN_BENCHMARKS=1`` is set."""

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``benchmark``-marked test unless ``RUN_BENCHMARKS=1``, keeping default runs fast and stable.

    Wall-clock numbers are noisy under shared CI load, so they never run as part of the normal suite.
    """
    if os.getenv("RUN_BENCHMARKS") == "1":
        return
    skip = pytest.mark.skip(reason="benchmark: set RUN_BENCHMARKS=1 to run (docs/performance-report.md)")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip)
