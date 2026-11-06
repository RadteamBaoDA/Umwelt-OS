"""Automation document-evidence checks and cleanup (trigger/run scrub and revoke) for the retained-evidence lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope
from modules.automations._execution.types import (
    AutomationCleanupProgress,
)
from modules.automations.conditions import validate_sample
from modules.automations.models import (
    AutomationRun,
    AutomationTrigger,
)
from modules.automations.scope import _actor, _admit


async def _retained_document_evidence_current(
    session: AsyncSession,
    payload: Mapping[str, Any],
    *,
    event_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Hold the Source lifecycle lock while proving exact retained Document/version ownership.

    Source is locked before re-reading Documents, matching canonical deletion and mutation order.
    Paused and connector-only archived sources remain eligible; unfinished or failed with-data
    purges fail closed. The original ready-event receipt proves historical identity; its generation
    may predate a pause, so current Source generation is captured separately and compared only to
    the current retained-version fence. The caller holds its transaction through publication.
    """
    try:
        source_id = UUID(str(payload.get("source_id")))
    except (TypeError, ValueError):
        return False
    from modules.ingestion import public as ingestion
    from modules.knowledge.documents import public as documents
    from modules.sources import public as sources

    proof = await ingestion.resolve_ready_event_provenance(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        proof is None
        or proof.document_id != document_id
        or proof.document_version_id != document_version_id
        or proof.source_id != source_id
    ):
        return False

    fence = await sources.lock_retained_evidence_source(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence,
    )
    if fence is None:
        return False
    version_fence = (
        await documents.review_version_fences(
            session,
            [document_version_id],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    ).get(document_version_id)
    return (
        version_fence is not None
        and version_fence.document_id == document_id
        and version_fence.source_id == source_id == fence.id
        and version_fence.current_source_generation == fence.generation
    )


async def _trigger_evidence_current(
    session: AsyncSession,
    event: AutomationTrigger,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Require exact ready-event provenance and a locked retained-Source fence before inbox fan-out."""
    if event.document_evidence_revoked:
        return False
    if (event.document_id is None) != (event.document_version_id is None):
        return False
    try:
        validate_sample("new_document", event.payload)
    except (TypeError, ValueError):
        return False
    try:
        source_id = UUID(str(event.payload.get("source_id")))
    except (ValueError, TypeError):
        return False
    try:
        event_id = UUID(event.event_key)
    except (ValueError, TypeError):
        return False
    if str(event_id) != event.event_key:
        return False
    if event.document_id is not None and event.document_version_id is not None:
        return await _retained_document_evidence_current(
            session,
            event.payload,
            event_id=event_id,
            document_id=event.document_id,
            document_version_id=event.document_version_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    from modules.ingestion import public as ingestion

    proof = await ingestion.resolve_ready_event_provenance(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if proof is None or proof.source_id != source_id:
        return False
    if not await _retained_document_evidence_current(
        session,
        event.payload,
        event_id=event_id,
        document_id=proof.document_id,
        document_version_id=proof.document_version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    ):
        return False
    event.document_id, event.document_version_id = proof.document_id, proof.document_version_id
    return True


def _scrub_document_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only condition metadata copied from the Document and its Source."""
    return {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "title",
            "mime_type",
            "source_type",
            "source_id",
        }
    }


async def _legacy_run_evidence_current(
    session: AsyncSession,
    run: AutomationRun,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Prove a document-less legacy run from its exact event pair, then backfill its document identity.

    Requires the canonical ``trigger_event_id``/``event:<id>`` pair, a ready-event provenance whose
    Source equals the payload's, and current retained evidence. On success the run row is stamped
    with the proven document and version (the caller's transaction owns the commit).
    """
    event_id = _legacy_run_event_id(run)
    if (
        event_id is None
        or run.trigger_key != f"event:{event_id}"
        or run.trigger_event_id != str(event_id)
    ):
        return False
    from modules.ingestion import public as ingestion

    proof = await ingestion.resolve_ready_event_provenance(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if proof is None:
        return False
    try:
        if UUID(str(run.payload.get("source_id"))) != proof.source_id:
            return False
    except (ValueError, TypeError):
        return False
    if not await _retained_document_evidence_current(
        session,
        run.payload,
        event_id=event_id,
        document_id=proof.document_id,
        document_version_id=proof.document_version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    ):
        return False
    run.document_id, run.document_version_id = proof.document_id, proof.document_version_id
    return True


async def _run_evidence_current(
    session: AsyncSession,
    run: AutomationRun,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Require exact ready-event lineage and a locked retained-Source fence before run action admission."""
    if run.trigger_type != "new_document":
        return True
    if run.document_evidence_revoked or (run.document_id is None) != (
        run.document_version_id is None
    ):
        return False
    if run.document_id is None or run.document_version_id is None:
        return await _legacy_run_evidence_current(
            session,
            run,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
        )
    event_id = _canonical_uuid(run.trigger_event_id) if run.trigger_event_id is not None else None
    if (
        event_id is None
        or run.trigger_key != f"event:{event_id}"
        or run.trigger_event_id != str(event_id)
    ):
        return False
    return await _retained_document_evidence_current(
        session,
        run.payload,
        event_id=event_id,
        document_id=run.document_id,
        document_version_id=run.document_version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )


async def _legacy_event_matches_cleanup(
    session: AsyncSession,
    event_id: UUID,
    *,
    document_id: UUID,
    source_id: UUID,
    version_ids: tuple[UUID, ...],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> Any | None:
    """Use Ingestion's strict outbox resolver to classify one pre-sidecar event against a receipt."""
    from modules.ingestion import public as ingestion

    proof = await ingestion.resolve_ready_event_provenance(
        session,
        event_id,
        document_id=document_id,
        source_id=source_id,
        accepted_version_ids=version_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return proof


async def _event_belongs_elsewhere(
    session: AsyncSession,
    event_key: str | None,
    *,
    document_id: UUID,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """True only when a strict canonical ready event provably resolves to a different Document."""
    from modules.ingestion import public as ingestion

    try:
        event_id = UUID(event_key or "")
    except (ValueError, TypeError):
        return False
    if str(event_id) != event_key:
        return False
    # Retained-receipt fallback: an event of an already-deleted other Document is still foreign, not unresolved.
    proof = await ingestion.resolve_ready_event_provenance(
        session,
        event_id,
        allow_retained_receipt=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return proof is not None and proof.document_id != document_id


async def scrub_document_triggers(
    session: AsyncSession,
    *,
    operation_id: UUID,
    document_id: UUID,
    source_id: UUID,
    version_ids: tuple[UUID, ...],
    final_reference_page: bool = False,
    after: UUID | None = None,
    limit: int = 100,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AutomationCleanupProgress:
    """Scrub one stable keyset page of exact new-document inbox copies without committing.

    Direct rows use private sidecars plus exact ready-event receipt; legacy rows require an exact UUID
    outbox receipt and version in the detached cleanup scope. Unresolved row IDs are provisional until
    the caller marks its last reference page. The caller retains operation and both cursor positions.
    """
    if not 1 <= limit <= 100 or len(version_ids) > 100:
        raise ValueError("Automation trigger cleanup exceeds its page bound")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = select(AutomationTrigger).where(
        AutomationTrigger.workspace_id == scope.workspace_id,
        AutomationTrigger.owner_id == _actor(scope),
        AutomationTrigger.trigger_type == "new_document",
    )
    if after is not None:
        statement = statement.where(AutomationTrigger.id > after)
    rows = list(
        (
            await session.scalars(
                statement.order_by(AutomationTrigger.id).limit(limit).with_for_update()
            )
        ).all()
    )
    complete = len(rows) < limit
    changed = 0
    provisional: list[UUID] = []
    unavailable: list[UUID] = []
    for event in rows:
        if event.document_evidence_revoked:
            continue
        matched = (
            event.document_id == document_id
            and event.document_version_id is not None
            and event.payload.get("source_id") == str(source_id)
        )
        proven_version = event.document_version_id if matched else None
        if matched:
            try:
                event_id = UUID(event.event_key)
                proof = (
                    await _legacy_event_matches_cleanup(
                        session,
                        event_id,
                        document_id=document_id,
                        source_id=source_id,
                        version_ids=version_ids,
                        scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                    if str(event_id) == event.event_key
                    else None
                )
            except (ValueError, TypeError):
                proof = None
            matched = proof is not None and proof.document_version_id == event.document_version_id
        if not matched and event.document_id is None and event.document_version_id is None:
            try:
                event_id = UUID(event.event_key)
                if str(event_id) == event.event_key:
                    proof = await _legacy_event_matches_cleanup(
                        session,
                        event_id,
                        document_id=document_id,
                        source_id=source_id,
                        version_ids=version_ids,
                        scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                    )
                    matched = proof is not None
                    proven_version = proof.document_version_id if proof is not None else None
            except (ValueError, TypeError):
                matched = False
        if not matched:
            # Rows proven to belong to another Document (sidecar or resolvable ready event) are skipped;
            # only this Document's unmatched rows and genuinely undecidable legacy rows are reported.
            if event.document_id is not None and event.document_id != document_id:
                continue
            if (
                event.document_id is None
                and event.document_version_id is None
                and await _event_belongs_elsewhere(
                    session,
                    event.event_key,
                    document_id=document_id,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            ):
                continue
            (unavailable if final_reference_page else provisional).append(event.id)
            continue
        event.document_id = document_id
        event.document_version_id = proven_version
        event.document_evidence_revoked = True
        event.payload = _scrub_document_payload(event.payload)
        event.status = "processed"
        changed += 1
    if changed:
        await session.flush()
    return AutomationCleanupProgress(
        rows[-1].id if rows else after,
        complete,
        changed,
        operation_id,
        tuple(provisional),
        tuple(unavailable),
    )


@dataclass(frozen=True)
class _CleanupMatch:
    """Outcome of proving whether one automation run belongs to a cleaned-up document.

    ``proven_version`` is the document version the proof established (``None`` when unmatched) and
    ``event_id`` the canonical trigger event the run was recognized by, if any.
    """

    matched: bool
    proven_version: UUID | None
    event_id: UUID | None


def _canonical_uuid(token: str) -> UUID | None:
    """Parse ``token`` as a UUID only when it is already in canonical text form, else ``None``."""
    try:
        candidate = UUID(token)
    except (ValueError, TypeError):
        return None
    return candidate if str(candidate) == token else None


def _legacy_run_event_id(run: AutomationRun) -> UUID | None:
    """Recover the original trigger event ID of a legacy run that never recorded a document.

    Accepts a canonical ``trigger_event_id`` or, failing that, the ``event:<id>`` trigger key. The
    caller still requires the exact canonical pair of both fields before trusting the event as proof.
    """
    event_id = _canonical_uuid(run.trigger_event_id) if run.trigger_event_id is not None else None
    if event_id is None and run.trigger_key.startswith("event:"):
        candidate = _canonical_uuid(run.trigger_key.removeprefix("event:"))
        if candidate is not None and run.trigger_key == f"event:{candidate}":
            event_id = candidate
    return event_id


async def _match_run_for_cleanup(
    session: AsyncSession,
    run: AutomationRun,
    *,
    document_id: UUID,
    source_id: UUID,
    version_ids: tuple[UUID, ...],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> _CleanupMatch:
    """Decide whether ``run`` carries the cleaned document's evidence, by recorded fields or event proof.

    A run already pointing at this document and source is confirmed through its trigger event;
    a legacy run with no document at all is recovered from its event identity. Reads the inbox
    events through the cleanup proof helper; mutates nothing.
    """
    matched = (
        run.document_id == document_id
        and run.document_version_id is not None
        and run.payload.get("source_id") == str(source_id)
    )
    proven_version = run.document_version_id if matched else None
    event_id: UUID | None = None
    if matched:
        try:
            event_id = UUID(run.trigger_event_id or "")
            proof = (
                await _legacy_event_matches_cleanup(
                    session,
                    event_id,
                    document_id=document_id,
                    source_id=source_id,
                    version_ids=version_ids,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                if str(event_id) == run.trigger_event_id and run.trigger_key == f"event:{event_id}"
                else None
            )
        except (ValueError, TypeError):
            proof = None
        matched = proof is not None and proof.document_version_id == run.document_version_id
    if not matched and run.document_id is None and run.document_version_id is None:
        event_id = _legacy_run_event_id(run)
        # The run formatter stores the original event ID twice; require the exact pair.
        if (
            event_id is not None
            and run.trigger_key == f"event:{event_id}"
            and run.trigger_event_id == str(event_id)
        ):
            proof = await _legacy_event_matches_cleanup(
                session,
                event_id,
                document_id=document_id,
                source_id=source_id,
                version_ids=version_ids,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            matched = proof is not None
            proven_version = proof.document_version_id if proof is not None else None
    return _CleanupMatch(matched, proven_version, event_id)


async def _revoke_run_document_evidence(
    session: AsyncSession,
    run: AutomationRun,
    *,
    document_id: UUID,
    proven_version: UUID | None,
) -> None:
    """Scrub a proven run's payload and settle its actions, keeping action results and uncertainty.

    Locks the run's action rows in ordinal order. Pending/approved actions are skipped, an in-flight
    action becomes ``requires_review``, and the run itself is closed as ``requires_review`` when an
    effect is uncertain or ``dropped`` when it was still active.
    """
    from modules.automations.models import AutomationRunAction

    actions = list(
        (
            await session.scalars(
                select(AutomationRunAction)
                .where(AutomationRunAction.run_id == run.id)
                .order_by(AutomationRunAction.ordinal)
                .with_for_update()
            )
        ).all()
    )
    run.document_id = document_id
    run.document_version_id = proven_version
    run.document_evidence_revoked = True
    run.payload = _scrub_document_payload(run.payload)
    uncertain = any(action.status in {"in_flight", "requires_review"} for action in actions)
    for action in actions:
        if action.status in {"pending", "approved", "awaiting_approval"}:
            action.status, action.error_code, action.approved_session_hash = (
                "skipped",
                "document_evidence_revoked",
                None,
            )
        elif action.status == "in_flight":
            action.status, action.error_code = "requires_review", "document_evidence_revoked"
    if uncertain:
        run.status, run.reason, run.finished_at = (
            "requires_review",
            "document_evidence_revoked",
            datetime.now(UTC),
        )
    elif run.status in {"queued", "running", "awaiting_approval"}:
        run.status, run.reason, run.finished_at = (
            "dropped",
            "document_evidence_revoked",
            datetime.now(UTC),
        )


async def scrub_document_runs(
    session: AsyncSession,
    *,
    operation_id: UUID,
    document_id: UUID,
    source_id: UUID,
    version_ids: tuple[UUID, ...],
    final_reference_page: bool = False,
    after: UUID | None = None,
    limit: int = 100,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AutomationCleanupProgress:
    """Scrub one bounded exact run-payload page while retaining action results and effect uncertainty.

    Paged receipts require the caller to retain both evidence-page and run-ID cursor positions.
    """
    if not 1 <= limit <= 100 or len(version_ids) > 100:
        raise ValueError("Automation run cleanup exceeds its page bound")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = select(AutomationRun).where(
        AutomationRun.workspace_id == scope.workspace_id,
        AutomationRun.owner_id == _actor(scope),
        AutomationRun.trigger_type == "new_document",
    )
    if after is not None:
        statement = statement.where(AutomationRun.id > after)
    rows = list(
        (
            await session.scalars(
                statement.order_by(AutomationRun.id).limit(limit).with_for_update()
            )
        ).all()
    )
    complete = len(rows) < limit
    changed = 0
    provisional: list[UUID] = []
    unavailable: list[UUID] = []
    for run in rows:
        if run.document_evidence_revoked:
            continue
        match = await _match_run_for_cleanup(
            session,
            run,
            document_id=document_id,
            source_id=source_id,
            version_ids=version_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if run.document_id is not None and run.document_id != document_id:
            continue
        if (
            not match.matched
            and run.document_id is None
            and run.document_version_id is None
            and match.event_id is not None
            and await _event_belongs_elsewhere(
                session,
                str(match.event_id),
                document_id=document_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        ):
            continue
        contradictory = (
            run.document_id == document_id
            and run.document_version_id is not None
            and run.document_version_id not in version_ids
        )
        if contradictory or not match.matched:
            (unavailable if final_reference_page else provisional).append(run.id)
            continue
        await _revoke_run_document_evidence(
            session,
            run,
            document_id=document_id,
            proven_version=match.proven_version,
        )
        changed += 1
    if changed:
        await session.flush()
    return AutomationCleanupProgress(
        rows[-1].id if rows else after,
        complete,
        changed,
        operation_id,
        tuple(provisional),
        tuple(unavailable),
    )


async def _document_trigger_sources(
    session: AsyncSession,
    triggers: Sequence[AutomationTrigger],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    held: set[UUID] | None = None,
) -> set[UUID] | None:
    """Resolve the Source of each live ready-document trigger through ingestion provenance.

    Triggers of other types, already revoked ones and those without a canonical event key are
    skipped. With ``held`` given, returns ``None`` at the first Source outside it (the caller must
    not acquire an unheld Source fence while holding inbox locks); otherwise returns all Sources.
    Read-only; takes no locks.
    """
    from modules.ingestion import public as ingestion

    found: set[UUID] = set()
    for trigger in triggers:
        if trigger.trigger_type != "new_document" or trigger.document_evidence_revoked:
            continue
        try:
            event_id = UUID(trigger.event_key)
        except (TypeError, ValueError):
            continue
        if str(event_id) != trigger.event_key:
            continue
        proof = await ingestion.resolve_ready_event_provenance(
            session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if proof is None:
            continue
        if held is not None and proof.source_id not in held:
            return None
        found.add(proof.source_id)
    return found
