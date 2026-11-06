"""Characterization of ``list_observations`` paging, cursor binding and source-scope errors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import modules.ingestion.models  # resolve Observation foreign-key targets
import modules.knowledge.documents.models
import modules.sources.models  # noqa: F401
from core.errors import NotFoundError, ValidationFailedError
from core.pagination import decode_fingerprinted_cursor
from modules.knowledge.observations import public
from modules.knowledge.observations.schemas import ObservationQuery

_WS, _SRC = uuid4(), uuid4()
_FROM = datetime(2026, 10, 1, tzinfo=UTC)
_TO = datetime(2026, 10, 2, tzinfo=UTC)


def _rows(count: int) -> list[Any]:
    """Newest-first observation stand-ins with the attributes the scan reads."""
    return [
        SimpleNamespace(
            id=uuid4(),
            observed_at=_TO - timedelta(minutes=n),
            source_id=_SRC,
            source_generation=1,
            provider="prov",
            provider_scope_discriminator="d",
            external_id=f"e{n}",
            document_id=uuid4(),
            document_version_id=uuid4(),
        )
        for n in range(count)
    ]


def _patch(
    monkeypatch: pytest.MonkeyPatch, *, scope_found: bool = True, allowed: set[Any] | None = None
) -> None:
    """Patch admission, source lookups and the evidence check."""

    async def admit(session: Any, *, scope: Any, multi_workspace_enabled: bool) -> Any:
        return SimpleNamespace(
            workspace_id=_WS, user_id=1, membership_revision=1, configuration_revision=1
        )

    async def get_source(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(status="active", generation=1)

    async def get_scope(*_a: Any, **_k: Any) -> Any:
        if not scope_found:
            return None
        return SimpleNamespace(
            source_id=_SRC, source_generation=1, provider_id="prov", discriminator="d"
        )

    async def evidence(session: Any, candidates: Any, *_a: Any, **_k: Any) -> dict[Any, int]:
        return {
            c.observation_id: 3
            for c in candidates
            if allowed is None or c.observation_id in allowed
        }

    monkeypatch.setattr(public, "_admit", admit)
    monkeypatch.setattr(public.sources, "get_connector_source", get_source)
    monkeypatch.setattr(public.connectors, "get_current_provider_scope", get_scope)
    monkeypatch.setattr(public.documents, "current_observation_evidence_versions", evidence)


def _session(*batches: list[Any]) -> Any:
    """Session whose ``scalars`` returns the given batches in order, then empty."""
    pending = list(batches)

    async def scalars(_statement: Any) -> Any:
        batch = pending.pop(0) if pending else []
        return SimpleNamespace(all=lambda: batch)

    return SimpleNamespace(scalars=AsyncMock(side_effect=scalars))


async def _list(session: Any, cursor: str | None = None, limit: int = 2, **filters: Any) -> Any:
    """Call ``list_observations`` for one source."""
    query = ObservationQuery(source_ids=[_SRC], from_at=_FROM, to_at=_TO, limit=limit, **filters)
    return await public.list_observations(
        session,
        query,
        cursor,
        scope=SimpleNamespace(workspace_id=_WS),
        multi_workspace_enabled=False,
    )


async def test_extra_row_yields_cursor_bound_to_last_returned_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With limit+1 accepted rows the page is trimmed and the cursor points at the last returned row."""
    _patch(monkeypatch)
    rows = _rows(3)
    page, cursor, truncated, versions = await _list(_session(rows))
    assert page == tuple(rows[:2]) and truncated is False
    assert versions == {rows[0].id: 3, rows[1].id: 3}
    assert cursor is not None
    query = ObservationQuery(source_ids=[_SRC], from_at=_FROM, to_at=_TO, limit=2)
    fingerprint = public._cursor_fingerprint(
        query,
        (
            public.ObservationScopeRead(
                source_id=_SRC, source_generation=1, provider="prov", discriminator="d"
            ),
        ),
        access_fence=SimpleNamespace(
            workspace_id=_WS, user_id=1, membership_revision=1, configuration_revision=1
        ),
    )
    assert decode_fingerprinted_cursor(cursor, fingerprint) == (rows[1].observed_at, rows[1].id)


async def test_short_window_has_no_cursor_and_filters_unallowed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows whose evidence is not allowed are dropped; a short batch ends the scan without a cursor."""
    rows = _rows(2)
    _patch(monkeypatch, allowed={rows[1].id})
    page, cursor, truncated, versions = await _list(_session(rows))
    assert (
        page == (rows[1],) and cursor is None and truncated is False and versions == {rows[1].id: 3}
    )


async def test_scan_ceiling_marks_truncated_and_resumes_after_last_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nothing is allowed across 2,048 scanned rows the scan is truncated with a cursor at the last row."""
    _patch(monkeypatch, allowed=set())
    batches = [_rows(256) for _ in range(8)]
    page, cursor, truncated, _ = await _list(_session(*batches))
    assert page == () and truncated is True and cursor is not None


async def test_missing_provider_scope_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source without a current provider scope is a 404 DomainError with the stable detail."""
    _patch(monkeypatch, scope_found=False)
    with pytest.raises(NotFoundError) as failure:
        await _list(_session())
    assert (failure.value.status_code, failure.value.detail) == (
        404,
        "One or more sources are unavailable",
    )


async def test_cursor_from_other_query_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cursor minted for different filters is HTTP 422 'Observation cursor is invalid'."""
    _patch(monkeypatch)
    _, cursor, _, _ = await _list(_session(_rows(3)))
    with pytest.raises(ValidationFailedError) as failure:
        await _list(_session(), cursor, history=True)
    assert (failure.value.status_code, failure.value.detail) == (
        422,
        "Observation cursor is invalid",
    )
