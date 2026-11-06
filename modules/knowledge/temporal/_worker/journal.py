"""Receipt witness index and journal inventory/binding reads for temporal graph operations.

Private implementation of ``modules.knowledge.temporal.worker``; import from that facade.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid5

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.workspaces.schemas import Scope
from modules.knowledge.entities import public as entities
from modules.knowledge.temporal._worker.admission import (
    _Admission,
    _admit,
    _commit,
    _get,
    _lease,
)
from modules.knowledge.temporal.adapter import (
    CanonicalEntityBinding,
    EvidenceIdentity,
    ExactFactState,
    ExactFactSupport,
    GraphOperationError,
    GraphReceiptInspection,
    GraphWriteReceipt,
    RecoveryReceiptAggregate,
)
from modules.knowledge.temporal.models import (
    GraphMapping,
    GraphOperation,
    GraphReceipt,
    GraphSupport,
)


async def _receipts(
    session: AsyncSession, operation_id: UUID, adm: _Admission
) -> tuple[GraphWriteReceipt, ...]:
    """Return actual immutable witnesses covering every distinct journal obligation; never truncate effects."""
    receipts, _ = await _receipt_inventory(session, operation_id, adm=adm)
    return receipts


@dataclass
class _WitnessIndex:
    """Incrementally index an append-only receipt ledger into bounded witness candidates.

    ``first``/``latest`` hold the earliest/latest receipt per phase and physical effect,
    ``matching`` the latest receipt whose fingerprint equals the live readback, and
    ``obligations`` the first receipt creating a shared/stale fact obligation. ``effects``
    accumulates the (entity, mention, fact, incident-link) id union for the aggregate.
    """

    inspection: GraphReceiptInspection | None
    witness_sequences: tuple[int, ...]
    digest: Any = field(default_factory=hashlib.sha256)
    first: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    latest: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    matching: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    obligations: dict[str, tuple[int, GraphWriteReceipt]] = field(default_factory=dict)
    extras: dict[int, GraphWriteReceipt] = field(default_factory=dict)
    effects: list[set[str]] = field(default_factory=lambda: [set(), set(), set(), set()])

    def __post_init__(self) -> None:
        """Precompute the live node and fact fingerprints the readback offers for matching."""
        inspection = self.inspection
        self.current_nodes = (
            dict(inspection.current_entity_state_fingerprints) if inspection else {}
        )
        self.current_facts = (
            {item.fact_id: item.state_fingerprint for item in inspection.current_fact_states}
            if inspection
            else {}
        )

    def add(self, cursor: int, payload: Any, receipt: GraphWriteReceipt) -> None:
        """Fold one receipt (at ledger position ``cursor``) into the digest, witnesses and effect union.

        Raises ``GraphOperationError`` when the effect union exceeds 100 ids.
        """
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.digest.update(f"{cursor}:".encode() + encoded.encode() + b"\n")
        entry = (cursor, receipt)
        keys = {"phase:" + receipt.phase}
        keys.update(
            "effect:" + identifier
            for identifier in (
                *receipt.entity_ids,
                *receipt.mention_ids,
                *receipt.fact_ids,
                *(link.edge_id for link in receipt.incident_links),
            )
        )
        for key in keys:
            self.first.setdefault(key, entry)
            self.latest[key] = entry
        if cursor in self.witness_sequences:
            self.extras[cursor] = receipt
        self._note_matching(entry)
        if receipt.phase == "cleanup_write_intent":
            self._note_obligations(entry)
        for destination, values in zip(
            self.effects,
            (
                receipt.entity_ids,
                receipt.mention_ids,
                receipt.fact_ids,
                tuple(link.edge_id for link in receipt.incident_links),
            ),
            strict=True,
        ):
            destination.update(values)
        if len(set().union(*self.effects)) > 100:
            raise GraphOperationError("graph_recovery_effect_inventory_over_bound")

    def _note_matching(self, entry: tuple[int, GraphWriteReceipt]) -> None:
        """Record the receipt as the current-matching witness for every effect/episode it fingerprints like the readback."""
        receipt = entry[1]
        for identifier, value in receipt.intended_entity_state_fingerprints:
            if value is not None and self.current_nodes.get(identifier) == value:
                self.matching["effect:" + identifier] = entry
        for identifier, present, value in receipt.prior_entity_state_fingerprints:
            if present and value is not None and self.current_nodes.get(identifier) == value:
                self.matching["effect:" + identifier] = entry
        states: tuple[ExactFactState | ExactFactSupport, ...] = (
            *receipt.existing_fact_states,
            *receipt.intended_fact_support,
        )
        for state in states:
            if self.current_facts.get(state.fact_id) == state.state_fingerprint:
                self.matching["effect:" + state.fact_id] = entry
        inspection = self.inspection
        if inspection and inspection.episode_state_fingerprint in {
            receipt.episode_state_fingerprint,
            receipt.prior_episode_state_fingerprint,
        }:
            self.matching["episode"] = entry
        if inspection:
            for link in receipt.incident_links:
                if link in inspection.incident_links:
                    self.matching["effect:" + link.edge_id] = entry

    def _note_obligations(self, entry: tuple[int, GraphWriteReceipt]) -> None:
        """Record the first cleanup receipt that leaves a shared-support or stale-validity fact obligation."""
        receipt = entry[1]
        prior_states = {state.fact_id: state for state in receipt.existing_fact_states}
        for intended in receipt.intended_fact_support:
            prior = prior_states.get(intended.fact_id)
            if prior is None or not prior.existed:
                continue
            target = str(receipt.episode_id)
            if (
                target in prior.episode_ids
                and target not in intended.episode_ids
                and intended.episode_ids
            ):
                self.obligations.setdefault("shared:" + intended.fact_id, entry)
            if (
                target not in prior.episode_ids
                and target not in intended.episode_ids
                and (prior.valid_at, prior.invalid_at, prior.expired_at)
                != (intended.valid_at, intended.invalid_at, intended.expired_at)
            ):
                self.obligations.setdefault("stale:" + intended.fact_id, entry)


async def _receipt_inventory(
    session: AsyncSession,
    operation_id: UUID,
    prefix_count: int | None = None,
    *,
    adm: _Admission,
    inspection: GraphReceiptInspection | None = None,
    witness_sequences: tuple[int, ...] = (),
) -> tuple[tuple[GraphWriteReceipt, ...], RecoveryReceiptAggregate | None]:
    """Page all original receipts and select bounded first/latest/current-matching witnesses per physical effect.

    Lifetime mutations are scanned without materializing their payloads. An
    original intermediate receipt is retained when its fingerprint matches the
    current exact readback; first prior and latest intended receipts also remain.
    The prefix digest binds every sequence/payload, including unselected history.
    Caller-supplied extra witness sequences are reread as originals and compared
    by the adapter against actual state; they cannot forge historical fingerprints.
    """
    adapter = TypeAdapter(GraphWriteReceipt)
    cursor = 0
    index = _WitnessIndex(inspection, witness_sequences)
    while prefix_count is None or cursor < prefix_count:
        query = select(GraphReceipt).where(
            GraphReceipt.workspace_id == adm.scope.workspace_id,
            GraphReceipt.operation_id == operation_id,
            GraphReceipt.sequence >= cursor,
        )
        if prefix_count is not None:
            query = query.where(GraphReceipt.sequence < prefix_count)
        rows = (await session.scalars(query.order_by(GraphReceipt.sequence).limit(100))).all()
        if not rows:
            break
        for row in rows:
            if row.sequence != cursor:
                raise GraphOperationError("graph_receipt_inventory_invalid")
            index.add(cursor, row.payload, adapter.validate_python(row.payload))
            cursor += 1
    if prefix_count is not None and cursor != prefix_count:
        raise GraphOperationError("graph_receipt_prefix_missing")
    selected = dict(index.extras)
    selected.update(
        {
            sequence: receipt
            for sequence, receipt in (
                *index.first.values(),
                *index.latest.values(),
                *index.matching.values(),
                *index.obligations.values(),
            )
        }
    )
    entries = tuple(sorted(selected.items()))
    if len(entries) > 512 or len(index.extras) != len(witness_sequences):
        raise GraphOperationError("graph_recovery_witness_inventory_invalid")
    if not entries:
        return (), None
    head = entries[0][1]
    effects = index.effects
    aggregate = RecoveryReceiptAggregate(
        operation_id=head.operation_id,
        group_id=head.group_id,
        lease_token=head.lease_token,
        mapping_revision=head.mapping_revision,
        ledger_count=cursor,
        ledger_digest=index.digest.hexdigest(),
        witness_sequences=tuple(sequence for sequence, _ in entries),
        entity_ids=tuple(sorted(effects[0])),
        mention_ids=tuple(sorted(effects[1])),
        fact_ids=tuple(sorted(effects[2])),
        incident_link_ids=tuple(sorted(effects[3])),
    )
    return tuple(receipt for _, receipt in entries), aggregate


async def _record(
    factory: async_sessionmaker[AsyncSession],
    adm: _Admission,
    operation_id: UUID,
    token: UUID,
    receipt: GraphWriteReceipt,
) -> None:
    """Commit every no-text prewrite receipt independently while source/document locks remain held by its caller."""
    await _lease(factory, adm, operation_id, token)
    async with factory() as session:
        await _admit(session, adm)
        operation = await _get(session, GraphOperation, operation_id, adm, lock=True)
        if operation is None:
            raise RuntimeError("invariant violated: operation must not be None")
        mapping = await _get(session, GraphMapping, operation.mapping_id, adm)
        if mapping is None:
            raise RuntimeError("invariant violated: mapping must not be None")
        if (
            receipt.operation_id != operation.id
            or receipt.lease_token != operation.receipt_token
            or receipt.mapping_revision != operation.desired_revision
            or receipt.episode_id != mapping.episode_id
            or receipt.group_id != str(mapping.partition_id)
        ):
            raise GraphOperationError("graph_receipt_not_owned")
        sequence = await session.scalar(
            select(func.max(GraphReceipt.sequence)).where(
                GraphReceipt.workspace_id == adm.scope.workspace_id,
                GraphReceipt.operation_id == operation.id,
            )
        )
        # Adapter strips all seed/candidate text from receipts; reject accidental leakage
        # instead of relying on a subsequent purge to remove copied narrative fields.
        if any(
            binding.node_name is not None or binding.node_summary is not None
            for binding in receipt.canonical_bindings
        ):
            raise GraphOperationError("graph_receipt_contains_seed_text")
        payload = TypeAdapter(GraphWriteReceipt).dump_python(receipt, mode="json")
        session.add(
            GraphReceipt(
                workspace_id=adm.scope.workspace_id,
                operation_id=operation.id,
                sequence=0 if sequence is None else sequence + 1,
                payload=payload,
            )
        )
        operation.phase = receipt.phase
        mapping.external_state = "unknown"
        await _commit(session, adm)


async def _inventory(
    session: AsyncSession, mapping: GraphMapping
) -> tuple[list[GraphMapping], list[GraphSupport]]:
    """Read every reservation/support in a selected bounded bucket, including historical/unknown rows."""
    rows = list(
        (
            await session.scalars(
                select(GraphMapping)
                .where(
                    GraphMapping.workspace_id == mapping.workspace_id,
                    GraphMapping.partition_id == mapping.partition_id,
                )
                .order_by(GraphMapping.id)
                .limit(101)
            )
        ).all()
    )
    if len(rows) > 100 or len({row.document_version_id for row in rows}) > 100:
        raise GraphOperationError("graph_partition_over_bound")
    supports = list(
        (
            await session.scalars(
                select(GraphSupport)
                .where(
                    GraphSupport.workspace_id == mapping.workspace_id,
                    GraphSupport.mapping_id.in_([row.id for row in rows]),
                )
                .order_by(GraphSupport.mapping_id, GraphSupport.chunk_id)
                .limit(101)
            )
        ).all()
    )
    if len(supports) > 100:
        raise GraphOperationError("graph_partition_support_over_bound")
    return rows, supports


async def _bindings(
    session: AsyncSession,
    mapping: GraphMapping,
    supports: list[GraphSupport],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[CanonicalEntityBinding, ...]:
    """Resolve exact owner memberships/seeds, assigning stable graph UUIDs only to proven canonical identities."""
    memberships = []
    by_version: dict[UUID, list[UUID]] = {}
    for support in supports:
        if not support.removed:
            by_version.setdefault(support.document_version_id, []).append(support.chunk_id)
    for version_id, chunk_ids in sorted(by_version.items(), key=lambda item: str(item[0])):
        memberships.extend(
            await entities.list_retained_version_membership_refs(
                session,
                version_id,
                chunk_ids,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    if len(memberships) > 100:
        raise GraphOperationError("graph_membership_closure_over_bound")
    seeds = (
        await entities.get_temporal_node_seeds(
            session,
            [item.membership_id for item in memberships],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if memberships
        else ()
    )
    bound_ids = {seed.entity_id for seed in seeds}
    if bound_ids != {item.entity_id for item in memberships}:
        raise GraphOperationError("graph_canonical_seed_unavailable")
    result = []
    support_by_ref = {
        (row.document_version_id, row.chunk_id): row for row in supports if not row.removed
    }
    for seed in seeds:
        evidence = []
        for member in seed.memberships:
            row = support_by_ref[(member.document_version_id, member.chunk_id)]
            evidence.append(
                EvidenceIdentity(
                    row.source_id,
                    row.source_generation,
                    row.document_id,
                    row.document_version_id,
                    row.chunk_id,
                )
            )
        fields = [("name", seed.name_hash, tuple(seed.name_support_membership_ids))]
        if seed.summary_hash is not None:
            fields.append(
                ("description", seed.summary_hash, tuple(seed.summary_support_membership_ids))
            )
        result.append(
            CanonicalEntityBinding(
                canonical_entity_id=seed.entity_id,
                canonical_revision=seed.revision,
                membership_ids=tuple(member.id for member in seed.memberships),
                evidence=tuple(sorted(set(evidence), key=str)),
                graph_entity_uuid=str(uuid5(mapping.partition_id, str(seed.entity_id))),
                group_id=str(mapping.partition_id),
                mapping_revision=mapping.desired_revision,
                node_name=seed.name,
                node_summary=seed.summary,
                field_support=tuple(fields),
            )
        )
    return tuple(result)
