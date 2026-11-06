import json
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "providers"
NOW = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def jfixture(name: str):
    return json.loads(fixture(name))


def wd(record):
    return record.metadata["provider_record"]["world_data"]
