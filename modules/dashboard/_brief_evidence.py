"""Brief evidence validation helpers: prompt-fact digests and captured-support manifest checks.

Split out of ``briefs`` to keep that module under the file-size ceiling; ``briefs`` re-imports every name so
existing call sites and ``briefs.<name>`` test patches keep resolving.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope
from modules.dashboard.models import DailyBrief, DailyBriefEvidence
from modules.knowledge.documents import public as documents
from modules.sources import public as sources

MAX_FACTS = 40
MAX_BRIEF_EVIDENCE = 100
MAX_PROMPT_TITLE = 200


def _prompt_title(title: str) -> str:
    """Normalize a canonical title to the bounded text sent to the model and hashed at capture."""
    return str(title)[:MAX_PROMPT_TITLE]


def _prompt_fact_digest(fact: dict[str, Any]) -> str:
    """Hash the text-safe identity and exact input fields for one prompted fact."""
    value = {key: fact[key] for key in ("kind", "id", "title", "detail", "source_ids")}
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_fact_group(
    captured: list[DailyBriefEvidence],
    seen: set[tuple[str, str, str]],
    captured_sources: set[UUID],
    captured_documents: set[UUID],
) -> bool:
    """Check one fact's captured support rows and accumulate its document/source identities.

    Pure (no I/O). Rows must share kind/id/hash, have contiguous support indexes, and either carry a
    full (document, version, chunk, source) tuple without repeats or be one independent marker row
    (never for stories). A story needs at least one real support row. Mutates the three
    accumulators; returns False on the first violation.
    """
    exemplar = captured[0]
    if (
        exemplar.fact_kind not in {"tasks", "goals", "stories", "events"}
        or not re.fullmatch(r"[0-9a-f]{64}", exemplar.fact_hash)
        or any(
            item.fact_kind != exemplar.fact_kind
            or item.fact_id != exemplar.fact_id
            or item.fact_hash != exemplar.fact_hash
            for item in captured
        )
        or [item.support_index for item in captured] != list(range(len(captured)))
    ):
        return False
    try:
        if str(UUID(exemplar.fact_id)) != exemplar.fact_id:
            return False
    except (TypeError, ValueError):
        return False
    observed: set[tuple[str, str, str]] = set()
    for item in captured:
        triple = (item.document_id, item.document_version_id, item.chunk_id)
        if all(value is None for value in triple):
            if (
                exemplar.fact_kind in {"stories"}
                or len(captured) != 1
                or item.support_index != 0
                or item.source_id is not None
            ):
                return False
            continue
        if any(value is None for value in triple) or item.source_id is None:
            return False
        normalized = (str(triple[0]), str(triple[1]), str(triple[2]))
        if normalized in observed:
            return False
        observed.add(normalized)
        seen.add(normalized)
        captured_sources.add(item.source_id)
        # all-None triples were handled above
        if item.document_id is None:
            raise RuntimeError("invariant violated: item.document_id must not be None")
        captured_documents.add(item.document_id)
    return not (exemplar.fact_kind == "stories" and not observed)


async def _load_evidence_groups(
    session: AsyncSession,
    row: DailyBrief,
    fact_count: int,
) -> tuple[dict[int, list[DailyBriefEvidence]], set[UUID], set[UUID]] | None:
    """Read and structurally validate the brief's captured evidence manifest.

    One bounded SELECT (no locks). Returns ``(grouped_by_fact_ref, source_ids, document_ids)`` or
    ``None`` when the manifest is oversized, has gaps/out-of-range refs, or any fact group is
    malformed (see ``_validate_fact_group``) or the distinct support exceeds ``MAX_BRIEF_EVIDENCE``.
    """
    rows = list(
        (
            await session.scalars(
                select(DailyBriefEvidence)
                .where(
                    DailyBriefEvidence.brief_id == row.id,
                )
                .order_by(DailyBriefEvidence.fact_ref, DailyBriefEvidence.support_index)
                .limit(4_041)
            )
        ).all()
    )
    if len(rows) > MAX_FACTS * (MAX_BRIEF_EVIDENCE + 1):
        return None
    grouped: dict[int, list[DailyBriefEvidence]] = {}
    for item in rows:
        if item.fact_ref < 1 or item.fact_ref > fact_count:
            return None
        grouped.setdefault(item.fact_ref, []).append(item)
    if set(grouped) != set(range(1, fact_count + 1)):
        return None
    seen: set[tuple[str, str, str]] = set()
    captured_sources: set[UUID] = set()
    captured_documents: set[UUID] = set()
    for reference in range(1, fact_count + 1):
        if not _validate_fact_group(grouped[reference], seen, captured_sources, captured_documents):
            return None
    if len(seen) > MAX_BRIEF_EVIDENCE:
        return None
    return grouped, captured_sources, captured_documents


def _validate_citations(
    row: DailyBrief,
    grouped: dict[int, list[DailyBriefEvidence]],
) -> dict[int, dict[str, Any]] | None:
    """Validate the stored citation list against the captured manifest; map ref -> citation.

    Pure. Each citation must have exactly the stored keys, a unique known ref whose kind/id match
    the captured fact, string title, unique canonical UUID source ids and exactly the captured
    source set. Returns ``None`` on any mismatch.
    """
    citations = row.citations
    if not isinstance(citations, list) or not citations or len(citations) > MAX_FACTS:
        return None
    seen_refs: set[int] = set()
    citation_by_ref: dict[int, dict[str, Any]] = {}
    for citation in citations:
        if (
            not isinstance(citation, dict)
            or set(citation) != {"ref", "kind", "id", "title", "source_ids"}
            or type(citation["ref"]) is not int
            or citation["ref"] not in grouped
            or citation["kind"] != grouped[citation["ref"]][0].fact_kind
            or citation["id"] != grouped[citation["ref"]][0].fact_id
            or not isinstance(citation["title"], str)
            or not isinstance(citation["source_ids"], list)
            or any(not isinstance(value, str) for value in citation["source_ids"])
            or len(citation["source_ids"]) != len(set(citation["source_ids"]))
        ):
            return None
        try:
            if any(str(UUID(value)) != value for value in citation["source_ids"]):
                return None
        except (TypeError, ValueError):
            return None
        reference = citation["ref"]
        if reference in seen_refs:
            return None
        seen_refs.add(reference)
        citation_by_ref[reference] = citation
        captured_source_ids = sorted(
            {str(item.source_id) for item in grouped[reference] if item.source_id is not None}
        )
        if sorted(set(citation["source_ids"])) != captured_source_ids:
            return None
    return citation_by_ref


async def _captured_support_eligible(
    session: AsyncSession,
    captured_sources: set[UUID],
    captured_documents: set[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence | None,
    lock: bool,
) -> bool:
    """Check every captured Source is active/shareable and every captured Document still exists.

    Order: Sources (sorted by id) first, then Documents. ``lock=True`` locks them (requires the
    original ``access_fence``, else ``ValueError``); ``lock=False`` only reads Source fences and plain
    Document ids so export transactions never accumulate locks. Page-level prelocking already holds
    these in canonical order; re-locking a held row cannot block.
    """
    for source_id in sorted(captured_sources, key=str):
        if lock:
            if access_fence is None:
                raise ValueError(
                    "Locked brief support validation requires its original access fence"
                )
            source = await sources.lock_source(
                session,
                source_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
        else:
            source = await sources.get_source_fence(
                session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        if source is None or source.status != "active" or source.local_only:
            return False
    if lock:
        locked = await documents.lock_document_ids(
            session,
            sorted(captured_documents, key=str),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return set(locked) == captured_documents
    for document_id in captured_documents:
        if not await documents.existing_document_ids(
            session, [document_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled
        ):
            return False
    return True


async def _fact_support_current(
    session: AsyncSession,
    fact_rows: list[DailyBriefEvidence],
    cited: dict[str, Any] | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Re-resolve one document-supported fact against its owner module and compare to the capture.

    Stories go through ``news.brief_story_support`` and events through ``timeline.brief_event_support``
    (both imported lazily to avoid an import cycle); any other kind, incomplete support, an
    independent event, a changed support set, a changed prompt-fact digest, or a citation whose title
    or sources drifted yields False.
    """
    from modules.news import public as news
    from modules.timeline import public as timeline

    exemplar = fact_rows[0]
    try:
        fact_uuid = UUID(exemplar.fact_id)
    except (TypeError, ValueError):
        return False
    expected_sources = sorted(
        {str(item.source_id) for item in fact_rows if item.source_id is not None}
    )
    if exemplar.fact_kind == "stories":
        support = await news.brief_story_support(
            session,
            fact_uuid,
            expected_title=None,
            expected_source_ids=expected_sources,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if not support.complete:
            return False
        current = {
            "kind": "stories",
            "id": str(support.story_id),
            "title": _prompt_title(support.title),
            "detail": "",
            "source_ids": [str(value) for value in support.source_ids],
        }
        observed = {
            (str(item.document_id), str(item.document_version_id), str(item.chunk_id))
            for item in support.evidence
        }
    elif exemplar.fact_kind == "events":
        event_support = await timeline.brief_event_support(
            session,
            fact_uuid,
            expected_title=None,
            expected_source_ids=expected_sources,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if not event_support.complete or event_support.independent:
            return False
        current = {
            "kind": "events",
            "id": str(event_support.event_id),
            "title": _prompt_title(event_support.title),
            "detail": event_support.event_type,
            "source_ids": [str(value) for value in event_support.source_ids],
        }
        observed = {
            (str(item.document_id), str(item.document_version_id), str(item.chunk_id))
            for item in event_support.evidence
        }
    else:
        return False
    expected = {
        (str(item.document_id), str(item.document_version_id), str(item.chunk_id))
        for item in fact_rows
    }
    if observed != expected or _prompt_fact_digest(current) != exemplar.fact_hash:
        return False
    return cited is None or not (
        cited["title"] != current["title"]
        or sorted(set(cited["source_ids"])) != sorted(current["source_ids"])
    )


async def _captured_inputs_match(
    session: AsyncSession,
    row: DailyBrief,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence | None = None,
    lock: bool = True,
) -> bool:
    """Validate the full prompted-fact manifest and current exact support eligibility.

    Independent owner-authored facts (tasks, goals, manual events) stay historical. Document-supported facts
    must still resolve to the complete captured support set. Legacy citation-only
    rows remain unavailable because they lack a full input manifest.

    ``lock=False`` is the export path: it takes no row locks (Source fences and plain Document reads
    only), so a multi-page export transaction can never accumulate locks across pages. Consistency
    then rests on the final ``_brief_export_validation`` pass rejecting any changed record.

    Ordering: manifest -> citations -> Sources -> Documents -> timeline event locks -> per-fact
    re-resolution; the first failing stage returns False.
    """
    if (
        row.evidence_capture_version != 1
        or row.evidence_capture_status != "captured"
        or not row.evidence_fact_count
        or not 1 <= row.evidence_fact_count <= MAX_FACTS
        or row.status != "current"
    ):
        return False
    fact_count = row.evidence_fact_count
    loaded = await _load_evidence_groups(session, row, fact_count)
    if loaded is None:
        return False
    grouped, captured_sources, captured_documents = loaded
    citation_by_ref = _validate_citations(row, grouped)
    if citation_by_ref is None:
        return False
    if not await _captured_support_eligible(
        session,
        captured_sources,
        captured_documents,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        lock=lock,
    ):
        return False
    from modules.timeline import public as timeline

    try:
        event_ids = [
            UUID(grouped[reference][0].fact_id)
            for reference in range(1, row.evidence_fact_count + 1)
            if grouped[reference][0].fact_kind == "events"
            and grouped[reference][0].document_id is not None
        ]
    except (TypeError, ValueError):
        return False
    if lock:
        await timeline.lock_brief_events(
            session, event_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    for reference in range(1, row.evidence_fact_count + 1):
        fact_rows = grouped[reference]
        if fact_rows[0].document_id is None:
            # The independent marker was proven (manual origin, zero evidence) under the capture-time
            # Event fence. Later edits or deletion must not retroactively turn the historical
            # prompt fact into a dependency, so it is trusted without a current-state recheck.
            continue
        if not await _fact_support_current(
            session,
            fact_rows,
            citation_by_ref.get(reference),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ):
            return False
    return True
