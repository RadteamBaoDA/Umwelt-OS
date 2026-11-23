"""Offline checks for the all-provider seed plan: every fixture is accounted for and each mapper yields rows."""

from __future__ import annotations

from tests.integration.chat_eval.seed_all import NOT_SEEDED, SEEDED, _provider_records
from tests.unit.modules.connectors.p3_helpers import FIXTURES, fixture


def test_every_fixture_is_seeded_or_reported_not_seeded() -> None:
    seeded = {name for files in SEEDED.values() for name in files}
    skipped = {name for name, _, _ in NOT_SEEDED}
    on_disk = {p.name for p in FIXTURES.iterdir() if p.is_file()}
    assert seeded.isdisjoint(skipped)
    assert on_disk == seeded | skipped


def test_every_seeded_mapper_yields_records_offline() -> None:
    for provider, files in SEEDED.items():
        records = _provider_records(provider, [fixture(name) for name in files])
        assert records, provider


def test_not_seeded_rows_carry_a_reason() -> None:
    assert all(reason.strip() for _, _, reason in NOT_SEEDED)
