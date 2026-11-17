"""Document-copied agent evidence cleanup: cursors, candidate selection, ledgers, per-run passes and the purge page contract."""

import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
)
from core.pagination import b64url_decode, b64url_encode
from core.workspaces.schemas import Scope
from modules.agents._public.runs import (
    _restore_fences,
)
from modules.agents._public.types import (
    AGENT_CLEANUP_LIMIT,
    AGENT_INPUT_PROVENANCE_VERSION,
    AgentCleanupLeasePreflight,
    AgentCopiedEvidenceCleanupProgress,
)
from modules.agents.access import actor, admit
from modules.agents.leases import try_agent_run_lease_in_uow
from modules.agents.models import (
    AgentApproval,
    AgentEffect,
    AgentEvidenceCleanup,
    AgentRun,
    AgentToolCall,
)
from modules.knowledge.documents.public import DocumentCleanupEvidenceScope


def _agent_cleanup_records(scope: DocumentCleanupEvidenceScope) -> list[dict[str, object]]:
    """Validate a detached Documents page and project exact identities for JSONB containment queries."""
    if (
        not isinstance(scope.operation_id, UUID)
        or not isinstance(scope.source_id, UUID)
        or not isinstance(scope.document_id, UUID)
        or len(scope.references) > 100
    ):
        raise ValueError("Invalid Agent cleanup evidence scope")
    records: list[dict[str, object]] = []
    seen: set[tuple[str, UUID, UUID | None]] = set()
    for reference in scope.references:
        kind, version_id, chunk_id = (
            reference.reference_kind,
            reference.document_version_id,
            reference.chunk_id,
        )
        if (
            kind not in {"version", "chunk"}
            or not isinstance(version_id, UUID)
            or (kind == "version" and chunk_id is not None)
            or (kind == "chunk" and not isinstance(chunk_id, UUID))
        ):
            raise ValueError("Invalid Agent cleanup evidence identity")
        identity = (kind, version_id, chunk_id)
        if identity in seen:
            raise ValueError("Duplicate Agent cleanup evidence identity")
        seen.add(identity)
        records.append(
            {
                "document_id": str(scope.document_id),
                "document_version_id": str(version_id),
                "source_id": str(scope.source_id),
                "chunk_id": str(chunk_id) if chunk_id is not None else None,
            }
        )
    return records


def _agent_cleanup_fingerprint(
    scope: DocumentCleanupEvidenceScope, records: list[dict[str, object]]
) -> str:
    """Bind a cleanup cursor to the immutable operation, document, source, and reference page."""
    payload = {
        "operation": str(scope.operation_id),
        "source": str(scope.source_id),
        "document": str(scope.document_id),
        "records": records,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _agent_cleanup_scope_fingerprint(scope: DocumentCleanupEvidenceScope) -> str:
    """Bind the durable Agent receipt to operation/source/document, independent of reference pages."""
    payload = {
        "operation": str(scope.operation_id),
        "source": str(scope.source_id),
        "document": str(scope.document_id),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _encode_agent_cleanup_cursor(
    scope: DocumentCleanupEvidenceScope,
    fingerprint: str,
    after: UUID | None,
    unavailable: bool,
) -> str:
    """Encode a canonical operation-and-page-bound Agent keyset continuation."""
    payload = {
        "v": 1,
        "operation": str(scope.operation_id),
        "fingerprint": fingerprint,
        "after": str(after) if after else None,
        "unavailable": unavailable,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return b64url_encode(raw)


def _decode_agent_cleanup_cursor(
    cursor: str,
    scope: DocumentCleanupEvidenceScope,
    fingerprint: str,
) -> tuple[UUID | None, bool]:
    """Reject malformed, noncanonical, or cross-operation/reference-page cleanup cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid Agent copied-evidence cursor")
        raw = b64url_decode(cursor)
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "operation", "fingerprint", "after", "unavailable"}
            or payload["v"] != 1
            or payload["operation"] != str(scope.operation_id)
            or payload["fingerprint"] != fingerprint
            or type(payload["unavailable"]) is not bool
            or (payload["after"] is not None and not isinstance(payload["after"], str))
        ):
            raise ValueError("Agent copied-evidence cursor belongs to another scope")
        after = UUID(payload["after"]) if payload["after"] is not None else None
        if (
            _encode_agent_cleanup_cursor(scope, fingerprint, after, payload["unavailable"])
            != cursor
        ):
            raise ValueError("Agent copied-evidence cursor is not canonical")
        return after, payload["unavailable"]
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid Agent copied-evidence cursor") from exc


def _fences_match_agent_scope(value: object, records: list[dict[str, object]]) -> bool:
    """Match only strict document/version/chunk records from a captured native Agent fence."""
    if not isinstance(value, dict):
        return False
    try:
        restored = _restore_fences(value)
    except (TypeError, ValueError, KeyError):
        return False
    for fence in restored["records"]:
        for record in records:
            if (
                str(fence.document_id) == record["document_id"]
                and str(fence.document_version_id) == record["document_version_id"]
                and str(fence.source_id) == record["source_id"]
                and (str(fence.chunk_id) if fence.chunk_id is not None else None)
                == record["chunk_id"]
            ):
                return True
    return False


def _agent_scope_contains(value: object, record: dict[str, object]) -> bool:
    """Check one exact immutable identity in a strict bounded native fence."""
    return _fences_match_agent_scope(value, [record])


def _strict_agent_fences(value: object) -> dict[str, object] | None:
    """Return a decoded classified fence, including a valid empty fence, or None for legacy/invalid."""
    if not isinstance(value, dict):
        return None
    try:
        return _restore_fences(value)
    except (TypeError, ValueError, KeyError):
        return None


async def _agent_cleanup_candidate_ids(
    session: AsyncSession,
    scope: DocumentCleanupEvidenceScope,
    records: list[dict[str, object]],
    after: UUID | None,
    limit: int,
    *,
    workspace_id: UUID,
    owner_id: int,
) -> list[UUID]:
    """Read a bounded UUID keyset page using exact JSONB containment and durable operation receipts."""
    run_clauses = []
    for record in records:
        identity = {
            key: record[key]
            for key in (
                "document_id",
                "document_version_id",
                "source_id",
                "chunk_id",
            )
        }
        run_clauses.append(AgentRun.source_fences.contains({"records": [identity]}))
    related_call = (
        exists(
            select(AgentToolCall.id).where(
                AgentToolCall.run_id == AgentRun.id,
                or_(
                    *[
                        AgentToolCall.input_source_fences.contains(
                            {
                                "records": [
                                    {
                                        key: record[key]
                                        for key in (
                                            "document_id",
                                            "document_version_id",
                                            "source_id",
                                            "chunk_id",
                                        )
                                    }
                                ]
                            }
                        )
                        for record in records
                    ]
                ),
            )
        )
        if records
        else False
    )
    related_approval = (
        exists(
            select(AgentApproval.id).where(
                AgentApproval.run_id == AgentRun.id,
                AgentApproval.workspace_id == workspace_id,
                or_(
                    *[
                        AgentApproval.source_fences.contains(
                            {
                                "records": [
                                    {
                                        key: record[key]
                                        for key in (
                                            "document_id",
                                            "document_version_id",
                                            "source_id",
                                            "chunk_id",
                                        )
                                    }
                                ]
                            }
                        )
                        for record in records
                    ]
                ),
            )
        )
        if records
        else False
    )
    durable_call_count = (
        select(func.count(AgentToolCall.id))
        .where(
            AgentToolCall.run_id == AgentRun.id,
        )
        .correlate(AgentRun)
        .scalar_subquery()
    )
    legacy_active = and_(
        AgentRun.status.in_({"queued", "running", "waiting_approval"}),
        exists(
            select(AgentToolCall.id).where(
                AgentToolCall.run_id == AgentRun.id,
                AgentToolCall.input_provenance_version.is_(None),
            )
        ),
    )
    # Supported old read-only checkpoints can reserve a durable tool counter without an
    # AgentToolCall row. Keep the active slot unavailable until exact owner evidence is found.
    legacy_unreconciled_active = and_(
        AgentRun.status.in_({"queued", "running", "waiting_approval"}),
        AgentRun.tool_calls > durable_call_count,
    )
    marker_exists = exists(
        select(AgentEvidenceCleanup.id).where(
            AgentEvidenceCleanup.run_id == AgentRun.id,
            AgentEvidenceCleanup.workspace_id == workspace_id,
            AgentEvidenceCleanup.operation_id == scope.operation_id,
        )
    )
    cleanup_clauses: list[Any] = [
        *run_clauses,
        related_call,
        related_approval,
        legacy_active,
        legacy_unreconciled_active,
        marker_exists,
    ]
    query = select(AgentRun.id).where(
        AgentRun.workspace_id == workspace_id,
        AgentRun.owner_id == owner_id,
        or_(*cleanup_clauses),
    )
    if after is not None:
        query = query.where(AgentRun.id > after)
    return list((await session.scalars(query.order_by(AgentRun.id).limit(limit + 1))).all())


async def _admit_cleanup_evidence(
    session: AsyncSession,
    evidence: DocumentCleanupEvidenceScope,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Admit the owner (members denied before SQL) and bind the detached page to this workspace."""
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if evidence.workspace_id != scope.workspace_id or evidence.actor_user_id != actor(scope):
        raise ConflictError("Document cleanup evidence belongs to another workspace")


async def preflight_document_copied_evidence_lease(
    session: AsyncSession,
    evidence: DocumentCleanupEvidenceScope,
    *,
    cursor: str | None = None,
    limit: int = AGENT_CLEANUP_LIMIT,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> AgentCleanupLeasePreflight:
    """Acquire a pending run's nonblocking saver lease before Documents/Memory locks.

    This reads only detached evidence identities, candidate IDs and an operation receipt. The
    transaction advisory lease, when required, stays on this session for the caller's commit.
    """
    if type(limit) is not int or not 1 <= limit <= AGENT_CLEANUP_LIMIT:
        raise ValueError(f"Agent cleanup page size must be between 1 and {AGENT_CLEANUP_LIMIT}")
    await _admit_cleanup_evidence(
        session,
        evidence,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    workspace_id, owner_id = scope.workspace_id, actor(scope)
    records = _agent_cleanup_records(evidence)
    fingerprint = _agent_cleanup_fingerprint(evidence, records)
    after, _ = (
        _decode_agent_cleanup_cursor(cursor, evidence, fingerprint) if cursor else (None, False)
    )
    candidate_ids = await _agent_cleanup_candidate_ids(
        session,
        evidence,
        records,
        after,
        1,
        workspace_id=workspace_id,
        owner_id=owner_id,
    )
    run_id = candidate_ids[0] if candidate_ids else None
    marker = None
    if run_id is not None:
        marker = await session.scalar(
            select(AgentEvidenceCleanup).where(
                AgentEvidenceCleanup.operation_id == evidence.operation_id,
                AgentEvidenceCleanup.workspace_id == workspace_id,
                AgentEvidenceCleanup.run_id == run_id,
            )
        )
    marker_state = (
        "none"
        if run_id is None
        else (
            "finalized"
            if marker is not None and marker.finalized_at is not None
            else marker.state
            if marker is not None
            else "absent"
        )
    )
    lease_required = marker_state == "pending"
    lease_acquired = (
        await try_agent_run_lease_in_uow(session, run_id)
        if lease_required and run_id is not None
        else False
    )
    return AgentCleanupLeasePreflight(
        evidence.operation_id,
        fingerprint,
        run_id,
        cursor,
        marker_state,
        lease_required,
        lease_acquired,
        lease_required and not lease_acquired,
    )


async def _agent_cleanup_ledgers(
    session: AsyncSession,
    run_id: UUID,
    *,
    workspace_id: UUID,
) -> tuple[list[AgentApproval], list[AgentEffect], list[AgentToolCall]]:
    """Lock the Agent action ledger in run→approval→effect→call order, inside one workspace."""
    approvals = list(
        (
            await session.scalars(
                select(AgentApproval)
                .where(
                    AgentApproval.run_id == run_id,
                    AgentApproval.workspace_id == workspace_id,
                )
                .order_by(AgentApproval.id)
                .with_for_update()
            )
        ).all()
    )
    effects = list(
        (
            await session.scalars(
                select(AgentEffect)
                .where(
                    AgentEffect.run_id == run_id,
                    AgentEffect.workspace_id == workspace_id,
                )
                .order_by(AgentEffect.action_id)
                .with_for_update()
            )
        ).all()
    )
    calls = list(
        (
            await session.scalars(
                select(AgentToolCall)
                .where(
                    AgentToolCall.run_id == run_id,
                    # AgentToolCall carries no workspace column; bind it through its workspace-scoped run.
                    AgentToolCall.run_id.in_(
                        select(AgentRun.id).where(
                            AgentRun.id == run_id,
                            AgentRun.workspace_id == workspace_id,
                        )
                    ),
                )
                .order_by(AgentToolCall.ordinal)
                .with_for_update()
            )
        ).all()
    )
    return approvals, effects, calls


def _revoke_agent_approval(
    approval: AgentApproval, effect: AgentEffect | None, now: datetime
) -> None:
    """Fence a proven dependent action while retaining its durable external-effect outcome."""
    if effect is not None:
        if effect.state == "in_flight":
            effect.state = "requires_review"
        elif effect.state == "reserved":
            effect.state = "failed"
        effect.payload = None
    if approval.status in {"pending", "approved"}:
        approval.status = (
            "requires_review"
            if effect is not None and effect.state == "requires_review"
            else "cancelled"
        )
    approval.arguments = None
    approval.resolved_at = approval.resolved_at or now


def _scrub_agent_scope_payloads(
    approvals: list[AgentApproval],
    effects: list[AgentEffect],
    calls: list[AgentToolCall],
    records: list[dict[str, object]],
    now: datetime,
) -> bool:
    """Clear exact action payloads and report retained calls lacking usable per-slot lineage.

    A strict approval snapshot joined to the same run/ordinal can classify a legacy call. Otherwise
    a nonempty legacy call payload remains untouched and prevents a clean completion claim.
    """
    effects_by_action = {effect.action_id: effect for effect in effects}
    approvals_by_ordinal = {approval.ordinal: approval for approval in approvals}
    unavailable = False
    for call in calls:
        matched = False
        classified = False
        if call.input_provenance_version == AGENT_INPUT_PROVENANCE_VERSION:
            fences = _strict_agent_fences(call.input_source_fences)
            classified = fences is not None
            matched = classified and _fences_match_agent_scope(call.input_source_fences, records)
        elif call.input_provenance_version is None:
            approval = approvals_by_ordinal.get(call.ordinal)
            approval_fences = (
                _strict_agent_fences(approval.source_fences) if approval is not None else None
            )
            if approval_fences is not None:
                classified = True
                if approval is None:
                    raise RuntimeError("invariant violated: approval must not be None")
                matched = _fences_match_agent_scope(approval.source_fences, records)
        if matched:
            # Input provenance justifies clearing derived arguments. Output references may identify
            # independent native evidence (such as BrowserPageEvidence), so retain those links.
            call.arguments = {}
            if call.status == "started":
                call.status, call.error_code, call.completed_at = "denied", "evidence_revoked", now
        elif not classified and (bool(call.arguments) or bool(call.evidence_refs)):
            unavailable = True
    for approval in approvals:
        if _fences_match_agent_scope(approval.source_fences, records):
            _revoke_agent_approval(approval, effects_by_action.get(approval.action_id), now)
    return unavailable


async def _agent_unavailable_run_count(
    session: AsyncSession,
    operation_id: UUID,
    workspace_id: UUID,
) -> int:
    """Count distinct per-operation/run unavailable receipts through their unique lookup index."""
    return int(
        await session.scalar(
            select(func.count(AgentEvidenceCleanup.id)).where(
                AgentEvidenceCleanup.operation_id == operation_id,
                AgentEvidenceCleanup.workspace_id == workspace_id,
                AgentEvidenceCleanup.state == "unavailable",
            )
        )
        or 0
    )


def _find_agent_scope_match(
    run: AgentRun,
    approvals: list[AgentApproval],
    calls: list[AgentToolCall],
    records: list[dict[str, object]],
) -> dict[str, object] | None:
    """Find the first strict exact run, classified call-input, or approval fence identity."""
    for record in records:
        if _agent_scope_contains(run.source_fences, record):
            return record
    for call in calls:
        if call.input_provenance_version == AGENT_INPUT_PROVENANCE_VERSION:
            for record in records:
                if _agent_scope_contains(call.input_source_fences, record):
                    return record
    for approval in approvals:
        for record in records:
            if _agent_scope_contains(approval.source_fences, record):
                return record
    return None


def _agent_cleanup_marker_state(marker: AgentEvidenceCleanup | None) -> str:
    """Return effective state, with a durable finalization timestamp taking precedence."""
    if marker is None:
        return "absent"
    return "finalized" if marker.finalized_at is not None else marker.state


@dataclass
class _CleanupPass:
    """Mutable state of one Agent copied-evidence cleanup transaction (at most one run per page).

    The caller owns the unit of work: no method here commits. ``last_processed``/``processed``/
    ``unavailable`` accumulate across the (single) run so every early return reports the same
    cursor and counters the monolithic loop did.
    """

    session: AsyncSession
    evidence: DocumentCleanupEvidenceScope
    records: list[dict[str, object]]
    workspace_id: UUID
    owner_id: int
    page_fingerprint: str
    scope_fingerprint: str
    preflight: AgentCleanupLeasePreflight
    cursor: str | None
    last_processed: UUID | None
    unavailable: bool
    processed: int = 0

    async def progress(
        self,
        next_cursor: str | None,
        complete: bool,
        rows_processed: int,
        unavailable_result: bool,
        lease_pending: bool = False,
        preflight_stale: bool = False,
    ) -> AgentCopiedEvidenceCleanupProgress:
        """Count operation-wide unique unavailable receipts; leave flush and commit to the caller's UoW."""
        return AgentCopiedEvidenceCleanupProgress(
            next_cursor,
            complete,
            rows_processed,
            unavailable_result,
            await _agent_unavailable_run_count(
                self.session, self.evidence.operation_id, self.workspace_id
            ),
            lease_pending,
            preflight_stale,
        )

    def encode(self, unavailable_flag: bool) -> str:
        """Encode the opaque resume cursor after the last fully processed run."""
        return _encode_agent_cleanup_cursor(
            self.evidence,
            self.page_fingerprint,
            self.last_processed,
            unavailable_flag,
        )

    async def stale(self) -> AgentCopiedEvidenceCleanupProgress:
        """Report that the detached preflight no longer matches durable state (caller rolls back)."""
        return await self.progress(
            self.cursor, False, self.processed, self.unavailable, preflight_stale=True
        )

    def advance(self, run_id: UUID) -> None:
        """Mark ``run_id`` as fully processed for the cursor and row counters."""
        self.last_processed, self.processed = run_id, self.processed + 1

    async def lock_run(self, run_id: UUID) -> AgentRun | None:
        """Take the AgentRun row lock (after Memory privacy) scoped to this workspace and owner."""
        return await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == self.workspace_id,
                AgentRun.owner_id == self.owner_id,
            )
            .with_for_update()
        )

    async def load_marker(
        self,
        run_id: UUID,
        *,
        lock: bool,
        populate: bool = False,
    ) -> AgentEvidenceCleanup | None:
        """Read this operation's cleanup marker for ``run_id``, optionally locked and refreshed."""
        statement = select(AgentEvidenceCleanup).where(
            AgentEvidenceCleanup.operation_id == self.evidence.operation_id,
            AgentEvidenceCleanup.workspace_id == self.workspace_id,
            AgentEvidenceCleanup.run_id == run_id,
        )
        if lock:
            statement = statement.with_for_update()
        if populate:
            statement = statement.execution_options(populate_existing=True)
        return await self.session.scalar(statement)

    def new_marker(
        self,
        run_id: UUID,
        state: str,
        matched_identity: dict[str, object] | None,
    ) -> AgentEvidenceCleanup:
        """Build and add a cleanup marker bound to this operation's source/document scope."""
        marker = AgentEvidenceCleanup(
            workspace_id=self.workspace_id,
            operation_id=self.evidence.operation_id,
            run_id=run_id,
            source_id=self.evidence.source_id,
            document_id=self.evidence.document_id,
            scope_fingerprint=self.scope_fingerprint,
            matched_identity=matched_identity,
            state=state,
        )
        self.session.add(marker)
        return marker

    def scrub(
        self,
        approvals: Any,
        effects: Any,
        calls: Any,
        now: datetime | None = None,
    ) -> bool:
        """Scrub exact-match action payloads; return whether any identity stayed unresolved."""
        return _scrub_agent_scope_payloads(
            approvals, effects, calls, self.records, now or datetime.now(UTC)
        )


def _revoke_run_for_cleanup(run: AgentRun) -> None:
    """Revoke a matched run's evidence: cancel, bump dispatch generation, clear the answer.

    Queued and waiting runs become ``cancelled`` immediately; running ones are stopped by the worker
    observing ``cancel_requested``.
    """
    run.evidence_revoked = True
    run.cancel_requested = True
    run.dispatch_generation += 1
    run.answer = None
    if run.status in {"queued", "waiting_approval"}:
        run.status, run.completed_at = "cancelled", datetime.now(UTC)
    run.updated_at = datetime.now(UTC)


async def _cleanup_finalized_marker(
    work: _CleanupPass,
    run_id: UUID,
) -> AgentCopiedEvidenceCleanupProgress | None:
    """Scrub a later reference page for a run whose checkpoint was already erased.

    Documents may deliver later reference pages for the same operation/run. The opaque checkpoint
    is already gone, but each page still needs its own selective ledger scrub. Lock order: privacy,
    run, marker, then ledgers. Returns a stale-preflight progress or ``None`` to continue.
    """
    from modules.memory.public import lock_export_privacy

    await lock_export_privacy(work.session, work.workspace_id)
    run = await work.lock_run(run_id)
    marker = await work.load_marker(run_id, lock=True, populate=True)
    if _agent_cleanup_marker_state(marker) != work.preflight.marker_state:
        return await work.stale()
    if run is not None:
        approvals, effects, calls = await _agent_cleanup_ledgers(
            work.session,
            run_id,
            workspace_id=work.workspace_id,
        )
        unresolved = work.scrub(approvals, effects, calls)
        if marker is None:
            raise RuntimeError("invariant violated: marker must not be None")
        marker.state = "unavailable" if unresolved else "finalized"
        work.unavailable = work.unavailable or unresolved or marker.state == "unavailable"
    work.advance(run_id)
    return None


async def _cleanup_unavailable_marker(
    work: _CleanupPass,
    run_id: UUID,
) -> AgentCopiedEvidenceCleanupProgress | None:
    """Re-test an ``unavailable`` receipt against this page's exact identities.

    An unassociated legacy active row is a coverage gate, not permission to cancel or erase an
    unrelated run. A later reference page may prove a real match and promote the receipt into the
    ordinary revoke/finalize sequence (``pending`` plus a lease-pending cursor).
    """
    from modules.memory.public import lock_export_privacy

    await lock_export_privacy(work.session, work.workspace_id)
    run = await work.lock_run(run_id)
    marker = await work.load_marker(run_id, lock=True, populate=True)
    if _agent_cleanup_marker_state(marker) != work.preflight.marker_state:
        return await work.stale()
    if run is None:
        work.advance(run_id)
        work.unavailable = True
        return None
    approvals, _effects, calls = await _agent_cleanup_ledgers(
        work.session, run_id, workspace_id=work.workspace_id
    )
    matching = _find_agent_scope_match(run, approvals, calls, work.records)
    if matching is None:
        work.advance(run_id)
        work.unavailable = True
        return None
    if marker is None:
        raise RuntimeError("invariant violated: marker must not be None")
    marker.matched_identity = matching
    marker.state = "pending"
    _revoke_run_for_cleanup(run)
    await work.session.flush()
    return await work.progress(
        work.encode(True), False, work.processed + 1, True, lease_pending=True
    )


async def _cleanup_existing_marker_phase_a(
    work: _CleanupPass,
    run_id: UUID,
    run: AgentRun,
    marker: AgentEvidenceCleanup,
) -> AgentCopiedEvidenceCleanupProgress | None:
    """Handle a marker that appeared between the unlocked read and the locked re-read (Phase A)."""
    approvals, effects, calls = await _agent_cleanup_ledgers(
        work.session, run_id, workspace_id=work.workspace_id
    )
    if marker.state == "unavailable" and marker.finalized_at is None:
        matching = _find_agent_scope_match(run, approvals, calls, work.records)
        if matching is None:
            work.advance(run_id)
            work.unavailable = True
            return None
        marker.matched_identity, marker.state = matching, "pending"
        _revoke_run_for_cleanup(run)
        await work.session.flush()
        return await work.progress(work.encode(True), False, work.processed + 1, True)
    unresolved = work.scrub(approvals, effects, calls)
    if unresolved:
        marker.state = "unavailable"
    work.unavailable = work.unavailable or unresolved or marker.state == "unavailable"
    work.advance(run_id)
    return None


async def _cleanup_phase_a(
    work: _CleanupPass, run_id: UUID
) -> AgentCopiedEvidenceCleanupProgress | None:
    """Phase A: commit an operation-scoped denial before attempting physical saver erasure.

    No lease is awaited or held while the transaction takes owner rows. A run with no exact match is
    persisted once as an ``unavailable`` coverage gate (keyset replay stays finite) and never mutated;
    a matching run gets a ``pending`` marker, revocation and a lease-pending cursor.
    """
    from modules.memory.public import lock_export_privacy

    await lock_export_privacy(work.session, work.workspace_id)
    run = await work.lock_run(run_id)
    if run is None:
        work.advance(run_id)
        return None
    marker = await work.load_marker(run_id, lock=True)
    if _agent_cleanup_marker_state(marker) != work.preflight.marker_state:
        return await work.stale()
    if marker is not None and marker.scope_fingerprint != work.scope_fingerprint:
        return await work.progress(work.encode(True), False, work.processed, True)
    if marker is not None and marker.state == "pending":
        return await work.progress(
            work.encode(work.unavailable), False, work.processed, work.unavailable
        )
    if marker is not None:
        return await _cleanup_existing_marker_phase_a(work, run_id, run, marker)
    approvals, _effects, calls = await _agent_cleanup_ledgers(
        work.session, run_id, workspace_id=work.workspace_id
    )
    matching = _find_agent_scope_match(run, approvals, calls, work.records)
    if matching is None:
        # An unclassified active legacy row is only a coverage gate. Persist it once so
        # keyset replay is finite, but never revoke, erase, or otherwise mutate that run.
        work.new_marker(run_id, "unavailable", None)
        await work.session.flush()
        work.advance(run_id)
        work.unavailable = True
        return None
    work.new_marker(run_id, "pending", matching)
    _revoke_run_for_cleanup(run)
    await work.session.flush()
    return await work.progress(
        work.encode(work.unavailable),
        False,
        work.processed + 1,
        work.unavailable,
        lease_pending=True,
    )


async def _cleanup_phase_b(
    work: _CleanupPass, run_id: UUID
) -> AgentCopiedEvidenceCleanupProgress | None:
    """Phase B: erase opaque saver state for a ``pending`` marker once the run lease is held.

    The detached try-lock conflicts with the worker's session lease and was acquired before Memory
    privacy or Agent rows; contention leaves this run at the current cursor position. Lock order:
    privacy, run, marker, then ledgers; checkpoint tables are deleted for the run's thread.
    """
    from modules.memory.public import lock_export_privacy

    preflight = work.preflight
    if preflight.blocked or (preflight.lease_required and not preflight.lease_acquired):
        return await work.progress(
            work.encode(work.unavailable), False, work.processed, work.unavailable
        )
    await lock_export_privacy(work.session, work.workspace_id)
    run = await work.lock_run(run_id)
    marker = await work.load_marker(run_id, lock=True)
    if _agent_cleanup_marker_state(marker) != preflight.marker_state or (
        _agent_cleanup_marker_state(marker) == "pending"
        and (not preflight.lease_required or not preflight.lease_acquired)
    ):
        return await work.stale()
    if run is None or marker is None or marker.scope_fingerprint != work.scope_fingerprint:
        return await work.progress(work.encode(True), False, work.processed, True)
    approvals, effects, calls = await _agent_cleanup_ledgers(
        work.session, run_id, workspace_id=work.workspace_id
    )
    now = datetime.now(UTC)
    unresolved = work.scrub(approvals, effects, calls, now)
    if marker.state == "finalized" or marker.finalized_at is not None:
        marker.state = "unavailable" if unresolved else "finalized"
        work.unavailable = work.unavailable or unresolved or marker.state == "unavailable"
        work.advance(run_id)
        return None
    if marker.state != "pending":
        return await work.progress(work.encode(True), False, work.processed, True)
    run.answer = None
    run.evidence_revoked = True
    run.cancel_requested = True
    if run.status in {"queued", "waiting_approval"}:
        run.status = "cancelled"
        run.completed_at = run.completed_at or now
    run.updated_at = now
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        await work.session.execute(
            text(f"DELETE FROM {table} WHERE thread_id = :thread_id"),  # noqa: S608  # table name comes from a pinned literal tuple; thread id is bound
            {"thread_id": run.checkpoint_thread_id},
        )
    marker.state = "unavailable" if unresolved else "finalized"
    marker.finalized_at = now
    work.unavailable = work.unavailable or unresolved
    work.advance(run_id)
    return None


async def _cleanup_one_run(
    work: _CleanupPass, run_id: UUID
) -> AgentCopiedEvidenceCleanupProgress | None:
    """Dispatch one candidate run to the phase its durable marker state calls for.

    The marker is read unlocked first; its state is re-checked after lease acquisition and before
    locking mutable Agent rows. A pending marker always needs an affirmative lease for this exact
    run before any privacy/receipt locks. Returns a progress to return immediately, or ``None``.
    """
    preflight, evidence = work.preflight, work.evidence
    marker = await work.load_marker(run_id, lock=False)
    current_marker_state = _agent_cleanup_marker_state(marker)
    if current_marker_state != preflight.marker_state or (
        current_marker_state == "pending"
        and (not preflight.lease_required or not preflight.lease_acquired)
    ):
        return await work.progress(work.cursor, False, 0, work.unavailable, preflight_stale=True)
    if marker is not None and (
        marker.source_id != evidence.source_id
        or marker.document_id != evidence.document_id
        or marker.scope_fingerprint != work.scope_fingerprint
    ):
        return await work.progress(work.encode(True), False, work.processed, True)
    if marker is not None and (marker.state == "finalized" or marker.finalized_at is not None):
        return await _cleanup_finalized_marker(work, run_id)
    if marker is not None and marker.state == "unavailable":
        return await _cleanup_unavailable_marker(work, run_id)
    if marker is None:
        return await _cleanup_phase_a(work, run_id)
    return await _cleanup_phase_b(work, run_id)


async def purge_document_copied_evidence_page(
    session: AsyncSession,
    evidence: DocumentCleanupEvidenceScope,
    *,
    cursor: str | None = None,
    limit: int = AGENT_CLEANUP_LIMIT,
    scope: Scope,
    multi_workspace_enabled: bool,
    preflight: AgentCleanupLeasePreflight,
) -> AgentCopiedEvidenceCleanupProgress:
    """Revoke and selectively scrub one bounded Agent page for a detached Documents cleanup scope.

    The caller owns commit. First encounter durably marks the run revoked and returns a cursor before
    it; a later delivery must acquire the worker's nonblocking run lease before privacy/run/ledger
    locks and deleting opaque saver state. Lock order is lease, Memory privacy, AgentRun, approvals,
    effects, then calls. Only strict source/document/version/chunk fence matches scrub action payloads.
    """
    if type(limit) is not int or not 1 <= limit <= AGENT_CLEANUP_LIMIT:
        raise ValueError(f"Agent cleanup page size must be between 1 and {AGENT_CLEANUP_LIMIT}")
    await _admit_cleanup_evidence(
        session,
        evidence,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    workspace_id, owner_id = scope.workspace_id, actor(scope)
    records = _agent_cleanup_records(evidence)
    page_fingerprint = _agent_cleanup_fingerprint(evidence, records)
    scope_fingerprint = _agent_cleanup_scope_fingerprint(evidence)
    after, unavailable = (
        _decode_agent_cleanup_cursor(cursor, evidence, page_fingerprint)
        if cursor
        else (None, False)
    )
    work = _CleanupPass(
        session,
        evidence,
        records,
        workspace_id,
        owner_id,
        page_fingerprint,
        scope_fingerprint,
        preflight,
        cursor,
        after,
        unavailable,
    )
    # One run per transaction keeps the caller's detached lease preflight aligned with the
    # exact candidate whose Agent rows this hook may lock. Later runs use this opaque cursor.
    if preflight.operation_id != evidence.operation_id or preflight.agent_cursor != cursor:
        raise ValueError("Agent cleanup lease preflight does not match the Documents cursor")
    if preflight.scope_fingerprint != page_fingerprint:
        raise ValueError(
            "Agent cleanup lease preflight does not match the Documents evidence scope"
        )
    if preflight.blocked:
        return await work.progress(cursor, False, 0, unavailable)
    candidate_ids = await _agent_cleanup_candidate_ids(
        session,
        evidence,
        records,
        after,
        1,
        workspace_id=workspace_id,
        owner_id=owner_id,
    )
    more_candidates = len(candidate_ids) > 1
    page = candidate_ids[:1]
    if (page[0] if page else None) != preflight.candidate_run_id:
        return await work.progress(cursor, False, 0, unavailable, preflight_stale=True)
    for run_id in page:
        early = await _cleanup_one_run(work, run_id)
        if early is not None:
            return early
    next_cursor = work.encode(work.unavailable) if more_candidates else None
    return await work.progress(next_cursor, not more_candidates, work.processed, work.unavailable)
