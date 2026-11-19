"""Media projection SQL (JSONB filters, guarded casts, release-date keyset paging) on real PostgreSQL."""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modules.knowledge.documents.public import ProviderRecordMetadata

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)

PROJECTIONS = "/api/v1/documents/dashboard-projections"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(title: str, source_fields: dict[str, Any]) -> dict[str, Any]:
    """Build provider_record through the real contract so the fixture is contract-valid."""
    return ProviderRecordMetadata(
        provider="tvmaze_schedule",
        identity=title,
        timestamp_basis="collection",
        coverage="returned_snapshot",
        content_truncated=False,
        source_fields=source_fields,
    ).model_dump(mode="json")


class Seeder:
    """Insert documents, versions, chunks and provenance for one media-flavoured manual source."""

    def __init__(self, engine: AsyncEngine, source_id: UUID, workspace_id: UUID, generation: int):
        self.engine, self.source_id = engine, source_id
        self.workspace_id, self.generation = workspace_id, generation

    async def add(
        self,
        title: str,
        source_fields: dict[str, Any] | None = None,
        *,
        raw_record: dict[str, Any] | None = None,
        extra_provenance: int = 0,
        created_at: datetime | None = None,
    ) -> str:
        """Insert one ready current document; ``raw_record`` bypasses the contract (legacy rows)."""
        doc, ver = uuid4(), uuid4()
        record = raw_record if raw_record is not None else _record(title, source_fields or {})
        created = created_at or datetime.now(UTC)
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO documents (workspace_id, id, source_id, external_id, title, "
                    "current_version, content_hash, extraction_status, observed_at, created_at) "
                    "VALUES (:w, :d, :s, :e, :t, 1, :h, 'ready', :c, :c)"
                ),
                {
                    "w": self.workspace_id,
                    "d": doc,
                    "s": self.source_id,
                    "e": f"m-{uuid4().hex}",
                    "t": title,
                    "h": uuid4().hex,
                    "c": created,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO document_versions (id, document_id, version_number, content, "
                    "content_hash) VALUES (:v, :d, 1, :t, :h)"
                ),
                {"v": ver, "d": doc, "t": title, "h": uuid4().hex},
            )
            await conn.execute(
                text(
                    "INSERT INTO document_chunks (id, document_version_id, chunk_index, content, "
                    "content_hash, token_count) VALUES (gen_random_uuid(), :v, 0, :t, :h, 3)"
                ),
                {"v": ver, "t": title, "h": uuid4().hex},
            )
            for _ in range(1 + extra_provenance):
                await conn.execute(
                    text(
                        "INSERT INTO normalized_version_provenance (id, document_id, "
                        "document_version_id, provider_id, accepted_record_hash, "
                        "normalization_version, source_generation, observed_at, "
                        "selection_observed_at, title, provenance) VALUES (gen_random_uuid(), "
                        ":d, :v, :t, :h, 1, :g, now(), now(), :t, CAST(:p AS jsonb))"
                    ),
                    {
                        "d": doc,
                        "v": ver,
                        "t": title,
                        "h": uuid4().hex + uuid4().hex,  # distinct hash per row (unique identity)
                        "g": self.generation,
                        "p": json.dumps({"provider_record": record}),
                    },
                )
        return str(doc)


@pytest.fixture
async def seeder(ready_owner_client: AsyncClient, committed_engine: AsyncEngine) -> Seeder:
    created = await ready_owner_client.post(
        "/api/v1/sources", json={"type": "manual", "name": f"media {uuid4().hex[:8]}"}
    )
    created.raise_for_status()
    source_id = UUID(created.json()["id"])
    async with committed_engine.begin() as conn:
        # Manual type skips the live provider-scope check; provider only names the contract.
        await conn.execute(
            text("UPDATE sources SET provider = 'tvmaze_schedule' WHERE id = :i"), {"i": source_id}
        )
        row = (
            await conn.execute(
                text("SELECT workspace_id, generation FROM sources WHERE id = :i"), {"i": source_id}
            )
        ).one()
    return Seeder(committed_engine, source_id, row[0], row[1])


async def _fetch(
    client: AsyncClient, seeder: Seeder, *params: tuple[str, Any], expect: int = 200
) -> dict[str, Any]:
    response = await client.get(
        PROJECTIONS, params=[("source_ids", str(seeder.source_id)), *params]
    )
    assert response.status_code == expect, response.text
    return dict(response.json())


async def _ids(client: AsyncClient, seeder: Seeder, *params: tuple[str, Any]) -> set[str]:
    body = await _fetch(client, seeder, ("limit", 100), *params)
    return {item["document_id"] for item in body["items"]}


@pytest.mark.asyncio
async def test_enum_and_genre_filters(ready_owner_client: AsyncClient, seeder: Seeder) -> None:
    a = await seeder.add(
        "a",
        {
            "genres": ["action", "drama"],
            "country": "US",
            "language": "en",
            "format": "tv",
            "content_rating": "tv_14",
        },
    )
    b = await seeder.add(
        "b",
        {
            "genres": ["comedy"],
            "country": "JP",
            "language": "ja",
            "format": "movie",
            "content_rating": "tv_ma",
        },
    )
    c = await seeder.add(
        "c", {"genres": ["drama"], "country": "US", "language": "en", "format": "movie"}
    )
    client = ready_owner_client
    assert await _ids(client, seeder) == {a, b, c}
    assert await _ids(client, seeder, ("genres", "drama")) == {a, c}
    assert await _ids(client, seeder, ("genres", "comedy"), ("genres", "action")) == {a, b}
    assert await _ids(client, seeder, ("countries", "JP")) == {b}
    assert await _ids(client, seeder, ("countries", "US"), ("countries", "JP")) == {a, b, c}
    assert await _ids(client, seeder, ("languages", "en")) == {a, c}
    assert await _ids(client, seeder, ("formats", "movie")) == {b, c}
    assert await _ids(client, seeder, ("content_ratings", "tv_14")) == {a}
    assert await _ids(client, seeder, ("countries", "US"), ("formats", "movie")) == {c}


@pytest.mark.asyncio
async def test_min_rating_boundaries_and_legacy_values(
    ready_owner_client: AsyncClient, seeder: Seeder
) -> None:
    exact = await seeder.add("r77", {"rating": 77.0, "release_year": 2020})
    decimal = await seeder.add("r777", {"rating": 77.7, "release_year": 2021})
    low = await seeder.add("r769", {"rating": 76.9})
    # Legacy rows bypass the contract: non-numeric rating / release_year must not break the cast.
    legacy = await seeder.add(
        "legacy",
        raw_record={
            "provider": "tvmaze_schedule",
            "identity": "legacy",
            "timestamp_basis": "collection",
            "coverage": "returned_snapshot",
            "content_truncated": False,
            "source_fields": {"rating": "high", "release_year": "N/A"},
        },
    )
    client = ready_owner_client
    assert await _ids(client, seeder, ("min_rating", 77)) == {exact, decimal}
    assert await _ids(client, seeder, ("min_rating", 77.7)) == {decimal}
    assert await _ids(client, seeder, ("min_rating", 77.8)) == set()
    at_zero = await _ids(client, seeder, ("min_rating", 0))
    assert {exact, decimal, low} <= at_zero
    assert legacy not in at_zero
    assert await _ids(client, seeder, ("release_year", 2020)) == {exact}
    assert await _ids(client, seeder, ("release_year_from", 2020), ("release_year_to", 2021)) == {
        exact,
        decimal,
    }


def _expected_in_default_window(release_date: str, now: datetime) -> bool:
    """Mirror the SQL parse of partial dates: first instant of the period, 7 days back / 30 ahead."""
    parts = [int(part) for part in release_date.split("-")]
    start = datetime(parts[0], parts[1] if len(parts) > 1 else 1, 1, tzinfo=UTC)
    return now - timedelta(days=7) <= start <= now + timedelta(days=30)


@pytest.mark.asyncio
async def test_release_date_asc_tolerates_invalid_and_partial_dates(
    ready_owner_client: AsyncClient, seeder: Seeder
) -> None:
    now = datetime.now(UTC)
    full = await seeder.add("full", {"release_date": _iso(now + timedelta(days=2))})
    year = f"{now.year}"
    month = (now + timedelta(days=3)).strftime("%Y-%m")
    partial_year = await seeder.add("year", {"release_date": year})
    partial_month = await seeder.add("month", {"release_date": month})
    date_only = await seeder.add(
        "dateonly", {"release_date": (now + timedelta(days=4)).strftime("%Y-%m-%d")}
    )
    # Shape-valid but impossible calendar date (bypasses the contract); the SQL parse must not raise
    # and falls back to created_at, which is placed outside the window so the row is not hydrated.
    invalid = await seeder.add(
        "invalid",
        raw_record={
            "provider": "tvmaze_schedule",
            "identity": "invalid",
            "timestamp_basis": "collection",
            "coverage": "returned_snapshot",
            "content_truncated": False,
            "source_fields": {"release_date": "2026-02-30"},
        },
        created_at=now,
    )
    # The contract-invalid row is inside the window: the page must succeed without it.
    ids = await _ids(ready_owner_client, seeder, ("sort", "release_date_asc"))
    assert {full, date_only} <= ids
    assert (partial_year in ids) == _expected_in_default_window(year, now)
    assert (partial_month in ids) == _expected_in_default_window(month, now)
    assert invalid not in ids


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [None, 45])
async def test_release_date_asc_keyset_paging_with_tied_timestamps(
    ready_owner_client: AsyncClient, seeder: Seeder, window: int | None
) -> None:
    now = datetime.now(UTC)
    tied = _iso(now + timedelta(days=5))
    tied_ids = {await seeder.add(f"tie{i}", {"release_date": tied}) for i in range(5)}
    later = await seeder.add("later", {"release_date": _iso(now + timedelta(days=9))})
    expected = {*tied_ids, later}
    # Outside the default 30-day window; inside only for an explicit 45-day window.
    far = await seeder.add("far", {"release_date": _iso(now + timedelta(days=40))})
    outside_back = await seeder.add("old", {"release_date": _iso(now - timedelta(days=10))})
    extra: tuple[tuple[str, Any], ...] = (
        (("release_window_days", window),) if window is not None else ()
    )
    if window is not None:
        expected.add(far)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: list[tuple[str, Any]] = [("sort", "release_date_asc"), ("limit", 2), *extra]
        if cursor:
            params.append(("cursor", cursor))
        body = await _fetch(ready_owner_client, seeder, *params)
        assert len(body["items"]) <= 2
        seen.extend(item["document_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(seen) == len(set(seen)), "duplicate across pages"
    assert set(seen) == expected
    assert outside_back not in seen
    if window is None:
        assert far not in seen
    # Tied timestamps order by document id, then later timestamps follow.
    assert seen[:5] == sorted(tied_ids)
    assert seen[5] == later


@pytest.mark.asyncio
async def test_multiple_provenance_rows_exclude_the_version(
    ready_owner_client: AsyncClient, seeder: Seeder
) -> None:
    now = datetime.now(UTC)
    fields = {"release_date": _iso(now + timedelta(days=2)), "genres": ["drama"]}
    single = await seeder.add("single", fields)
    double = await seeder.add("double", fields, extra_provenance=1)
    for params in (
        (("sort", "release_date_asc"),),
        (("genres", "drama"),),
        (("sort", "release_date_asc"), ("genres", "drama")),
    ):
        assert await _ids(ready_owner_client, seeder, *params) == {single}, params
    assert double not in await _ids(ready_owner_client, seeder)


@pytest.mark.asyncio
async def test_release_season_filter_and_validation(
    ready_owner_client: AsyncClient, seeder: Seeder
) -> None:
    winter = await seeder.add("w", {"release_season": "winter"})
    await seeder.add("s", {"release_season": "summer"})
    assert await _ids(ready_owner_client, seeder, ("release_season", "winter")) == {winter}
    await _fetch(ready_owner_client, seeder, ("release_season", "monsoon"), expect=422)


@pytest.mark.asyncio
async def test_cursor_is_bound_to_its_filter_and_sort(
    ready_owner_client: AsyncClient, seeder: Seeder
) -> None:
    for i in range(3):
        await seeder.add(f"c{i}", {"genres": ["drama"]})
    first = await _fetch(ready_owner_client, seeder, ("limit", 1), ("genres", "drama"))
    cursor = first["next_cursor"]
    assert cursor
    await _fetch(
        ready_owner_client, seeder, ("limit", 1), ("genres", "drama"), ("cursor", cursor)
    )  # same context still works
    for other in (("genres", "comedy"), ("sort", "release_date_asc")):
        await _fetch(
            ready_owner_client,
            seeder,
            ("limit", 1),
            ("genres", "drama"),
            other,
            ("cursor", cursor),
            expect=422,
        )
