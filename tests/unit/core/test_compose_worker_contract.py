"""Compose stop_grace_period must outlast each worker's drain window (job_timeout + job_completion_wait margin)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apps.worker import main as worker_main

ROOT = Path(__file__).resolve().parents[3]


def _seconds(value: str) -> int:
    """Parse a compose duration such as ``240s``/``11m`` (only s and m are used by this repo)."""
    return int(value[:-1]) * (60 if value.endswith("m") else 1)


@pytest.mark.parametrize(("service", "settings"), [
    ("worker", worker_main.WorkerSettings),
    ("cron-worker", worker_main.CronWorkerSettings),
    ("chat-worker", worker_main.ChatWorkerSettings),
    ("model-worker", worker_main.ModelWorkerSettings),
])
def test_stop_grace_covers_job_completion_wait(service: str, settings: type) -> None:
    """SIGTERM drains running jobs for ``job_completion_wait``; compose must not SIGKILL before that."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    grace = _seconds(str(compose["services"][service]["stop_grace_period"]))
    assert grace >= settings.job_completion_wait, (service, grace, settings.job_completion_wait)


def test_worker_health_probes_match_arq_health_keys() -> None:
    """Each worker container probes the exact key its arq settings write."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for service, settings in (("worker", worker_main.WorkerSettings), ("cron-worker", worker_main.CronWorkerSettings),
                              ("chat-worker", worker_main.ChatWorkerSettings),
                              ("model-worker", worker_main.ModelWorkerSettings)):
        test = compose["services"][service]["healthcheck"]["test"]
        assert test == ["CMD", "python", "-m", "core.worker_healthcheck", settings.health_check_key], service
