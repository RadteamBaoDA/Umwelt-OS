"""Seed contract-valid media documents into one existing manual source of the DISPOSABLE e2e stack.

Usage: ``uv run python tests/e2e/seed_media_fixture.py <source_id> [rows]`` (reads ``TEST_DATABASE_URL``, which
``scripts/dev.ps1 test`` exports). Refuses any database other than ``bbd_test``. The rows exist so the media_feed /
media_calendar gadget specs never depend on a live provider: genres, country, language, format, content_rating,
rating, https image_url, release dates 1-6 days back and 1-29 days ahead, and more rows than one projection page.
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from modules.knowledge.documents.public import ProviderRecordMetadata

GENRES = ["action", "drama", "comedy", "fantasy"]
COUNTRIES = ["US", "JP", "GB"]


def _fields(i: int, now: datetime) -> dict[str, object]:
    """Source fields for row ``i``: alternating past/upcoming releases, cycling genres and countries."""
    days = -(1 + i % 6) if i % 2 == 0 else 1 + i % 29
    return {
        "title": f"E2E media fixture {i:03d}",
        "media_type": "anime",  # the projection yields a media DTO only when media_type is present
        "genres": [GENRES[i % 4], GENRES[(i + 1) % 4]],
        "country": COUNTRIES[i % 3],
        "language": "ja" if i % 3 == 1 else "en",
        "format": "tv" if i % 2 else "movie",
        "content_rating": "tv_14" if i % 2 else "tv_ma",
        "rating": 50.0 + i % 50,
        "release_date": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "image_url": f"https://example.com/e2e-media/{i}.jpg",
    }


async def main(source_id: UUID, rows: int) -> None:
    """Insert ``rows`` ready documents (version, chunk, single provenance row) for ``source_id``."""
    url = os.environ["TEST_DATABASE_URL"]
    if not url.rstrip("/").endswith("/bbd_test"):
        raise SystemExit("refusing to seed a database other than bbd_test")
    engine = create_async_engine(url)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        # Manual sources skip the live provider-scope check; provider only names the media field contract.
        await conn.execute(
            text("UPDATE sources SET provider = 'tvmaze_schedule' WHERE id = :i"), {"i": source_id}
        )
        ws, gen = (
            await conn.execute(
                text("SELECT workspace_id, generation FROM sources WHERE id = :i"), {"i": source_id}
            )
        ).one()
        for i in range(rows):
            fields = _fields(i, now)
            title = str(fields["title"])
            record = ProviderRecordMetadata(
                provider="tvmaze_schedule",
                identity=f"e2e-fixture-{i}",
                timestamp_basis="collection",
                coverage="returned_snapshot",
                content_truncated=False,
                source_fields=fields,
            ).model_dump(mode="json")
            doc, ver = uuid4(), uuid4()
            params = {
                "w": ws,
                "d": doc,
                "v": ver,
                "s": source_id,
                "t": title,
                "g": gen,
                "c": now,
                "x": f"fx-{doc.hex}",
                "h": ver.hex,
                "h2": doc.hex + ver.hex,
            }
            await conn.execute(
                text(
                    "INSERT INTO documents (workspace_id, id, source_id, external_id, title, "
                    "current_version, content_hash, extraction_status, observed_at, created_at) "
                    "VALUES (:w, :d, :s, :x, :t, 1, :h, 'ready', :c, :c)"
                ),
                params,
            )
            await conn.execute(
                text(
                    "INSERT INTO document_versions (id, document_id, version_number, content, "
                    "content_hash) VALUES (:v, :d, 1, :t, :h)"
                ),
                params,
            )
            await conn.execute(
                text(
                    "INSERT INTO document_chunks (id, document_version_id, chunk_index, content, "
                    "content_hash, token_count) VALUES (gen_random_uuid(), :v, 0, :t, :h, 3)"
                ),
                params,
            )
            await conn.execute(
                text(
                    "INSERT INTO normalized_version_provenance (id, document_id, "
                    "document_version_id, provider_id, accepted_record_hash, "
                    "normalization_version, source_generation, observed_at, selection_observed_at, "
                    "title, provenance) VALUES (gen_random_uuid(), :d, :v, :t, :h2, 1, :g, "
                    ":c, :c, :t, CAST(:p AS jsonb))"
                ),
                {**params, "p": json.dumps({"provider_record": record})},
            )
    await engine.dispose()
    print(f"seeded {rows} media documents into {source_id}")


if __name__ == "__main__":
    asyncio.run(main(UUID(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 130))
