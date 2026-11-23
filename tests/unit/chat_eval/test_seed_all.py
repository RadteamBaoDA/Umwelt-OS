"""Offline checks for the all-provider seed plan: every fixture is accounted for and each mapper yields rows."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests.integration.chat_eval.seed_all import (
    NOT_SEEDED,
    SEEDED,
    _provider_records,
    normalize_failure_reason,
)
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


def test_alpha_vantage_seed_configuration_gives_a_verifiable_scope() -> None:
    """The market scope fields must come from the seed; without them the chat scope fails closed."""
    from uuid import uuid4

    from modules.connectors.service.scope import _scope_snapshot
    from tests.integration.chat_eval.seed_all import _AV_CONFIG_DUMP

    def source(configuration: dict[str, object]) -> Any:
        return SimpleNamespace(
            id=uuid4(),
            workspace_id=uuid4(),
            generation=1,
            status="active",
            provider="alpha_vantage",
            type="api",
            configuration=configuration,
        )

    assert _scope_snapshot(source({})) is None
    assert _scope_snapshot(source(dict(_AV_CONFIG_DUMP))) is not None


def test_normalize_failure_reason_names_stage_and_row_codes() -> None:
    reason = normalize_failure_reason(
        "normalization_failed", {"invalid_normalization_record": 5, "document_deleted": 1}
    )
    assert reason == (
        "normalize stage failed (error_code=normalization_failed); "
        "row error_codes: document_deleted x1, invalid_normalization_record x5"
    )


def test_normalize_failure_reason_without_codes_still_explains() -> None:
    assert normalize_failure_reason(None, {}) == (
        "normalize stage failed (error_code=none); row error_codes: none"
    )
