"""Real-data eval support: load owner-accepted questions and seed the eval from a restored snapshot.

The snapshot (``snapshot.dump`` + ``meta.json`` + reviewed ``questions.csv``) is produced by
``scripts/eval-collect.ps1``, ``snapshot_meta`` and ``draft_real``. ``scripts/dev.ps1 test
-EvalSnapshot <dir>`` restores it into the disposable stack; this module then reads the restored
database. Gold keys are ``<source_key>/<external_id>`` (split on the first ``/``).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.planted import resolve_doc_versions
from tests.integration.test_chat_collected_data_matrix import Matrix

MAX_ANCHOR_AGE = timedelta(days=80)  # db_query's default aggregate window is a rolling 90 days
_LANGS = frozenset({"en", "vi"})


def _split(value: str, sep: str) -> tuple[str, ...]:
    """Non-blank, stripped parts of a separator-joined CSV cell (pure)."""
    return tuple(part.strip() for part in value.split(sep) if part.strip())


def _case_from_row(row: dict[str, str]) -> Case:
    """One accepted CSV row as a ``Case``; ValueError names the row id and the bad cell (pure)."""
    rid = row.get("id", "").strip() or "<no id>"
    try:
        nums = tuple(float(n) for n in _split(row.get("nums", ""), ";"))
    except ValueError as exc:
        raise ValueError(f"row {rid}: bad nums {row.get('nums')!r}") from exc
    empty = row.get("empty", "").strip() or "0"
    if empty not in ("0", "1"):
        raise ValueError(f"row {rid}: empty must be 0 or 1, got {empty!r}")
    lang = row.get("lang", "").strip()
    if lang not in _LANGS:
        raise ValueError(f"row {rid}: lang must be en or vi, got {lang!r}")
    raw_recall = row.get("min_recall", "").strip()
    try:
        min_recall = float(raw_recall) if raw_recall else None
    except ValueError as exc:
        raise ValueError(f"row {rid}: bad min_recall {raw_recall!r}") from exc
    return Case(
        id=rid,
        cat=row.get("cat", "").strip(),
        lang=lang,
        q=row.get("q", "").strip(),
        nums=nums,
        texts=_split(row.get("texts", ""), "|"),
        empty=empty == "1",
        gold_keys=_split(row.get("gold_keys", ""), ";"),
        source_scope=_split(row.get("source_scope", ""), ";"),
        min_recall=min_recall,
        gold_facts=_split(row.get("gold_facts", ""), "|"),
    )


def load_real_cases(csv_path: Path) -> tuple[Case, ...]:
    """Cases for the rows the owner marked ``accept`` in ``questions.csv`` (utf-8-sig).

    Drop and blank decisions are skipped. ``gold_obs`` is ignored: observations carry no document
    version id, so they are graded by ``nums``/judge only. Raises ValueError naming the row on a bad
    ``nums``, ``empty``, ``lang`` or ``min_recall`` cell.
    """
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return tuple(
        _case_from_row(r) for r in rows if (r.get("decision") or "").strip().lower() == "accept"
    )


def reconcile_scopes(cases: tuple[Case, ...], known: Collection[str]) -> tuple[Case, ...]:
    """Drop source-scope keys the snapshot does not hold from the ``empty`` cases (pure).

    An empty case about an uncollected provider asks whether the workspace holds that data, so it runs
    unscoped (an empty scope) and expects no data. An unknown key on a non-empty case would silently widen
    its scope, so it raises ValueError listing every offending case id and key.
    """
    bad = [
        f"{case.id}: {key}"
        for case in cases
        if not case.empty
        for key in case.source_scope
        if key not in known
    ]
    if bad:
        raise ValueError("source_scope key not in snapshot: " + "; ".join(bad))
    return tuple(
        replace(case, source_scope=tuple(k for k in case.source_scope if k in known))
        if case.empty
        else case
        for case in cases
    )


def split_gold_key(key: str) -> tuple[str, str]:
    """``(source_key, external_id)`` from a gold key, splitting on the FIRST ``/`` (pure).

    External ids may themselves contain ``/`` (URLs), source keys never do. ValueError if no ``/``.
    """
    source_key, sep, external_id = key.partition("/")
    if not sep or not source_key or not external_id:
        raise ValueError(f"gold key {key!r} is not <source_key>/<external_id>")
    return source_key, external_id


def read_meta(snapdir: Path) -> dict[str, Any]:
    """The snapshot's ``meta.json`` as a dict (raises FileNotFoundError when absent)."""
    data: dict[str, Any] = json.loads((snapdir / "meta.json").read_text(encoding="utf-8"))
    return data


def anchor_from_meta(snapdir: Path) -> tuple[datetime, str]:
    """``(anchor_utc as aware datetime, IANA tz name)`` from ``meta.json``; ValueError if no anchor."""
    meta = read_meta(snapdir)
    raw = meta.get("anchor_utc")
    if not raw:
        raise ValueError(f"{snapdir / 'meta.json'} has no anchor_utc (the snapshot holds no data)")
    anchor = datetime.fromisoformat(raw)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return anchor, str(meta.get("tz") or "UTC")


def format_anchor(anchor: datetime, tz: str) -> str:
    """The anchor as the production prompt line ``YYYY-MM-DD HH:MM (<tz>)`` (pure).

    Mirrors ``modules.chat._worker.setup._local_now_text`` so the frozen chat clock reads the same.
    """
    return f"{anchor.astimezone(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M')} ({tz})"


def ensure_fresh(anchor: datetime, now: datetime | None = None) -> None:
    """Raise RuntimeError when the snapshot anchor is older than 80 days (pure).

    The chat query tool defaults aggregates to a rolling 90 day window that is not frozen, so an
    older snapshot would silently return no rows.
    """
    age = (now or datetime.now(UTC)) - anchor
    if age > MAX_ANCHOR_AGE:
        raise RuntimeError(f"snapshot anchor {anchor.isoformat()} is {age.days} days old (max 80)")


async def seed_real(
    engine: AsyncEngine, snapdir: Path, cases: tuple[Case, ...]
) -> tuple[Matrix, dict[str, str]]:
    """Matrix and gold ``key -> document_version_id`` map from the restored snapshot database.

    The matrix takes the owner's first workspace (as the synthetic seed does) and its sources from
    ``meta.json``; nothing is written. Raises RuntimeError listing every gold key of ``cases`` that
    has no current document version (stale CSV or wrong snapshot).
    """
    async with engine.connect() as conn:
        ws, owner = (
            await conn.execute(
                text("SELECT id, owner_user_id FROM workspaces ORDER BY created_at LIMIT 1")
            )
        ).one()
        revision: int = (
            await conn.execute(
                text(
                    "SELECT revision FROM workspace_memberships "
                    "WHERE workspace_id = :w AND role = 'owner'"
                ),
                {"w": ws},
            )
        ).scalar_one()
    matrix = Matrix(UUID(str(ws)), int(str(owner)), int(revision))
    matrix.sources.update({k: UUID(v) for k, v in read_meta(snapdir)["sources"].items()})
    targets: dict[str, tuple[str, str]] = {}
    for key in sorted({k for case in cases for k in case.gold_keys}):
        source_key, external_id = split_gold_key(key)
        if source_key not in matrix.sources:
            raise RuntimeError(f"gold key {key!r}: source {source_key!r} is not in meta.json")
        targets[key] = (str(matrix.sources[source_key]), external_id)
    key_map = await resolve_doc_versions(engine, targets)
    missing = sorted(set(targets) - set(key_map))
    if missing:
        raise RuntimeError(f"gold keys without a document version: {', '.join(missing)}")
    return matrix, key_map
