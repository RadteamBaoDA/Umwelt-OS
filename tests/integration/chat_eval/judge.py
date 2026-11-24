"""JEV-style scoring judge: a report-only second grader for chat eval answers.

The request and response contract follows the ragflow ``ScoredSwitch`` component
(``internal/agent/component/scored_switch_request.go`` for the call and URL rules,
``scored_switch_route.go`` for the probability parsing, the 0.02 sum tolerance and the 0.15
margin). The judge never changes a case's pass/fail result; it only adds a verdict that the
report uses to list disagreements with the regex grader and cases to review by hand.

Config comes from ``E2E_JUDGE_BASE_URL``, ``E2E_JUDGE_MODEL`` and ``E2E_JUDGE_API_KEY``. The key is
sent only as a bearer header and is masked in any error text this module returns.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.grading import passed
from tests.integration.chat_eval.hybrid import mask_secrets

LABELS: tuple[str, ...] = (
    "correct_complete",
    "correct_partial",
    "wrong",
    "fabricated",
    "correct_no_data",
)
QUESTION_ID = "verdict"
SUM_TOLERANCE = 0.02  # ragflow scoreSumTolerance: probabilities must sum to 1 within this
MARGIN = 0.15  # ragflow defaultMarginThreshold: top1 - top2 below this is uncertain
TIMEOUT_S = 60.0
NO_DATA_REFERENCE = "no data exists"
NO_EVIDENCE = "(no evidence retrieved)"
# Bounds on the evidence the judge sees: the rows or excerpts the turn actually had. Items beyond the count
# are dropped, each item is cut to the item size, and the whole block stays within the total size.
EVIDENCE_MAX_ITEMS = 12
EVIDENCE_ITEM_CHARS = 400
EVIDENCE_TOTAL_CHARS = 6000
EVIDENCE_ROW_KEYS = ("observations", "documents", "rows", "observation_aggregates")
CRITERIA: dict[str, str] = {
    "correct_complete": "True to the reference and states every fact in it.",
    "correct_partial": "True to the reference but misses one or more facts in it.",
    "wrong": "Contradicts the reference or gives a wrong fact or figure.",
    "fabricated": (
        "States facts or figures that are neither in the reference nor supported by the evidence."
    ),
    "correct_no_data": "Correctly says there is no data for the question.",
}
INSTRUCTIONS = (
    "You grade an answer to a question against a reference. Pick the one label that fits the answer. "
    "Give a probability to every label; the probabilities must sum to 1. Grade the answer against the "
    "reference. The evidence is what the answer was drawn from: 'fabricated' means a claim that is not "
    "supported by the EVIDENCE. Figures derived arithmetically from the evidence (sums, averages, "
    "differences) are supported."
)


@dataclass(frozen=True)
class Verdict:
    """One judge result. ``status`` is ``ok``, ``uncertain`` (thin margin) or ``invalid`` (no usable scores)."""

    label: str
    probs: dict[str, float]
    status: Literal["ok", "uncertain", "invalid"]
    error: str = ""


@dataclass(frozen=True)
class JudgeConfig:
    """Judge endpoint settings read from the env mapping; the key is never rendered."""

    base_url: str
    model: str
    api_key: str = field(repr=False)  # secret: kept out of reprs and pytest tracebacks


def judge_config(env: Mapping[str, str]) -> JudgeConfig | None:
    """The judge settings when all three ``E2E_JUDGE_*`` values are set, else None (pure)."""
    base = env.get("E2E_JUDGE_BASE_URL", "").strip()
    model = env.get("E2E_JUDGE_MODEL", "").strip()
    key = env.get("E2E_JUDGE_API_KEY", "").strip()
    if not (base and model and key):
        return None
    return JudgeConfig(base, model, key)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` env file into a dict (empty when absent). Values are never logged."""
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def decisions_url(base: str) -> str:
    """The scoring endpoint for an OpenAI-style base URL (ragflow ``decisionsPath``, pure).

    ``.../api/v1`` maps to ``.../api/alpha/decisions``, ``.../v1`` to ``.../alpha/decisions``, and any
    other path gets ``/decisions`` appended.
    """
    path = base.strip().rstrip("/")
    if path.lower().endswith("/api/v1"):
        return path[: -len("/api/v1")] + "/api/alpha/decisions"
    if path.lower().endswith("/v1"):
        return path[: -len("/v1")] + "/alpha/decisions"
    return path + "/decisions"


def evidence_from_result(result: str) -> list[str]:
    """The evidence texts of one query-tool result (pure).

    Each row under ``EVIDENCE_ROW_KEYS`` becomes one JSON item, so the per-item bound keeps whole rows instead
    of the head of one large blob. A result that is not a JSON object, or has no row list, stays a single item
    so the judge still sees what the model saw.
    """
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return [result]
    if not isinstance(data, dict):
        return [result]
    rows = [
        row for key in EVIDENCE_ROW_KEYS if isinstance(data.get(key), list) for row in data[key]
    ]
    if not rows:
        return [result]
    return [json.dumps(row, ensure_ascii=False, default=str) for row in rows]


def evidence_block(evidence: Sequence[str]) -> str:
    """The judge's evidence section: bounded excerpts of what the turn retrieved (pure).

    At most ``EVIDENCE_MAX_ITEMS`` non-blank items, in order, each whitespace-collapsed to one line and cut to
    ``EVIDENCE_ITEM_CHARS`` (a cut item ends with ``...``). Stops before any line would push the block past
    ``EVIDENCE_TOTAL_CHARS``. No usable item gives ``NO_EVIDENCE``, so the judge is told the turn had none.
    """
    lines: list[str] = []
    used = 0
    for raw in evidence:
        if len(lines) >= EVIDENCE_MAX_ITEMS:
            break
        text = " ".join(raw.split())
        if not text:
            continue
        if len(text) > EVIDENCE_ITEM_CHARS:
            text = text[: EVIDENCE_ITEM_CHARS - 3] + "..."
        line = f"- {text}"
        separator = 1 if lines else 0
        if used + separator + len(line) > EVIDENCE_TOTAL_CHARS:
            break
        used += separator + len(line)
        lines.append(line)
    return "\n".join(lines) or NO_EVIDENCE


def build_body(
    model: str,
    question: str,
    gold_facts: tuple[str, ...],
    empty: bool,
    answer: str,
    evidence: Sequence[str] = (),
) -> dict[str, Any]:
    """The decisions request body: one choice question with a criterion per label (pure).

    The state carries the question, the hand-written reference, the bounded evidence block (see
    ``evidence_block``) and the final answer. Raw tool output is sent only as those bounded excerpts.
    For ``empty`` cases the reference is ``no data exists``.
    """
    if empty:
        reference = NO_DATA_REFERENCE
    else:
        reference = "\n".join(f"- {fact}" for fact in gold_facts) or "(no reference facts)"
    state = (
        f"Question:\n{question}\n\nReference:\n{reference}\n\n"
        f"Evidence:\n{evidence_block(evidence)}\n\nAnswer:\n{answer}"
    )
    return {
        "model": model,
        "state": state,
        "questions": {
            QUESTION_ID: {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": dict(CRITERIA),
            }
        },
    }


def _invalid(error: str) -> Verdict:
    """An ``invalid`` verdict carrying a short reason (never a score)."""
    return Verdict("", {}, "invalid", error=error[:300])


def _number(value: Any) -> float | None:
    """A finite float from a JSON number or numeric string (ragflow ``floatFromJSON``); None otherwise."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _choice_object(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The choice answer for ``QUESTION_ID``, else the only choice answer under ``answers`` (ragflow order)."""
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        return None
    named: Any = answers.get(QUESTION_ID)
    if isinstance(named, Mapping):
        return named
    found = [a for a in answers.values() if isinstance(a, Mapping) and a.get("type") == "choice"]
    return found[0] if len(found) == 1 else None


def parse_verdict(payload: dict[str, Any]) -> Verdict:
    """Turn a decisions response into a ``Verdict`` (pure).

    ``invalid`` when there is no choice answer, a probability is missing, non-numeric or negative, the
    label set is not exactly ``LABELS``, or the sum is outside 1 +/- ``SUM_TOLERANCE``. ``uncertain`` when
    top-1 minus top-2 is below ``MARGIN`` (this includes an exact tie).
    """
    obj = _choice_object(payload)
    if obj is None or not isinstance(obj.get("probabilities"), Mapping):
        return _invalid("choice answer not found")
    probs: dict[str, float] = {}
    for key, value in obj["probabilities"].items():
        score = _number(value)
        if score is None or score < 0:
            return _invalid("probability is not a non-negative number")
        probs[str(key).strip()] = score
    if set(probs) != set(LABELS):
        return _invalid("probabilities do not cover exactly the five labels")
    if abs(sum(probs.values()) - 1.0) > SUM_TOLERANCE:
        return _invalid("probabilities do not sum to 1")
    ranked = sorted(LABELS, key=lambda label: probs[label], reverse=True)
    margin = probs[ranked[0]] - probs[ranked[1]]
    status: Literal["ok", "uncertain"] = "uncertain" if margin < MARGIN else "ok"
    return Verdict(ranked[0], probs, status)


def _client() -> httpx.AsyncClient:
    """The HTTP client for the judge call (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT_S)


async def judge(
    case: Case, answer: str, env: Mapping[str, str], evidence: Sequence[str] = ()
) -> Verdict | None:
    """Score one final answer against the case reference, with the turn's evidence as context.

    ``evidence`` is the text the turn retrieved (tool rows and retrieved chunks); ``evidence_block`` bounds it
    and the env keys are never included. Returns None when the judge env is incomplete. A transport error, a
    non-2xx status or a non-JSON body returns an ``invalid`` verdict whose error text is masked with the env
    key values, so a judge failure never fails the case or the run.
    """
    cfg = judge_config(env)
    if cfg is None:
        return None
    body = build_body(cfg.model, case.q, case.gold_facts, case.empty, answer, evidence)
    try:
        async with _client() as client:
            resp = await client.post(
                decisions_url(cfg.base_url),
                json=body,
                headers={"Authorization": f"Bearer {cfg.api_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return _invalid(mask_secrets(f"{type(exc).__name__}: {exc}", env))
    if not isinstance(payload, dict):
        return _invalid("response is not a JSON object")
    return parse_verdict(payload)


def to_record(verdict: Verdict) -> dict[str, Any]:
    """The report record of a verdict: ``label``, ``probs``, ``status``, plus ``error`` when invalid."""
    record: dict[str, Any] = {
        "label": verdict.label,
        "probs": verdict.probs,
        "status": verdict.status,
    }
    if verdict.status == "invalid":
        record["error"] = verdict.error
    return record


def disagreement(criteria: Mapping[str, Any], verdict: Verdict) -> bool:
    """True when the judge and the regex grader disagree (pure).

    Never for a non-``ok`` verdict. Regex pass with a judge ``wrong``/``fabricated``/``correct_partial``,
    or regex fail with a judge ``correct_complete``.
    """
    if verdict.status != "ok":
        return False
    if passed(criteria):
        return verdict.label in {"wrong", "fabricated", "correct_partial"}
    return verdict.label == "correct_complete"


def summarize_judge(entries: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Case ids with a judge disagreement, an uncertain verdict or an invalid verdict (pure).

    Only entries that carry a ``judge`` record are considered.
    """
    disagree: list[str] = []
    uncertain: list[str] = []
    invalid: list[str] = []
    for entry in entries:
        record = entry.get("judge")
        if not record:
            continue
        verdict = Verdict(record["label"], record["probs"], record["status"])
        if verdict.status == "invalid":
            invalid.append(entry["id"])
        elif verdict.status == "uncertain":
            uncertain.append(entry["id"])
        if disagreement(entry.get("criteria", {}), verdict):
            disagree.append(entry["id"])
    return {
        "judge_disagreements": disagree,
        "judge_uncertain": uncertain,
        "judge_invalid": invalid,
    }
