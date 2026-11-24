"""Write ``meta.json`` for a frozen real-data collection snapshot.

Usage: ``python -m tests.integration.chat_eval.snapshot_meta <snapshot-dir>``. The DSN comes from
``EVAL_COLLECT_DSN`` (default: the collection stack's Postgres on 127.0.0.1:45432).

Every source of the collection stack that is not ``local_only`` is read: the stack is single-purpose, so
built-in sources (named by provider) and ``evalc-`` sources both count. ``local_only`` rows never reach
the anchor, the counts or meta.json. Two sources that map to one source key raise. Read-only: SELECTs only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TZ_NAME = "Asia/Ho_Chi_Minh"
SOURCE_PREFIX = "evalc-"  # stripped from source names that have no provider value
DEFAULT_DSN = "postgresql+asyncpg://bbd_test:bbd-os-test-db-only@127.0.0.1:45432/bbd_test"

# Document collection time is the version row's ``created_at`` (documents carry no collected_at).
_DOC_STATS_SQL = """
SELECT s.id::text AS source_id, s.name, s.provider, count(*) AS n,
       min(v.created_at) AS first_at, max(v.created_at) AS last_at
FROM documents d
JOIN document_versions v ON v.document_id = d.id AND v.version_number = d.current_version
JOIN sources s ON s.id = d.source_id
WHERE s.local_only IS FALSE
GROUP BY s.id, s.name, s.provider
"""

_OBS_STATS_SQL = """
SELECT s.id::text AS source_id, s.name, s.provider, count(*) AS n,
       min(o.collected_at) AS first_at, max(o.collected_at) AS last_at
FROM observations o
JOIN sources s ON s.id = o.source_id
WHERE s.local_only IS FALSE AND o.is_current
GROUP BY s.id, s.name, s.provider
"""


def source_key(provider: str | None, name: str) -> str:
    """The stable key used in gold keys: the provider, else the source name without ``evalc-``.

    Pure. ``provider`` wins when set because it is the connector identity; the name fallback covers
    built-in sources that have no provider column value.
    """
    if provider:
        return provider
    return name.removeprefix(SOURCE_PREFIX)


def anchor_utc(*times: datetime | None) -> datetime | None:
    """The latest non-null timestamp, or None when every input is None. Pure.

    The anchor is the snapshot's "now": the chat clock and the question drafting both use it, so it
    must be the newest collection time, not the wall clock at dump time.
    """
    present = [t for t in times if t is not None]
    return max(present) if present else None


def build_meta(
    *,
    anchor: datetime | None,
    revision: str,
    sources: dict[str, str],
    stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The ``meta.json`` payload (pure): anchor as ISO-8601 UTC, tz, Alembic head, sources and stats."""
    return {
        "anchor_utc": anchor.isoformat() if anchor is not None else None,
        "tz": TZ_NAME,
        "alembic_revision": revision,
        "sources": dict(sorted(sources.items())),
        "source_stats": {k: stats[k] for k in sorted(stats)},
    }


async def _aggregates(conn: Any) -> tuple[list[Any], list[Any], str]:
    """Run the two per-source aggregates and the Alembic head on one connection (thin I/O)."""
    revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    docs = (await conn.execute(text(_DOC_STATS_SQL))).all()
    obs = (await conn.execute(text(_OBS_STATS_SQL))).all()
    return list(docs), list(obs), str(revision)


def _merge_stats(
    docs: list[Any], obs: list[Any]
) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[datetime]]:
    """Fold the document and observation aggregates per source key (pure).

    Returns ``(source_id by key, stats by key, every timestamp seen)``. Counts are kept per kind;
    first/last are the min/max across both kinds so one source reports one collection window.
    Raises ValueError when two different source ids map to one source key: the gold keys and the
    stats would silently merge two sources, so the snapshot is refused instead.
    """
    sources: dict[str, str] = {}
    stats: dict[str, dict[str, Any]] = {}
    seen: list[datetime] = []
    for kind, rows in (("documents", docs), ("observations", obs)):
        for row in rows:
            key = source_key(row.provider, row.name)
            prior = sources.get(key)
            if prior is not None and prior != row.source_id:
                raise ValueError(
                    f"source key {key!r} maps to two sources: {prior} and {row.source_id}"
                )
            sources[key] = row.source_id
            entry = stats.setdefault(
                key, {"source_id": row.source_id, "documents": 0, "observations": 0}
            )
            entry[kind] = int(row.n)
            entry["first_collected_at"] = _min_time(entry.get("first_collected_at"), row.first_at)
            entry["last_collected_at"] = _max_time(entry.get("last_collected_at"), row.last_at)
            seen.extend(t for t in (row.first_at, row.last_at) if t is not None)
    for entry in stats.values():
        for field in ("first_collected_at", "last_collected_at"):
            value = entry.get(field)
            entry[field] = value.isoformat() if isinstance(value, datetime) else None
    return sources, stats, seen


def _min_time(current: datetime | None, candidate: datetime | None) -> datetime | None:
    """The earlier of two optional timestamps; None only when both are None (pure)."""
    if current is None or candidate is None:
        return current or candidate
    return min(current, candidate)


def _max_time(current: datetime | None, candidate: datetime | None) -> datetime | None:
    """The later of two optional timestamps; None only when both are None (pure)."""
    if current is None or candidate is None:
        return current or candidate
    return max(current, candidate)


async def read_meta(dsn: str) -> dict[str, Any]:
    """Query the collection DB and return the ``meta.json`` payload.

    Read-only. Connection or SQL errors propagate, so the caller writes nothing on failure.
    """
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            docs, obs, revision = await _aggregates(conn)
    finally:
        await engine.dispose()
    sources, stats, seen = _merge_stats(docs, obs)
    anchor = anchor_utc(*seen)
    return build_meta(anchor=anchor, revision=revision, sources=sources, stats=stats)


def write_meta(snapdir: Path, meta: dict[str, Any]) -> Path:
    """Write ``meta.json`` into ``snapdir`` (UTF-8, sorted keys) and return its path."""
    path = snapdir / "meta.json"
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return path


def main(argv: list[str]) -> int:
    """CLI entry: ``snapshot_meta <snapshot-dir>``; returns a process exit code."""
    if len(argv) != 1:
        print("usage: snapshot_meta <snapshot-dir>", file=sys.stderr)
        return 2
    snapdir = Path(argv[0])
    if not snapdir.is_dir():
        print(f"not a directory: {snapdir}", file=sys.stderr)
        return 2
    dsn = os.environ.get("EVAL_COLLECT_DSN", DEFAULT_DSN)
    meta = asyncio.run(read_meta(dsn))
    print(write_meta(snapdir, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
