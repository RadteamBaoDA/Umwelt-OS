"""Temporal owner scheduling and reads; helpers flush while callers own atomic canonical commits."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import KnowledgeChanged, make_knowledge_change
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.sources import public as sources
from modules.knowledge.temporal.models import (
    GraphAllocation, GraphChange, GraphDispatch, GraphMapping, GraphOperation, GraphPartition,
    GraphReconcileMember, GraphReconcileRun, GraphSupport,
)
from modules.knowledge.temporal.schemas import ChangePage, ChangeRead, GraphStatus, ReconcileRequest, ReconcileStatus


def digest(value: object) -> str:
    """Hash deterministically serialized detached IDs/revisions; never use this as authorization."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _cursor(value: int, fingerprint: str) -> str:
    """Bind stable row continuation to the normalized filter fingerprint."""
    return base64.urlsafe_b64encode(json.dumps([value, fingerprint]).encode()).decode()


def _after(cursor: str | None, fingerprint: str) -> int:
    """Validate a bounded opaque continuation and reject cross-filter reuse."""
    if cursor is None:
        return 0
    try:
        if len(cursor) > 1024:
            raise ValueError
        value, bound = json.loads(base64.urlsafe_b64decode(cursor))
        if bound != fingerprint or not isinstance(value, int) or value < 0:
            raise ValueError
        return value
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid temporal cursor") from exc


async def schedule_version(session: AsyncSession, ready: documents.ReadyVersionRef) -> UUID:
    """Reserve one immutable bounded bucket/episode and durable intent under the caller's source/document fence.

    No provider work or commit occurs here. Bucket slots include tombstones and failed
    attempts permanently; retry never moves an episode into a new graph context.
    Out-of-bound immutable input creates blocked mapping/intent status without
    rolling back canonical readiness or truncating evidence for graph inference.
    """
    mapping = await session.scalar(select(GraphMapping).where(
        GraphMapping.document_version_id == ready.document_version_id,
        GraphMapping.source_generation == ready.source_generation,
    ).with_for_update())
    if mapping is not None:
        return mapping.id
    input_error = None
    try:
        data = await documents.read_extraction_input(session, ready.document_version_id)
    except documents.ExtractionInputLimitError:
        # An optional projection's capacity limit must not poison the canonical
        # ready event/outbox transaction or its other consumers' durable work.
        data = None
        input_error = "graph_extraction_input_limit"
    chunk_count = len(data.chunks) if data is not None else 0
    await session.execute(insert(GraphAllocation).values(
        source_id=ready.source_id, generation=ready.source_generation, next_bucket=0,
    ).on_conflict_do_nothing())
    allocation = await session.get(GraphAllocation, (ready.source_id, ready.source_generation), with_for_update=True)
    partition = await session.scalar(select(GraphPartition).where(
        GraphPartition.source_id == ready.source_id, GraphPartition.generation == ready.source_generation,
        GraphPartition.sealed.is_(False),
    ).order_by(GraphPartition.ordinal.desc()).limit(1).with_for_update())
    if partition is None or partition.reservations >= 100 or partition.evidence_reservations + chunk_count > 100:
        if partition is not None:
            partition.sealed = True
        partition = GraphPartition(source_id=ready.source_id, generation=ready.source_generation,
                                   ordinal=allocation.next_bucket, reservations=0)
        allocation.next_bucket += 1
        session.add(partition)
        await session.flush()
    partition.reservations += 1
    partition.evidence_reservations += chunk_count
    state = {"version_id": str(ready.document_version_id), "generation": ready.source_generation}
    mapping = GraphMapping(partition_id=partition.id, source_id=ready.source_id,
                           source_generation=ready.source_generation, document_id=ready.document_id,
                           document_version_id=ready.document_version_id, local_only=ready.local_only,
                           desired_digest=digest(state), canonical_state=state)
    session.add(mapping)
    await session.flush()
    if data is not None:
        for chunk in data.chunks:
            session.add(GraphSupport(mapping_id=mapping.id, document_version_id=ready.document_version_id,
                                     chunk_id=chunk.id, document_id=ready.document_id,
                                     source_id=ready.source_id, source_generation=ready.source_generation))
    operation_id = await _queue(session, mapping, "upsert")
    if input_error is not None:
        mapping.status, mapping.error_code = "blocked", input_error
        operation = await session.get(GraphOperation, operation_id)
        operation.status, operation.error_code = "blocked", input_error
        # Version chunks are immutable; model/dependency retries cannot make
        # over-limit input valid. A new ready version gets its own mapping.
        operation.next_attempt_at = datetime.max.replace(tzinfo=UTC)
    return mapping.id


async def _queue(session: AsyncSession, mapping: GraphMapping, kind: str) -> UUID:
    """Flush one idempotent current revision intent; historical receipts remain on the original attempt."""
    operation = await session.scalar(select(GraphOperation).where(
        GraphOperation.mapping_id == mapping.id, GraphOperation.desired_revision == mapping.desired_revision,
        GraphOperation.kind == kind, GraphOperation.status.in_(["pending", "running", "blocked", "reconcile_needed"]),
    ).order_by(GraphOperation.created_at.desc()).limit(1))
    if operation is None:
        operation = GraphOperation(mapping_id=mapping.id, partition_id=mapping.partition_id, kind=kind,
                                   desired_revision=mapping.desired_revision, desired_digest=mapping.desired_digest,
                                   next_attempt_at=datetime.now(UTC), replacement_created_at=datetime.now(UTC))
        session.add(operation)
        await session.flush()
    return operation.id


async def schedule_canonical_change(session: AsyncSession, *, kind: str, canonical_id: UUID,
                                    revision: int | None, fields: list[str], support: list[tuple[UUID, UUID]],
                                    origin: str, deleted: bool = False) -> None:
    """Atomically capture identifier-only canonical change and refresh every affected temporal desired state.

    Call after owner mutations while their locks are held, before owner commit/replay.
    Support is exact owner evidence, not inferred names; deleted text is never retained.
    """
    if kind not in {"entity", "relationship", "event"} or len(fields) > 100:
        raise ValueError("Invalid canonical change identity")
    pairs = sorted(set(support), key=lambda item: (str(item[0]), str(item[1])))
    session.add(GraphChange(kind=kind, canonical_id=canonical_id, revision=revision,
                            fingerprint=digest([kind, str(canonical_id), revision, fields, pairs, deleted]),
                            changed_fields=sorted(set(fields)), origin=origin, deleted=deleted,
                            support=[[str(version), str(chunk)] for version, chunk in pairs]))
    version_ids = {item[0] for item in pairs}
    # Existing identity linkage also covers edits/deletion whose current evidence is empty.
    rows = (await session.scalars(select(GraphMapping).where(
        (GraphMapping.document_version_id.in_(version_ids)) |
        (GraphMapping.canonical_state.contains({kind + "_ids": [str(canonical_id)]})),
    ).order_by(GraphMapping.id).with_for_update())).all()
    for mapping in rows:
        if mapping.tombstoned:
            continue
        state = dict(mapping.canonical_state)
        ids = set(state.get(kind + "_ids", []))
        if deleted:
            ids.discard(str(canonical_id))
        else:
            ids.add(str(canonical_id))
        state[kind + "_ids"] = sorted(ids)
        state["change"] = [kind, str(canonical_id), revision, deleted]
        mapping.canonical_state = state
        mapping.desired_revision += 1
        mapping.desired_digest = digest(state)
        mapping.status, mapping.error_code = "pending", None
        await _queue(session, mapping, "reconcile" if mapping.applied_revision else "upsert")
    await session.flush()


async def tombstone_scope(session: AsyncSession, *, document_id: UUID | None = None,
                          source_id: UUID | None = None) -> None:
    """Capture exact detached cleanup identity before source/document cascades; never call Graphiti here."""
    if (document_id is None) == (source_id is None):
        raise ValueError("Choose one temporal cleanup scope")
    condition = GraphMapping.document_id == document_id if document_id else GraphMapping.source_id == source_id
    mappings = (await session.scalars(select(GraphMapping).where(condition).order_by(GraphMapping.id).with_for_update())).all()
    for mapping in mappings:
        mapping.tombstoned, mapping.status = True, "tombstoned"
        mapping.desired_revision += 1
        mapping.canonical_state = {}  # Purge former derived identities without removing the exact cleanup ledger.
        mapping.desired_digest = digest([str(mapping.id), "deleted", mapping.desired_revision])
        supports = (await session.scalars(select(GraphSupport).where(GraphSupport.mapping_id == mapping.id))).all()
        for item in supports:
            item.removed = True
        await _queue(session, mapping, "delete")
    await session.flush()


async def mapping_statuses(session: AsyncSession, version_ids: list[UUID], *, graph_enabled: bool = False) -> list[GraphStatus]:
    """Read retained owner-version graph status under the current source-generation fence.

    Missing/deleted versions and stale source generations expose no detached graph
    identifiers. Disabling the optional graph does not hide retained canonical data.
    """
    if len(version_ids) > 100:
        raise ValueError("Status batch exceeds100 versions")
    fences = await documents.review_version_fences(session, version_ids)
    if not fences:
        return []
    # Apply the owner fence in SQL: filtering after .all() would load every
    # historical generation for each requested version without a memory bound.
    retained = or_(*(and_(GraphMapping.document_version_id == version_id,
                         GraphMapping.document_id == fence.document_id,
                         GraphMapping.source_id == fence.source_id,
                         GraphMapping.source_generation == fence.current_source_generation)
                     for version_id, fence in fences.items()))
    rows = (await session.scalars(select(GraphMapping).where(
        retained, GraphMapping.tombstoned.is_(False),
    ).order_by(GraphMapping.created_at.desc()))).all()
    return [GraphStatus(mapping_id=row.id, document_version_id=row.document_version_id,
                        episode_id=row.episode_id, partition_id=row.partition_id, status=row.status,
                        desired_revision=row.desired_revision, applied_revision=row.applied_revision,
                        error_code=row.error_code, graph_enabled=graph_enabled, applied_at=row.applied_at)
            for row in rows
            if (fence := fences.get(row.document_version_id)) is not None
            and fence.document_id == row.document_id and fence.source_id == row.source_id
            and fence.current_source_generation == row.source_generation]


async def select_search_partitions(session: AsyncSession, partition_ids: list[UUID]) -> dict[UUID, tuple[UUID, ...]]:
    """Snapshot complete synchronized episode inventories for 1..10 explicit buckets.

    No graph/model calls, leases or commits occur. Each bucket has at most100
    lifetime mappings; deleted entries require exact cleanup proof. Active entries
    must retain owner versions in the current active source generation. Missing,
    pending, uncertain or writer-owned buckets reject the whole selection. This
    snapshot is not authorization: consumers must acquire owned dispatch and
    revalidate complete support/current policy immediately before search_at.
    """
    if not 1 <= len(partition_ids) <= 10 or len(set(partition_ids)) != len(partition_ids):
        raise ValueError("Choose 1 to 10 unique graph partitions")
    result = {}
    now = datetime.now(UTC)
    for partition_id in partition_ids:
        partition = await session.get(GraphPartition, partition_id)
        if (partition is None or partition.uncertain_operation_id is not None
                or (partition.lease_expires_at is not None and partition.lease_expires_at > now)):
            raise ValueError("Graph partition requires recovery or is busy")
        source = await sources.get_connector_source(session, partition.source_id)
        if source is None or source.status != "active" or source.generation != partition.generation:
            raise ValueError("Graph partition source generation is unavailable")
        unresolved = await session.scalar(select(GraphDispatch.id).join(
            GraphOperation, GraphOperation.id == GraphDispatch.operation_id,
        ).where(GraphOperation.partition_id == partition_id, GraphDispatch.completed_at.is_(None),
                GraphDispatch.cessation_verified_at.is_(None)).limit(1))
        if unresolved is not None:
            raise ValueError("Graph partition dispatch has not ceased")
        rows = (await session.scalars(select(GraphMapping).where(
            GraphMapping.partition_id == partition_id,
        ).order_by(GraphMapping.id).limit(101))).all()
        if len(rows) > 100:
            raise ValueError("Graph partition exceeds its lifetime mapping bound")
        active = [row for row in rows if not row.tombstoned]
        fences = await documents.review_version_fences(session, [row.document_version_id for row in active])
        for row in rows:
            if (row.source_id != partition.source_id or row.source_generation != partition.generation
                    or row.applied_revision != row.desired_revision or row.applied_digest != row.desired_digest
                    or row.error_code is not None):
                raise ValueError("Graph partition contains unconverged mappings")
            if row.tombstoned:
                if row.external_state != "absent":
                    raise ValueError("Graph partition deletion is not proven")
            else:
                fence = fences.get(row.document_version_id)
                if (row.status != "synchronized" or row.external_state != "present" or fence is None
                        or fence.document_id != row.document_id or fence.source_id != row.source_id
                        or fence.current_source_generation != row.source_generation):
                    raise ValueError("Graph partition evidence is unavailable")
        result[partition_id] = tuple(row.episode_id for row in active)
    return result


async def graph_status_change(session: AsyncSession, mapping_id: UUID) -> KnowledgeChanged:
    """Build an identifier-only invalidation for atomic status/replay publication.

    Caller holds the mapping's publication locks and commits with commit_with_replay.
    A projection deletion does not imply canonical document deletion. Source scope
    covers mappings with no entity/event yet and survives retained-owner deletion.
    """
    mapping = await session.get(GraphMapping, mapping_id)
    if mapping is None:
        raise ValueError("Graph mapping is unavailable")
    return make_knowledge_change(mapping.source_id, mapping.document_id)


async def request_reconcile(session: AsyncSession, request: ReconcileRequest) -> UUID:
    """Atomically snapshot selected IDs/revisions in PostgreSQL; the HTTP caller commits before202.

    INSERT SELECT keeps large source scopes out of application memory. Subsequent
    corrections cannot add members or silently substitute another desired revision.
    """
    scope = request.model_dump(mode="json", exclude_none=True)
    condition = await _scope_condition(session, scope)
    run = GraphReconcileRun(scope=scope, status="pending")
    session.add(run)
    await session.flush()
    await session.execute(insert(GraphReconcileMember).from_select(
        ["run_id", "mapping_id", "desired_revision", "desired_digest", "tombstoned"],
        select(literal(run.id), GraphMapping.id, GraphMapping.desired_revision,
               GraphMapping.desired_digest, GraphMapping.tombstoned).where(condition),
    ))
    run.upper_mapping_id = await session.scalar(select(GraphReconcileMember.mapping_id).where(
        GraphReconcileMember.run_id == run.id,
    ).order_by(GraphReconcileMember.mapping_id.desc()).limit(1))
    if run.upper_mapping_id is None:
        run.status = "succeeded"
    await session.flush()
    return run.id


async def _scope_condition(session: AsyncSession, scope: dict[str, object]):
    """Resolve a run's selected canonical identity through current owner refs, preserving its saved filter."""
    if scope.get("source_id"):
        return GraphMapping.source_id == UUID(str(scope["source_id"]))
    if scope.get("document_version_ids"):
        return GraphMapping.document_version_id.in_([UUID(str(value)) for value in scope["document_version_ids"]])
    try:
        canonical = await entities.resolve_canonical_entity_id(session, UUID(str(scope["entity_id"])))
    except LookupError as exc:
        raise ValueError("Entity scope is unavailable") from exc
    return GraphMapping.canonical_state.contains({"entity_ids": [str(canonical)]})


async def reconcile_slice(session: AsyncSession, run_id: UUID, *, limit: int = 25) -> bool:
    """Queue a bounded slice of immutable selected revisions without re-resolving mutable scope.

    Superseded members remain visible as blocked coverage. Work is never queued for
    the replacement revision on behalf of a run that did not select that revision.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Reconcile slice must be1..100")
    run = await session.get(GraphReconcileRun, run_id, with_for_update=True)
    if run is None or run.upper_mapping_id is None:
        return False
    filters = [GraphReconcileMember.run_id == run.id]
    if run.cursor:
        filters.append(GraphReconcileMember.mapping_id > run.cursor)
    members = (await session.scalars(select(GraphReconcileMember).where(*filters)
        .order_by(GraphReconcileMember.mapping_id).limit(limit + 1))).all()
    for member in members[:limit]:
        row = await session.get(GraphMapping, member.mapping_id, with_for_update=True)
        run.scanned += 1
        # Do not lock the partition after a mapping: worker ownership uses the
        # opposite order. A quarantined bucket needs recovery even if its old
        # applied metadata still matches this selected canonical revision.
        partition = await session.get(GraphPartition, row.partition_id) if row is not None else None
        projection_safe = partition is not None and partition.uncertain_operation_id is None
        if (row is not None and row.desired_revision == member.desired_revision
                and row.desired_digest == member.desired_digest and row.tombstoned == member.tombstoned
                and row.status not in ("blocked", "failed")
                and not (projection_safe and row.status == "synchronized" and row.applied_revision == member.desired_revision
                         and row.applied_digest == member.desired_digest)
                and not (projection_safe and row.tombstoned and row.external_state == "absent"
                         and row.applied_revision == member.desired_revision
                         and row.applied_digest == member.desired_digest and row.error_code is None)):
            await _queue(session, row, "delete" if row.tombstoned else "reconcile")
        run.cursor = member.mapping_id
    # 'partial' marks complete scanning, not permanent remote failure. GET computes
    # current outcomes from exact member revisions even after this scanner stops.
    run.status = "running" if len(members) > limit else "partial"
    await session.flush()
    return len(members) > limit


async def reconcile_status(session: AsyncSession, run_id: UUID) -> ReconcileStatus | None:
    """Aggregate current exact-revision outcomes for already scanned immutable members.

    Reads perform no writes. Queue counts are outstanding scanned work; terminal
    success requires all selected members converged and the scan exhausted.
    Missing ledgers fail, superseded revisions and uncertain partitions block;
    a live writer or unresolved dispatch prevents certifying physical convergence.
    """
    run = await session.get(GraphReconcileRun, run_id)
    if run is None:
        return None
    matching = and_(GraphMapping.desired_revision == GraphReconcileMember.desired_revision,
                    GraphMapping.desired_digest == GraphReconcileMember.desired_digest,
                    GraphMapping.tombstoned == GraphReconcileMember.tombstoned)
    # 'absent' is also a fresh mapping's default and can survive a later failed
    # delete. Only the current applied revision/digest certifies cleanup completion.
    uncertain = or_(GraphPartition.id.is_(None), GraphPartition.uncertain_operation_id.is_not(None))
    unresolved = select(GraphDispatch.id).join(GraphOperation,
        GraphOperation.id == GraphDispatch.operation_id).where(
        GraphOperation.partition_id == GraphMapping.partition_id,
        GraphDispatch.completed_at.is_(None), GraphDispatch.cessation_verified_at.is_(None),
    ).correlate(GraphMapping).exists()
    # Applied metadata survives an interrupted sibling write. Shared physical
    # state is not proved while that bucket is quarantined or still has a writer.
    physical_ready = and_(~uncertain, ~unresolved,
        or_(GraphPartition.lease_expires_at.is_(None), GraphPartition.lease_expires_at <= datetime.now(UTC)))
    converged = and_(matching, physical_ready,
        GraphMapping.applied_revision == GraphReconcileMember.desired_revision,
        GraphMapping.applied_digest == GraphReconcileMember.desired_digest,
        GraphMapping.error_code.is_(None), or_(
        and_(GraphMapping.tombstoned.is_(True), GraphMapping.external_state == "absent"),
        and_(GraphMapping.tombstoned.is_(False), GraphMapping.status == "synchronized"),
    ))
    failed = or_(GraphMapping.id.is_(None), and_(matching, GraphMapping.status == "failed"))
    blocked = and_(GraphMapping.id.is_not(None), ~failed, or_(~matching, uncertain,
                   and_(~converged, GraphMapping.status == "blocked")))
    filters = [GraphReconcileMember.run_id == run.id]
    if run.cursor is None:
        filters.append(literal(False))
    else:
        filters.append(GraphReconcileMember.mapping_id <= run.cursor)
    counts = (await session.execute(select(
        func.count(), func.coalesce(func.sum(case((converged, 1), else_=0)), 0),
        func.coalesce(func.sum(case((blocked, 1), else_=0)), 0),
        func.coalesce(func.sum(case((failed, 1), else_=0)), 0),
    ).select_from(GraphReconcileMember).outerjoin(GraphMapping,
        GraphMapping.id == GraphReconcileMember.mapping_id).outerjoin(GraphPartition,
        GraphPartition.id == GraphMapping.partition_id).where(*filters))).one()
    scanned, done, held, errors = (int(value) for value in counts)
    queued = scanned - done - held - errors
    scanning = run.status in ("pending", "running")
    status = run.status if scanning else ("succeeded" if not (queued or held or errors) else "partial")
    return ReconcileStatus(run_id=run.id, status=status, scanned=scanned, queued=queued,
                           converged=done, blocked=held, failed=errors,
                           continuation=str(run.cursor) if run.status == "running" and run.cursor else None)


async def find_changes(session: AsyncSession, *, kind: str | None = None, canonical_id: UUID | None = None,
                       observed_from: datetime | None = None, observed_to: datetime | None = None,
                       limit: int = 50, cursor: str | None = None) -> ChangePage:
    """Page recorded owner mutations and currently retained citations; never fabricate prior snapshots."""
    if not 1 <= limit <= 100 or any(value is not None and value.tzinfo is None for value in (observed_from, observed_to)):
        raise ValueError("Invalid change bounds")
    fingerprint = digest([kind, canonical_id, observed_from, observed_to])
    filters = [GraphChange.id > _after(cursor, fingerprint)]
    if kind:
        filters.append(GraphChange.kind == kind)
    if canonical_id:
        filters.append(GraphChange.canonical_id == canonical_id)
    if observed_from:
        filters.append(GraphChange.created_at >= observed_from)
    if observed_to:
        filters.append(GraphChange.created_at < observed_to)
    rows = (await session.scalars(select(GraphChange).where(*filters).order_by(GraphChange.id).limit(limit + 1))).all()
    items = []
    for row in rows[:limit]:
        refs = []
        if not row.deleted:
            # A historical ID is not authority to recover removed evidence text.
            for offset in range(0, len(row.support), 100):
                batch = [(UUID(version), UUID(chunk)) for version, chunk in row.support[offset:offset + 100]]
                try:
                    refs.extend(await documents.read_evidence_refs(session, batch))
                except ValueError:
                    continue
        if row.origin == "derived" and (not refs or len(refs) != len(row.support)):
            # Audit identity/field metadata can reveal revoked material too; only a fully
            # current permitted support closure authorizes a derived historical row.
            continue
        items.append(ChangeRead(id=row.id, kind=row.kind, canonical_id=row.canonical_id,
                                revision=row.revision, changed_fields=row.changed_fields, origin=row.origin,
                                deleted=row.deleted, observed_at=row.created_at,
                                evidence=[item.model_dump(mode="json") for item in refs]))
    return ChangePage(items=items, next_cursor=_cursor(rows[limit - 1].id, fingerprint) if len(rows) > limit else None)
