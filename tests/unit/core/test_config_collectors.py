from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core.config import Settings
from modules.connectors.catalog import _ENTRIES

ROOT = Path(__file__).resolve().parents[3]


def test_collector_scheduler_flag_defaults_on_and_reads_env(monkeypatch) -> None:
    assert Settings().collector_scheduler_enabled is True
    monkeypatch.setenv("COLLECTOR_SCHEDULER_ENABLED", "false")
    assert Settings().collector_scheduler_enabled is False


def test_compose_split_keeps_browser_and_n8n_separate() -> None:
    browser = yaml.safe_load((ROOT / "docker-compose.browser.yml").read_text())
    legacy = yaml.safe_load((ROOT / "docker-compose.connectors.yml").read_text())
    assert "browser" in browser["services"] and "n8n" not in browser["services"]
    assert {"connectors_internal", "browser_egress"} <= set(browser["networks"])
    assert legacy["include"] == ["docker-compose.browser.yml"]
    assert "n8n" in legacy["services"] and "browser" not in legacy["services"]
    assert "n8n_egress" in legacy["networks"]


def test_catalog_requires_service() -> None:
    by_id = {e.provider_id: e.requires_service for e in _ENTRIES}
    assert by_id["browser"] == "browser" and by_id["web"] == "browser"
    assert by_id["rss"] is None


def test_redis_url_without_password_rejected_outside_dev(monkeypatch) -> None:
    """Non-dev/test environments (including "auto") refuse an unauthenticated Redis; dev/test accept it."""
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("ENVIRONMENT", "development")
    Settings()  # explicit development keeps working
    for env in ("auto", "production"):
        monkeypatch.setenv("ENVIRONMENT", env)
        monkeypatch.setenv("PUBLIC_ORIGIN", "http://localhost:3000")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:realpw@postgres:5432/d")
        with pytest.raises(ValidationError, match="redis_url must include a password"):
            Settings()
    monkeypatch.setenv("REDIS_URL", "redis://:s3cret@redis:6379/0")
    with pytest.raises(ValidationError) as exc:  # other template secrets still fail, but not redis_url
        Settings()
    assert "redis_url" not in str(exc.value)
