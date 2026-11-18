"""Settle sweep: post each finished Topic watch run into the rule's Chat thread, once per run.

A ``collect_source`` action only queues a collection (see ``_execution.collect_source``); the
connectors worker runs it later. This sweep, driven by the automations worker cron, finds actions
whose request has settled, reads the Documents that run collected through the owners' public reads and
posts one assistant message per run. Posting is idempotent on ``run:<run id>`` in Chat, and the action
is re-marked ``settled:<request id>`` afterwards so it leaves the candidate set; a crash between the
two steps only repeats a no-op post. No lock is held across an owner read.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.errors import DomainError
from core.workspaces.schemas import Scope
from modules.automations._execution.runs import (
    _commit,
    _fence_current,
    _locked_run,
    _mark,
)
from modules.automations.models import AutomationRevision, AutomationRun, AutomationRunAction
from modules.automations.scope import _actor, _admit
from modules.chat import public as chat
from modules.connectors import public as connectors
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents

logger = logging.getLogger(__name__)

SWEEP_LIMIT = 50  # candidates per tick per workspace
SWEEP_WINDOW = timedelta(hours=24)  # older unsettled runs are left alone
_STAT_KEYS = (
    "queries",
    "results",
    "fetched",
    "blocked",
    "timeout",
    "failed",
    "seen",
    "duplicates",
    "off_topic",
    "items",
    "tokens",
)
_REFERENCE_PREFIX = "collection:"
# URLs and bare domains: the posted text line must stay inert for the markdown fallback and plain export.
_LINKISH = re.compile(
    r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}\b(?:/\S*)?"
)
_TERMINAL_RUN = ("succeeded", "failed", "needs_ocr")


@dataclass(frozen=True)
class Candidate:
    """One succeeded ``collect_source`` action whose collection request may have settled."""

    run_id: UUID
    ordinal: int
    automation_id: UUID
    rule_name: str
    since: datetime  # run creation time: Documents created from here on belong to this run
    request_id: UUID
    source_id: UUID
    max_items: int


@dataclass(frozen=True)
class Post:
    """The thread message of one run: text, allowlist-bound metadata and the idempotency key."""

    content: str
    metadata: dict[str, Any]
    dedupe_key: str


def _one_line(text: str | None, *, strip_links: bool = False) -> str:
    """Collapse all whitespace so one item stays one line; ``strip_links`` also drops URLs and bare domains."""
    if strip_links:
        text = _LINKISH.sub("", text or "")
    return " ".join((text or "").split())


def _clean_stats(stats: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the known counters (ints) and a string ``reason`` from the receipt telemetry."""
    source = stats or {}
    clean: dict[str, Any] = {
        k: v for k in _STAT_KEYS if isinstance(v := source.get(k), int) and v >= 0
    }
    reason = source.get("reason")
    if isinstance(reason, str) and reason:
        clean["reason"] = reason[:32]
    return clean


def build_post(cand: Candidate, docs: list[Any], stats: dict[str, Any] | None) -> Post:
    """Build the thread post of one run from its Documents and receipt stats; pure.

    Content is one ``- title: summary`` line per item (plain text; the web client never renders it as
    markup; titles are link-stripped so the markdown fallback and export stay inert, while the
    metadata item keeps the stored title). Metadata carries ``automation_id`` (for the run-log deep
    link), ``source_id`` (stored for withholding, never sent to clients), the item
    list and ``coverage="partial"`` exactly when the receipt gave a stop ``reason``. The dedupe key is
    ``run:<run id>``, so a re-sweep or retry appends nothing.
    """
    clean = _clean_stats(stats)
    lines = []
    for doc in docs:
        title = _one_line(doc.title, strip_links=True) or "-"
        summary = _one_line(doc.summary)
        lines.append(f"- {title}: {summary}" if summary else f"- {title}")
    items = [
        {
            "document_id": str(doc.id),
            "title": doc.title,
            "url": doc.canonical_url,
            "site": doc.site,
            "summary": doc.summary,
        }
        for doc in docs
    ]
    return Post(
        content="\n".join(lines),
        metadata={
            "task_kind": "topic_watch",
            "run_id": str(cand.run_id),
            "automation_id": str(cand.automation_id),
            "source_id": str(cand.source_id),
            "items": items,
            "stats": clean,
            "coverage": "partial" if clean.get("reason") else "complete",
        },
        dedupe_key=f"run:{cand.run_id}",
    )


def _read_session(factory: async_sessionmaker[AsyncSession]) -> Any:
    """Open the short read-only session used for owner reads (a seam for tests)."""
    return factory()


async def _candidates(
    factory: async_sessionmaker[AsyncSession], *, scope: Scope
) -> list[Candidate]:
    """List up to ``SWEEP_LIMIT`` unsettled Topic watch actions of the last 24 h, oldest first.

    Scoped to the workspace and owner; read-only, no locks. A row whose stored spec or reference is
    unreadable is skipped here (it cannot post) rather than failing the sweep.
    """
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        AutomationRunAction.run_id,
                        AutomationRunAction.ordinal,
                        AutomationRunAction.result_reference,
                        AutomationRun.automation_id,
                        AutomationRun.created_at,
                        AutomationRevision.name,
                        AutomationRevision.actions,
                    )
                    .join(AutomationRun, AutomationRun.id == AutomationRunAction.run_id)
                    .join(
                        AutomationRevision,
                        (AutomationRevision.automation_id == AutomationRun.automation_id)
                        & (AutomationRevision.revision == AutomationRun.revision),
                    )
                    .where(
                        AutomationRun.workspace_id == scope.workspace_id,
                        AutomationRun.owner_id == _actor(scope),
                        AutomationRun.created_at >= datetime.now(UTC) - SWEEP_WINDOW,
                        AutomationRunAction.action_type == "collect_source",
                        AutomationRunAction.status == "succeeded",
                        AutomationRunAction.result_reference.like(f"{_REFERENCE_PREFIX}%"),
                    )
                    .order_by(AutomationRun.created_at, AutomationRunAction.run_id)
                    .limit(SWEEP_LIMIT)
                )
            )
            .tuples()
            .all()
        )
        await session.rollback()
    found: list[Candidate] = []
    for run_id, ordinal, reference, automation_id, created_at, name, actions in rows:
        try:
            spec = actions[ordinal - 1]
            found.append(
                Candidate(
                    run_id=run_id,
                    ordinal=ordinal,
                    automation_id=automation_id,
                    rule_name=name,
                    since=created_at,
                    request_id=UUID(reference.removeprefix(_REFERENCE_PREFIX)),
                    source_id=UUID(str(spec["source_id"])),
                    max_items=int(spec["watch"]["max_items"]),
                )
            )
        except (IndexError, KeyError, TypeError, ValueError):
            logger.warning("Topic watch settle skipped an unreadable action row")
    return found


async def _receipt(
    session: AsyncSession,
    cand: Candidate,
    ingestion_run_id: UUID | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[dict[str, Any] | None, datetime | None]:
    """Return the request's ingestion-run telemetry and, once that run is terminal, its end time.

    The end time (the run's last update) bounds the Document read so a later run's items never leak
    into this post; it is None while the run is still going or unknown (no upper bound then).
    """
    if ingestion_run_id is None:
        return None, None
    history = await ingestion.list_source_runs(
        session,
        cand.source_id,
        limit=20,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    for run in history.items if history is not None else []:
        if run.run_id == ingestion_run_id:
            until = run.updated_at if run.status in _TERMINAL_RUN else None
            return run.collection_stats, until
    return None, None


async def _close(
    factory: async_sessionmaker[AsyncSession],
    cand: Candidate,
    code: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Mark the action settled with ``code`` (``succeeded`` + reference ``settled:<request id>``).

    Takes the run then the action lock through ``_mark``; the action leaves the sweep's candidate set.
    A ``None`` code records a clean post.
    """
    await _mark(
        factory,
        cand.run_id,
        cand.ordinal,
        "succeeded",
        code,
        f"settled:{cand.request_id}",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def _publish(
    factory: async_sessionmaker[AsyncSession],
    cand: Candidate,
    post: Post,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Post ``post`` into the rule's thread in one short transaction, then settle the action.

    Lock order matches the brief delivery: access fence, run, rule head (``FOR SHARE``), then the
    conversation row inside ``append_task_message``. If the run needs review or the rule is gone,
    disabled or edited, nothing is posted and the action is closed with the reason (returns False).
    Idempotent: ``append_task_message`` returns the existing message for the same dedupe key.
    """
    reason: str | None = None
    async with factory() as session:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        run = await _locked_run(session, cand.run_id, scope)
        if run is None or run.document_evidence_revoked or run.status == "requires_review":
            reason = "document_evidence_unavailable"
        elif not await _fence_current(session, run, scope=scope, share=True):
            reason = "stale_revision"
        if reason is not None:
            await session.rollback()
        else:
            conversation_id = await chat.ensure_automation_conversation(
                session, cand.automation_id, cand.rule_name, task_kind="topic_watch", scope=scope
            )
            await chat.append_task_message(
                session,
                conversation_id,
                content=post.content,
                citations=[],
                metadata=post.metadata,
                source_ids=[cand.source_id],
                dedupe_key=post.dedupe_key,
                scope=scope,
            )
            await _commit(
                session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
    await _close(
        factory, cand, reason, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return reason is None


async def settle_candidate(
    factory: async_sessionmaker[AsyncSession],
    cand: Candidate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Settle one candidate; return ``pending``, ``posted``, ``no_new_data`` or ``closed``.

    Reads the request through the connectors owner (a missing request closes the action as
    ``collection_unavailable``); ``queued``/``running`` stays ``pending`` for a later tick;
    ``failed``/``cancelled`` close as ``collection_<status>`` without a post. A settled request
    reads at most ``max_items`` Documents of the Source created since the run began and, when the
    ingestion run is terminal, no later than its end (newest first):
    none closes as ``no_new_data``, otherwise one post is published.
    """
    kw: dict[str, Any] = {"scope": scope, "multi_workspace_enabled": multi_workspace_enabled}
    async with _read_session(factory) as session:
        try:
            request = await connectors.get_collection_request(
                session,
                scope,
                cand.source_id,
                cand.request_id,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except DomainError:
            request = None
        docs: list[Any] = []
        stats: dict[str, Any] | None = None
        if request is not None and request.status in ("succeeded", "no_changes"):
            stats, until = await _receipt(session, cand, request.ingestion_run_id, **kw)
            docs = await documents.list_recent_source_documents(
                session, cand.source_id, cand.since, cand.max_items, until=until, **kw
            )
        await session.rollback()
    if request is None:
        await _close(factory, cand, "collection_unavailable", **kw)
        return "closed"
    if request.status in ("queued", "running"):
        return "pending"
    if request.status in ("failed", "cancelled"):
        await _close(factory, cand, f"collection_{request.status}", **kw)
        return "closed"
    if not docs:
        await _close(factory, cand, "no_new_data", **kw)
        return "no_new_data"
    posted = await _publish(factory, cand, build_post(cand, docs, stats), **kw)
    return "posted" if posted else "closed"


async def sweep(
    factory: async_sessionmaker[AsyncSession], *, scope: Scope, multi_workspace_enabled: bool
) -> int:
    """Settle every ready Topic watch run of the workspace (bounded) and return the posts made.

    One candidate failing (owner refusal, transient error) is logged by type and skipped so it cannot
    starve the rest; it is retried on the next tick while it stays inside the 24 h window.
    """
    posted = 0
    for cand in await _candidates(factory, scope=scope):
        try:
            outcome = await settle_candidate(
                factory, cand, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        except Exception as exc:  # noqa: BLE001  # one bad run must not stop the sweep; retried next tick
            logger.warning("Topic watch settle failed; error_type=%s", type(exc).__name__)
            continue
        posted += outcome == "posted"
    return posted
