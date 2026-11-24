"""Draft real-data chat eval questions from a frozen collection snapshot, for owner review.

Usage: ``python -m tests.integration.chat_eval.draft_real <snapshot-dir> [--n 150]``.

It reads the live collection DB (``EVAL_COLLECT_DSN``) that ``snapshot_meta`` described, samples
current documents and observations stratified by source and day, asks the chat model for one batch of
questions per category, validates every draft against the sampled rows, and writes ``questions.csv``
(utf-8-sig, git-ignored with its snapshot). Nothing is graded here; the owner sets ``decision``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.chat_eval.hybrid import mask_secrets
from tests.integration.chat_eval.judge import load_env_file
from tests.integration.chat_eval.snapshot_meta import DEFAULT_DSN, TZ_NAME, source_key

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env.e2e-real"
TIMEOUT_S = 120.0
BATCH_SAMPLE = 40  # rows shown to the model per category call
PREVIEW_CHARS = 300
REL_TOL = 0.005  # a drafted figure must sit within 0.5% of a sampled value
CSV_COLUMNS = (
    "decision",
    "id",
    "cat",
    "lang",
    "q",
    "gold_keys",
    "gold_obs",
    "gold_facts",
    "nums",
    "texts",
    "empty",
    "source_scope",
    "min_recall",
    "evidence_preview",
)

# Base share per category (sums to 140); ``--n`` scales them. Hints go into the prompt verbatim.
CATEGORIES: dict[str, tuple[int, str]] = {
    "latest_value": (20, "the most recent value of one series; name the series and the time"),
    "range_aggregate": (20, "a min, max, average or count over a time window inside the rows"),
    "paraphrase": (20, "the same fact asked in different words than the row title"),
    "cross_lingual": (
        20,
        "alternate lang: Vietnamese questions over English rows and English over Vietnamese rows",
    ),
    "attribute_filter": (
        15,
        "filter on one attribute: quake magnitude, air-quality value or a fire detection",
    ),
    "multi_source": (15, "needs rows from two different sources; list both in source_scope"),
    "empty": (
        15,
        "no-data answers: a date before the first collection, or a symbol or provider "
        "absent from the rows; set empty=true and give no gold",
    ),
    "relative_time": (15, "relative time words (today, yesterday, last 3 days)"),
}


@dataclass(frozen=True)
class Draft:
    """One model-drafted question before validation. Gold fields name sampled row keys only."""

    cat: str
    lang: str
    q: str
    gold_keys: tuple[str, ...]
    gold_obs: tuple[str, ...]
    gold_facts: tuple[str, ...]
    nums: tuple[float, ...]
    texts: tuple[str, ...]
    empty: bool
    source_scope: tuple[str, ...]


@dataclass(frozen=True)
class Sample:
    """One collected row shown to the model: its gold key, stratum, evidence text and numbers."""

    key: str
    source_key: str
    day: str
    kind: str  # "doc" | "obs"
    text: str
    numbers: tuple[float, ...]


def extract_numbers(text_value: str) -> tuple[float, ...]:
    """Every unsigned decimal in ``text_value`` as a float, in order (pure).

    Used to build the set of figures a drafted question may cite. Signs are dropped on purpose:
    the validator compares magnitudes, and sampled values are also stored with their sign.
    """
    return tuple(float(m) for m in re.findall(r"\d+(?:\.\d+)?", text_value))


def number_matches(value: float, numbers: Sequence[float], rel_tol: float = REL_TOL) -> bool:
    """True when ``value`` is within ``rel_tol`` (relative) of any sampled number (pure).

    A NaN value (an unparseable figure from the model) never matches.
    """
    return any(abs(value - n) <= rel_tol * abs(n) for n in numbers)


def find_duplicates(questions: Sequence[str]) -> set[int]:
    """Indices of questions that repeat an earlier one, ignoring case and whitespace (pure)."""
    seen: set[str] = set()
    dupes: set[int] = set()
    for i, q in enumerate(questions):
        norm = " ".join(q.split()).casefold()
        if norm in seen:
            dupes.add(i)
        seen.add(norm)
    return dupes


def reject_reasons(draft: Draft, keys: frozenset[str], numbers: Sequence[float]) -> list[str]:
    """Why ``draft`` must be dropped; an empty list means it is kept (pure).

    ``keys`` are the gold keys of the rows shown in the same batch and ``numbers`` every figure in
    those rows. A question that fails any rule is dropped with its reason, never repaired.
    """
    reasons: list[str] = []
    if not draft.q.strip():
        reasons.append("empty question")
    if draft.lang not in ("en", "vi"):
        reasons.append(f"lang {draft.lang!r} is not en or vi")
    for key in draft.gold_keys + draft.gold_obs:
        if key not in keys:
            reasons.append(f"gold key not in sample: {key}")
    if draft.empty and (draft.gold_keys or draft.gold_obs):
        reasons.append("empty draft must not carry gold keys")
    for value in draft.nums:
        if not number_matches(value, numbers):
            reasons.append(f"number {value:g} not in sampled rows")
    if any("|" in item for item in draft.gold_facts + draft.texts):
        reasons.append("'|' is the CSV separator and may not appear in facts or texts")
    return reasons


def _float(value: Any) -> float:
    """A model-supplied figure as float; anything unparseable becomes NaN (pure)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _strings(value: Any) -> tuple[str, ...]:
    """A JSON list as a tuple of stripped strings; a non-list is treated as empty (pure)."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _draft_from_item(item: Mapping[str, Any], cat: str) -> Draft:
    """Map one JSON object to a Draft, keeping the requested category (pure)."""
    nums = item.get("nums")
    return Draft(
        cat=cat,
        lang=str(item.get("lang", "")).strip().lower(),
        q=str(item.get("q", "")).strip(),
        gold_keys=_strings(item.get("gold_keys")),
        gold_obs=_strings(item.get("gold_obs")),
        gold_facts=_strings(item.get("gold_facts")),
        nums=tuple(_float(n) for n in nums) if isinstance(nums, list) else (),
        texts=_strings(item.get("texts")),
        empty=item.get("empty") is True,
        source_scope=_strings(item.get("source_scope")),
    )


def parse_drafts(raw: str, cat: str) -> list[Draft]:
    """Parse the model reply into drafts for ``cat`` (pure).

    Accepts a bare JSON array or one wrapped in a Markdown fence. Raises ValueError when no JSON
    array is present; non-object items are skipped rather than failing the whole batch.
    """
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        raise ValueError("reply contains no JSON array")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply is not valid JSON: {exc.msg}") from None
    if not isinstance(payload, list):
        raise ValueError("reply is not a JSON array")
    return [_draft_from_item(item, cat) for item in payload if isinstance(item, dict)]


CATEGORY_RULES = (
    "Reply with a JSON array only, no prose. Each item is an object with keys: "
    '"q" (question text), "lang" ("en" or "vi"), "gold_keys" (row keys whose text answers it), '
    '"gold_obs" (row keys of observation rows that answer it), "gold_facts" (short facts a correct '
    'answer states), "nums" (figures a correct answer states), "texts" (always []), "empty" '
    '(true only for no-data questions), "source_scope" (source keys the answer draws on). '
    "Use only keys and figures that appear in the rows. Never use the character | in any string."
)


def build_prompt(
    cat: str, anchor_local: str, samples: Sequence[Sample], count: int
) -> list[dict[str, str]]:
    """Chat messages asking for ``count`` questions of category ``cat`` over ``samples`` (pure).

    The anchor is stated as "today" so relative-time questions resolve against the snapshot clock,
    not the wall clock at drafting time.
    """
    hint = CATEGORIES[cat][1]
    rows = "\n".join(f"- {s.key} :: {s.text}" for s in samples)
    user = (
        f"Today is {anchor_local}. Category: {cat} ({hint}). Write {count} items, mixing "
        f"English and Vietnamese only where the category asks for it.\n\nRows:\n{rows}"
    )
    return [{"role": "system", "content": CATEGORY_RULES}, {"role": "user", "content": user}]


def seeded_rng(seed: str) -> random.Random:
    """A deterministic RNG for sample selection, so one snapshot always drafts the same batches.

    The generator is not for security use; it only makes batch selection reproducible.
    """
    return random.Random(seed)  # noqa: S311 - reproducible sample selection, not a security token


def stratified_sample(samples: Sequence[Sample], n: int, rng: random.Random) -> list[Sample]:
    """Up to ``n`` samples taken round-robin across (source, day) strata (pure given ``rng``).

    Each stratum is shuffled with ``rng`` and one row is taken from every stratum per round, so no
    single source or day dominates a batch. Output order is deterministic for a seeded ``rng``.
    """
    strata: dict[tuple[str, str], list[Sample]] = {}
    for sample in samples:
        strata.setdefault((sample.source_key, sample.day), []).append(sample)
    buckets = [rng.sample(rows, len(rows)) for _, rows in sorted(strata.items())]
    picked: list[Sample] = []
    while len(picked) < n and any(buckets):
        for bucket in buckets:
            if bucket and len(picked) < n:
                picked.append(bucket.pop())
    return picked


def _cell(value: Any) -> str:
    """One CSV cell: floats use ``repr`` (round-trippable), integral floats drop ``.0`` (pure)."""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def to_row(draft: Draft, row_id: str, evidence: str) -> dict[str, str]:
    """The CSV row for an accepted draft; ``decision`` and ``min_recall`` stay blank (pure).

    ``evidence`` is the text of the first gold row, truncated to 300 characters for review.
    """
    return {
        "decision": "",
        "id": row_id,
        "cat": draft.cat,
        "lang": draft.lang,
        "q": draft.q,
        "gold_keys": ";".join(draft.gold_keys),
        "gold_obs": ";".join(draft.gold_obs),
        "gold_facts": "|".join(draft.gold_facts),
        "nums": ";".join(_cell(n) for n in draft.nums),
        "texts": "|".join(draft.texts),
        "empty": "1" if draft.empty else "0",
        "source_scope": ";".join(draft.source_scope),
        "min_recall": "",
        "evidence_preview": evidence[:PREVIEW_CHARS],
    }


def write_questions(path: Path, rows: Sequence[Mapping[str, str]]) -> Path:
    """Write ``rows`` as a UTF-8 BOM CSV with the fixed ``CSV_COLUMNS`` header (side effect: file)."""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


def scaled_targets(n: int) -> dict[str, int]:
    """Per-category question counts for a total of about ``n`` (pure); each category gets at least one."""
    base_total = sum(count for count, _ in CATEGORIES.values())
    return {cat: max(1, round(count * n / base_total)) for cat, (count, _) in CATEGORIES.items()}


def anchor_local_text(anchor: datetime) -> str:
    """The snapshot clock in the production chat format, ``YYYY-MM-DD HH:MM (tz)`` (pure)."""
    local = anchor.astimezone(ZoneInfo(TZ_NAME))
    return f"{local:%Y-%m-%d %H:%M} ({TZ_NAME})"


def document_sample(
    provider: str | None, name: str, external_id: str, title: str, content: str, observed: datetime
) -> Sample:
    """A document row as a Sample; the gold key is ``<source_key>/<external_id>`` (pure)."""
    skey = source_key(provider, name)
    body = f"[{skey}] {title} :: {content}"
    return Sample(
        key=f"{skey}/{external_id}",
        source_key=skey,
        day=_day(observed),
        kind="doc",
        text=body,
        numbers=extract_numbers(body),
    )


def observation_sample(
    provider: str | None,
    name: str,
    external_id: str,
    detail: str,
    value: float | None,
    unit: str,
    observed: datetime,
) -> Sample:
    """An observation row as a Sample; ``value`` is kept with its sign (pure)."""
    skey = source_key(provider, name)
    body = f"[{skey}] {detail} = {value} {unit} @ {observed.isoformat()}"
    numbers = extract_numbers(body)
    if value is not None:
        numbers = (float(value), *numbers)
    return Sample(
        key=f"{skey}/{external_id}",
        source_key=skey,
        day=_day(observed),
        kind="obs",
        text=body,
        numbers=numbers,
    )


def _day(observed: datetime) -> str:
    """The Asia/Ho_Chi_Minh calendar day of ``observed`` (pure); naive times count as UTC."""
    aware = observed if observed.tzinfo else observed.replace(tzinfo=ZoneInfo("UTC"))
    return aware.astimezone(ZoneInfo(TZ_NAME)).date().isoformat()


DOC_SQL = """
SELECT s.name, s.provider, d.external_id, d.title, left(v.content, 600) AS content,
       v.observed_at
FROM documents d
JOIN document_versions v ON v.document_id = d.id AND v.version_number = d.current_version
JOIN sources s ON s.id = d.source_id
WHERE s.name LIKE 'evalc-%' AND d.external_id IS NOT NULL
"""

OBS_SQL = """
SELECT s.name, s.provider, o.external_id, o.metric, o.symbol, o.region, o.value, o.unit,
       o.observed_at
FROM observations o
JOIN sources s ON s.id = o.source_id
WHERE s.name LIKE 'evalc-%' AND o.is_current
"""


async def load_samples(engine: AsyncEngine) -> list[Sample]:
    """Every collected document and current observation of the collection sources (read-only I/O)."""
    out: list[Sample] = []
    async with engine.connect() as conn:
        for row in (await conn.execute(text(DOC_SQL))).all():
            out.append(
                document_sample(
                    row.provider, row.name, row.external_id, row.title, row.content, row.observed_at
                )
            )
        for row in (await conn.execute(text(OBS_SQL))).all():
            detail = f"{row.metric} {row.symbol or row.region or ''}".strip()
            out.append(
                observation_sample(
                    row.provider,
                    row.name,
                    row.external_id,
                    detail,
                    row.value,
                    row.unit,
                    row.observed_at,
                )
            )
    return out


async def complete_chat(messages: list[dict[str, str]], env: Mapping[str, str]) -> str:
    """POST one chat completion to the ``E2E_REAL_*`` endpoint and return the reply text (I/O).

    Raises RuntimeError when the endpoint settings are incomplete or the call fails. Error text is
    masked with the env secret values, so a key never reaches a log or a traceback.
    """
    base = env.get("E2E_REAL_BASE_URL", "").strip().rstrip("/")
    model = env.get("E2E_REAL_MODEL", "").strip()
    key = env.get("E2E_REAL_API_KEY", "").strip()
    if not (base and model and key):
        raise RuntimeError("E2E_REAL_BASE_URL, E2E_REAL_MODEL and E2E_REAL_API_KEY must all be set")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                json={"model": model, "messages": messages, "temperature": 0.7},
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            return str(resp.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(mask_secrets(f"{type(exc).__name__}: {exc}", env)) from None


Completer = Callable[[list[dict[str, str]]], Awaitable[str]]


def _accept(
    drafts: Sequence[Draft], batch: Sequence[Sample], notes: list[str]
) -> list[tuple[Draft, str]]:
    """Keep drafts that pass the batch validators; append one note per rejection (pure).

    Returns ``(draft, evidence_text)`` pairs, where evidence is the first gold row's text.
    """
    keys = frozenset(s.key for s in batch)
    numbers = tuple(n for s in batch for n in s.numbers)
    texts = {s.key: s.text for s in batch}
    kept: list[tuple[Draft, str]] = []
    for draft in drafts:
        reasons = reject_reasons(draft, keys, numbers)
        if reasons:
            notes.append(f"{draft.cat}: dropped {draft.q[:60]!r}: {'; '.join(reasons)}")
            continue
        gold = next((k for k in draft.gold_keys + draft.gold_obs if k in texts), "")
        kept.append((draft, texts.get(gold, "")))
    return kept


async def draft_rows(
    samples: Sequence[Sample],
    n: int,
    anchor: datetime,
    complete: Completer,
    notes: list[str],
) -> list[dict[str, str]]:
    """Run one batch per category and return accepted CSV rows, deduplicated across batches.

    The RNG is seeded from the anchor so the same snapshot always yields the same sample batches.
    A category whose reply cannot be parsed contributes nothing and a note records why.
    """
    rng = seeded_rng(anchor.isoformat())
    anchor_text = anchor_local_text(anchor)
    accepted: list[tuple[Draft, str]] = []
    for cat, count in scaled_targets(n).items():
        batch = stratified_sample(samples, BATCH_SAMPLE, rng)
        try:
            drafts = parse_drafts(await complete(build_prompt(cat, anchor_text, batch, count)), cat)
        except ValueError as exc:
            notes.append(f"{cat}: reply rejected: {exc}")
            continue
        accepted.extend(_accept(drafts, batch, notes))
    dupes = find_duplicates([d.q for d, _ in accepted])
    counters: dict[str, int] = {}
    rows: list[dict[str, str]] = []
    for i, (draft, evidence) in enumerate(accepted):
        if i in dupes:
            notes.append(f"duplicate dropped: {draft.q[:60]!r}")
            continue
        counters[draft.cat] = counters.get(draft.cat, 0) + 1
        row_id = f"r_{draft.cat}_{counters[draft.cat]:03d}_{draft.lang}"
        rows.append(to_row(draft, row_id, evidence))
    return rows


async def run(snapdir: Path, n: int, dsn: str, env: Mapping[str, str]) -> Path:
    """Draft questions for the snapshot in ``snapdir`` and write ``questions.csv`` beside it."""
    meta = json.loads((snapdir / "meta.json").read_text(encoding="utf-8"))
    anchor = datetime.fromisoformat(meta["anchor_utc"])
    engine = create_async_engine(dsn)
    try:
        samples = await load_samples(engine)
    finally:
        await engine.dispose()
    notes: list[str] = []
    rows = await draft_rows(samples, n, anchor, partial(complete_chat, env=env), notes)
    for note in notes:
        print(note, file=sys.stderr)
    print(f"{len(rows)} accepted of about {n}; {len(notes)} note(s)", file=sys.stderr)
    return write_questions(snapdir / "questions.csv", rows)


def main(argv: list[str]) -> int:
    """CLI entry: ``draft_real <snapshot-dir> [--n N]``; returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapdir", type=Path)
    parser.add_argument("--n", type=int, default=150)
    args = parser.parse_args(argv)
    dsn = os.environ.get("EVAL_COLLECT_DSN", DEFAULT_DSN)
    env = load_env_file(ENV_FILE)
    out = asyncio.run(run(args.snapdir, args.n, dsn, env))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
