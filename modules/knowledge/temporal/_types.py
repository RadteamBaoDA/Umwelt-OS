"""Graph state, bounded-ID constants, receipt/action value objects and operation authorization."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from core.config import Settings
from core.model_gateway.schemas import ModelMapping, RequestPolicy

logger = logging.getLogger(__name__)


OPERATION_SECONDS = 120
QUERY_SECONDS = 5
STARTUP_SECONDS = 15
MAX_SUPPORT = 100
# Original first/latest/current states, shared and stale obligation witnesses,
# and at most two anchors for each of five receipt phases plus episode state.
MAX_RECOVERY_WITNESSES = 5 * MAX_SUPPORT + 12
MAX_HISTORY = 10
MAX_INPUT_BYTES = 64_000
MAX_QUERY_BYTES = 4_096
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_RESULTS = 50
MAX_CANDIDATES = 100
MAX_MODEL_CALLS = 32
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_ABSENT_FACT_FINGERPRINT = hashlib.sha256(b"absent").hexdigest()


class GraphState(StrEnum):
    """Graph readiness without exposing connection details."""

    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"
    READY = "ready"


class GraphOperationError(RuntimeError):
    """Stable graph error category safe to persist without private query data."""


class GraphOperationUnknown(GraphOperationError):
    """A graph write may have partially completed and requires exact-ID reconciliation."""

    def __init__(self, message: str, receipt: GraphWriteReceipt | None = None) -> None:
        """Carry a stable category and any durable identifier receipt without private graph text."""
        super().__init__(message)
        self.receipt = receipt


@dataclass(frozen=True)
class EvidenceIdentity:
    """Identify one exact retained chunk under a source-generation fence."""

    source_id: UUID
    source_generation: int
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID


@dataclass(frozen=True)
class CanonicalEntityBinding:
    """Bind owner identity to optional source-proved graph seed fields and exact field support."""

    canonical_entity_id: UUID
    canonical_revision: int
    membership_ids: tuple[UUID, ...]
    evidence: tuple[EvidenceIdentity, ...]
    graph_entity_uuid: str
    group_id: str
    mapping_revision: int
    node_name: str | None = None
    node_summary: str | None = None
    field_support: tuple[tuple[str, str, tuple[UUID, ...]], ...] = ()
    prior_node_state_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """Validate bounded owner identity and require each optional seed value's exact field proof."""
        if (self.canonical_revision < 1 or self.mapping_revision < 0 or not self.group_id
                or len(self.group_id.encode()) > 200 or not self.membership_ids
                or len(self.membership_ids) > MAX_SUPPORT
                or len(set(self.membership_ids)) != len(self.membership_ids)
                or any(not isinstance(item, UUID) for item in self.membership_ids)
                or not self.evidence or len(self.evidence) > MAX_SUPPORT
                or not _UUID.fullmatch(self.graph_entity_uuid)
                or len(self.field_support) > 2):
            raise ValueError("Canonical entity binding exceeds its owner bounds")
        hashes: dict[str, str] = {}
        for field, digest, support_ids in self.field_support:
            if (field not in {"name", "description"} or field in hashes
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not support_ids or len(set(support_ids)) != len(support_ids)
                    or not set(support_ids) <= set(self.membership_ids)):
                raise ValueError("Canonical entity field support is invalid")
            hashes[field] = digest
        if self.node_name is not None and (
            not self.node_name.strip() or len(self.node_name.encode()) > 4096
            or hashes.get("name") != hashlib.sha256(self.node_name.encode()).hexdigest()
        ):
            raise ValueError("Canonical graph node name requires exact source field support")
        if self.node_summary is not None and (
            len(self.node_summary.encode()) > MAX_INPUT_BYTES
            or hashes.get("description") != hashlib.sha256(self.node_summary.encode()).hexdigest()
        ):
            raise ValueError("Canonical graph node summary requires exact source field support")
        if (self.prior_node_state_fingerprint is not None
                and not re.fullmatch(r"[0-9a-f]{64}", self.prior_node_state_fingerprint)):
            raise ValueError("Canonical graph node prior state must be a SHA256 fingerprint")


@dataclass(frozen=True)
class CanonicalNodeRecoveryAction:
    """Apply one owner-authorized exact-node recovery decision under the current lease.

    delete_for_rebuild carries the complete incident inventory and sorted bounded
    dependent mapping IDs; the owner commits their rebuild schedules before any
    mutation. Orphan deletion continues to require no incident relationships.
    """

    graph_entity_uuid: str
    expected_current_state_fingerprint: str | None
    action: Literal["retain", "delete_orphan", "replace_from_current_support", "delete_for_rebuild"]
    replacement_binding: CanonicalEntityBinding | None = None
    expected_incident_links: tuple[ExactGraphLink, ...] = ()
    replacement_candidate: CandidateNodeReplacement | None = None
    replacement_created_at: datetime | None = None
    rebuild_mapping_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        """Require exact IDs/state and a stable aware creation time only for absent-node replacement."""
        if (not _UUID.fullmatch(self.graph_entity_uuid)
                or self.action not in {"retain", "delete_orphan", "replace_from_current_support", "delete_for_rebuild"}
                or (self.expected_current_state_fingerprint is not None
                    and not re.fullmatch(r"[0-9a-f]{64}", self.expected_current_state_fingerprint))
                or (int(self.replacement_binding is not None) + int(self.replacement_candidate is not None)
                    != (1 if self.action == "replace_from_current_support" else 0))
                or (self.replacement_binding is not None
                    and (self.replacement_binding.graph_entity_uuid != self.graph_entity_uuid
                         or self.replacement_binding.node_name is None))
                or (self.replacement_candidate is not None
                    and self.replacement_candidate.graph_entity_uuid != self.graph_entity_uuid)
                or (self.replacement_created_at is not None and (
                    not isinstance(self.replacement_created_at, datetime)
                    or self.action != "replace_from_current_support"
                    or self.replacement_created_at.tzinfo is None
                    or self.replacement_created_at.utcoffset() is None
                ))):
            raise ValueError("Canonical node recovery action is invalid")
        _validate_rebuild_mapping_ids(self.action, self.rebuild_mapping_ids)
        if (len(self.expected_incident_links) > MAX_SUPPORT
                or len({item.edge_id for item in self.expected_incident_links}) != len(self.expected_incident_links)
                or tuple(sorted(self.expected_incident_links, key=lambda item: item.edge_id))
                != self.expected_incident_links):
            raise ValueError("Canonical node recovery link inventory exceeds its bounds")


@dataclass(frozen=True)
class CandidateNodeReplacement:
    """Carry an owner-proved replacement for an extracted candidate without canonical identity."""

    graph_entity_uuid: str
    group_id: str
    node_name: str
    node_summary: str
    support_episode_ids: tuple[str, ...]
    field_support: tuple[tuple[str, str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        """Bound fresh candidate fields and require hashes tied to surviving support IDs."""
        hashes = {field: (digest, support) for field, digest, support in self.field_support}
        if (not _UUID.fullmatch(self.graph_entity_uuid) or not self.group_id
                or len(self.group_id.encode()) > 200 or not self.node_name.strip()
                or len(self.node_name.encode()) > MAX_INPUT_BYTES
                or len(self.node_summary.encode()) > MAX_INPUT_BYTES
                or not self.support_episode_ids or len(self.support_episode_ids) > MAX_SUPPORT
                or tuple(sorted(set(self.support_episode_ids))) != self.support_episode_ids
                or any(not _UUID.fullmatch(value) for value in self.support_episode_ids)
                or not self.field_support or len(self.field_support) > MAX_SUPPORT
                or len(hashes) != len(self.field_support)
                or any(not field or len(field) > 64 or not re.fullmatch(r"[0-9a-f]{64}", digest)
                       or not support or len(support) > MAX_SUPPORT
                       or tuple(sorted(set(support))) != support
                       or not set(support) <= set(self.support_episode_ids)
                       for field, digest, support in self.field_support)
                or set(self.support_episode_ids) != {
                    value for _, _, support in self.field_support for value in support
                }
                or hashes.get("name", (None, ()))[0] != hashlib.sha256(self.node_name.encode()).hexdigest()
                or hashes.get("name", (None, ()))[1] == ()
                or (self.node_summary and hashes.get("description", (None, ()))[0]
                    != hashlib.sha256(self.node_summary.encode()).hexdigest())):
            raise ValueError("Candidate node replacement lacks bounded surviving field support")


@dataclass(frozen=True)
class ExactGraphLink:
    """Identify one exact incident graph edge and its support without retaining edge text."""

    edge_id: str
    relationship_type: str
    source_node_id: str
    target_node_id: str
    episode_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require exact endpoints and bounded episode support for one incident edge."""
        if (not _UUID.fullmatch(self.edge_id) or not self.relationship_type
                or len(self.relationship_type) > 64
                or not _UUID.fullmatch(self.source_node_id) or not _UUID.fullmatch(self.target_node_id)
                or len(self.episode_ids) > MAX_SUPPORT
                or len(set(self.episode_ids)) != len(self.episode_ids)
                or tuple(sorted(self.episode_ids)) != self.episode_ids
                or any(not _UUID.fullmatch(value) for value in self.episode_ids)):
            raise ValueError("Exact graph link inventory is invalid")


@dataclass(frozen=True)
class CanonicalNodeRecoveryOutcome:
    """Report bounded exact-node convergence without exposing canonical seed text."""

    converged: bool
    retained_ids: tuple[str, ...]
    deleted_ids: tuple[str, ...]
    replaced_ids: tuple[str, ...]
    unwritten_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ExactFactState:
    """Record only exact identifiers, support and a nonreversible prior-state fingerprint."""

    fact_id: str
    source_node_id: str
    target_node_id: str
    episode_ids: tuple[str, ...]
    state_fingerprint: str
    existed: bool
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    expired_at: datetime | None = None


@dataclass(frozen=True)
class ExactFactSupport:
    """Represent intended support and validity state for one exact fact mutation."""

    fact_id: str
    episode_ids: tuple[str, ...]
    source_node_id: str = ""
    target_node_id: str = ""
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    expired_at: datetime | None = None
    state_fingerprint: str = ""


@dataclass(frozen=True)
class ExactFactReplacement:
    """Carry a fresh owner-approved fact snapshot in memory; reserved graph properties cannot be attributes."""

    fact_id: str
    group_id: str
    source_node_id: str
    target_node_id: str
    name: str
    fact: str
    episode_ids: tuple[str, ...]
    created_at: datetime
    reference_time: datetime
    valid_at: datetime | None
    invalid_at: datetime | None
    expired_at: datetime | None
    fact_embedding: tuple[float, ...]
    attributes: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        """Bound IDs, fresh support, temporal metadata, reserved attribute names and the stored vector payload."""
        times = (self.created_at, self.reference_time, self.valid_at, self.invalid_at, self.expired_at)
        attrs_size = len(json.dumps(dict(self.attributes), sort_keys=True, default=str).encode())
        reserved_attributes = {
            "source_uuid", "target_uuid", "source_node_uuid", "target_node_uuid", "uuid",
            "name", "fact", "group_id", "episodes", "created_at", "reference_time",
            "valid_at", "invalid_at", "expired_at", "fact_embedding",
        }
        if (not _UUID.fullmatch(self.fact_id) or not _UUID.fullmatch(self.source_node_id)
                or not _UUID.fullmatch(self.target_node_id) or not self.group_id
                or len(self.group_id.encode()) > 200 or not self.name.strip()
                or len(self.name.encode()) > 256 or not self.fact.strip()
                or len(self.fact.encode()) > MAX_INPUT_BYTES or not self.episode_ids
                or len(self.episode_ids) > MAX_SUPPORT or len(set(self.episode_ids)) != len(self.episode_ids)
                or tuple(sorted(self.episode_ids)) != self.episode_ids
                or any(not _UUID.fullmatch(value) for value in self.episode_ids)
                or any(value is not None and value.tzinfo is None for value in times)
                or not self.fact_embedding or len(self.fact_embedding) > 4096
                or any(isinstance(value, bool) or not isinstance(value, int | float)
                       or not math.isfinite(value) for value in self.fact_embedding)
                or len({key for key, _ in self.attributes}) != len(self.attributes)
                or any(not key or len(key.encode()) > 128 for key, _ in self.attributes)
                or any(key in reserved_attributes for key, _ in self.attributes)
                or attrs_size > MAX_INPUT_BYTES):
            raise ValueError("Exact fact replacement exceeds its owner bounds")


@dataclass(frozen=True)
class ExactFactRecoveryAction:
    """Authorize one exact fact correction from current surviving evidence.

    delete_for_rebuild carries sorted bounded dependent mapping IDs whose durable
    rebuild schedules must be committed by the owner before exact fact deletion.
    Replacement preserves the original endpoints and uses a fresh full snapshot.
    """

    fact_id: str
    source_node_id: str
    target_node_id: str
    expected_current_state_fingerprint: str
    action: Literal["replace_from_current_support", "delete_unsupported", "delete_for_rebuild"]
    replacement: ExactFactReplacement | None = None
    rebuild_mapping_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        """Bind the action to exact endpoints and require a complete fresh snapshot only for replacement."""
        if (not _UUID.fullmatch(self.fact_id) or not _UUID.fullmatch(self.source_node_id)
                or not _UUID.fullmatch(self.target_node_id)
                or not re.fullmatch(r"[0-9a-f]{64}", self.expected_current_state_fingerprint)
                or self.action not in {"replace_from_current_support", "delete_unsupported", "delete_for_rebuild"}
                or (self.action == "replace_from_current_support") != (self.replacement is not None)
                or (self.replacement is not None and (
                    self.replacement.fact_id != self.fact_id
                    or self.replacement.source_node_id != self.source_node_id
                    or self.replacement.target_node_id != self.target_node_id
                ))):
            raise ValueError("Exact fact recovery action is invalid")
        _validate_rebuild_mapping_ids(self.action, self.rebuild_mapping_ids)


def _validate_rebuild_mapping_ids(action: str, mapping_ids: tuple[UUID, ...]) -> None:
    """Require exact dependent mappings only for deletion with durable rebuild scheduling.

    IDs are a typed obligation, not consent: owner recovery callbacks must prove
    complete incident/support closure and commit each dependent rebuild before send.
    """
    if (len(mapping_ids) > MAX_SUPPORT or any(not isinstance(item, UUID) for item in mapping_ids)
            or tuple(sorted(set(mapping_ids), key=str)) != mapping_ids
            or (action == "delete_for_rebuild") != bool(mapping_ids)):
        raise ValueError("Rebuild deletion requires bounded exact dependent mappings")


@dataclass(frozen=True)
class ExactFactRecoveryOutcome:
    """Report exact fact convergence without exposing persisted fact content."""

    converged: bool
    replaced_ids: tuple[str, ...]
    deleted_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class GraphWriteReceipt:
    """Carry bounded exact IDs and fingerprints for T3 journaling without seed text.

    rebuild_absence_observed records this operation's actual absent-object readback
    after the owner proves a separate completed deletion and dependent rebuild
    obligation. It never relabels another operation's receipt or waives a read.
    """

    operation_id: UUID
    lease_token: UUID
    episode_id: UUID
    group_id: str
    mapping_revision: int
    desired_support_digest: str
    phase: Literal[
        "canonical_node_write_intent", "shell_write_intent", "bulk_ids_intent",
        "bulk_write_intent", "cleanup_write_intent",
    ]
    intended_entity_state_fingerprints: tuple[tuple[str, str | None], ...] = ()
    prior_entity_state_fingerprints: tuple[tuple[str, bool, str | None], ...] = ()
    episode_state_fingerprint: str | None = None
    prior_episode_state_fingerprint: str | None = None
    entity_ids: tuple[str, ...] = ()
    mention_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    existing_fact_states: tuple[ExactFactState, ...] = ()
    intended_fact_support: tuple[ExactFactSupport, ...] = ()
    canonical_bindings: tuple[CanonicalEntityBinding, ...] = ()
    incident_links: tuple[ExactGraphLink, ...] = ()
    candidate_field_support: tuple[tuple[str, str, str, tuple[str, ...]], ...] = ()
    rebuild_absence_observed: Literal["node", "fact"] | None = None

    def __post_init__(self) -> None:
        """Require bounded exact IDs and one before/after state pair for every planned fact write.

        Rule families raise distinct ``ValueError`` messages: identifier bounds,
        candidate field support, fact completeness, per-phase state requirements,
        rebuild-absence shape, cleanup anchors and SHA256 episode fingerprints.
        """
        self._check_identifier_bounds()
        self._check_candidate_support()
        self._check_phase_requirements()
        self._check_rebuild_absence()
        if self.phase == "cleanup_write_intent" and not self.prior_episode_state_fingerprint and not (
            self.entity_ids
            and {item[0] for item in self.prior_entity_state_fingerprints} == set(self.entity_ids)
            and {item[0] for item in self.intended_entity_state_fingerprints} == set(self.entity_ids)
        ) and self.rebuild_absence_observed != "fact":
            raise ValueError("Cleanup receipt requires prior episode or complete node states")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in (
            self.episode_state_fingerprint, self.prior_episode_state_fingerprint,
        ) if value is not None):
            raise ValueError("Episode state fingerprints must be SHA256 digests")

    def _check_identifier_bounds(self) -> None:
        """Reject malformed, duplicated, unsorted or over-bound ids, fingerprints, facts and bindings."""
        error = "Graph receipt identifiers exceed their bounds"
        if (not isinstance(self.operation_id, UUID) or not isinstance(self.lease_token, UUID)
                or not _UUID.fullmatch(str(self.episode_id)) or not self.group_id
                or len(self.group_id.encode()) > 200 or self.mapping_revision < 0
                or len(self.entity_ids) > MAX_SUPPORT or len(self.mention_ids) > MAX_SUPPORT
                or len(self.fact_ids) > MAX_SUPPORT
                or len(self.intended_entity_state_fingerprints) > MAX_SUPPORT
                or len(self.prior_entity_state_fingerprints) > MAX_SUPPORT
                or len(self.canonical_bindings) > MAX_SUPPORT
                or len(self.candidate_field_support) > MAX_SUPPORT
                or len(self.incident_links) > MAX_SUPPORT
                or len({item.edge_id for item in self.incident_links}) != len(self.incident_links)
                or tuple(sorted(self.incident_links, key=lambda item: item.edge_id)) != self.incident_links
                or len(set(self.entity_ids)) != len(self.entity_ids)
                or len(set(self.mention_ids)) != len(self.mention_ids)
                or len(set(self.fact_ids)) != len(self.fact_ids)
                or len({*self.entity_ids, *self.mention_ids, *self.fact_ids,
                        *(item.edge_id for item in self.incident_links)}) > MAX_SUPPORT):
            raise ValueError(error)
        if (self._entity_fingerprints_invalid()
                or len({item.fact_id for item in self.existing_fact_states}) != len(self.existing_fact_states)
                or len({item.fact_id for item in self.intended_fact_support}) != len(self.intended_fact_support)
                or not re.fullmatch(r"[0-9a-f]{64}", self.desired_support_digest)
                or any(not _UUID.fullmatch(value) for value in (*self.entity_ids, *self.mention_ids, *self.fact_ids))
                or any(_fact_record_invalid(item) for item in self.existing_fact_states)
                or any(_fact_record_invalid(item) for item in self.intended_fact_support)
                or any(_binding_outside_receipt(item, self.group_id, self.mapping_revision)
                       for item in self.canonical_bindings)):
            raise ValueError(error)

    def _entity_fingerprints_invalid(self) -> bool:
        """Report duplicated or malformed intended/prior node fingerprint entries."""
        intended, prior = self.intended_entity_state_fingerprints, self.prior_entity_state_fingerprints
        return bool(
            len({item[0] for item in intended}) != len(intended)
            or any(not _UUID.fullmatch(item[0]) or (item[1] is not None
                   and not re.fullmatch(r"[0-9a-f]{64}", item[1]))
                   for item in intended)
            or len({item[0] for item in prior}) != len(prior)
            or any(not _UUID.fullmatch(item[0]) or not isinstance(item[1], bool)
                   or (item[1] != (item[2] is not None))
                   or (item[2] is not None and not re.fullmatch(r"[0-9a-f]{64}", item[2]))
                   for item in prior)
        )

    def _check_candidate_support(self) -> None:
        """Require well-formed candidate field support that names journaled nodes only."""
        if any(not _UUID.fullmatch(node_id) or not field or len(field) > 64
               or not re.fullmatch(r"[0-9a-f]{64}", digest)
               or not support_ids or len(support_ids) > MAX_SUPPORT
               or tuple(sorted(set(support_ids))) != support_ids
               or any(not _UUID.fullmatch(value) for value in support_ids)
               for node_id, field, digest, support_ids in self.candidate_field_support):
            raise ValueError("Candidate receipt field support exceeds its bounds")
        if any(node_id not in self.entity_ids for node_id, _, _, _ in self.candidate_field_support):
            raise ValueError("Candidate receipt proof must identify a journaled node")

    def _check_phase_requirements(self) -> None:
        """Require the state completeness each receipt phase promises to its recovery readers."""
        if self.phase != "shell_write_intent" and (
            {item.fact_id for item in self.existing_fact_states} != set(self.fact_ids)
            or {item.fact_id for item in self.intended_fact_support} != set(self.fact_ids)
        ):
            raise ValueError("Graph receipt fact IDs require complete prior and intended state")
        if self.phase == "bulk_ids_intent" and (
            {item[0] for item in self.prior_entity_state_fingerprints} != set(self.entity_ids)
            or self.intended_entity_state_fingerprints
        ):
            raise ValueError("Bulk ID receipt requires exact prior node states without asserting hydration")
        if self.phase == "shell_write_intent" and not self.episode_state_fingerprint:
            raise ValueError("Shell receipt requires its exact intended episode fingerprint")
        if self.phase == "canonical_node_write_intent" and (
            not self.entity_ids
            or {item[0] for item in self.intended_entity_state_fingerprints} != set(self.entity_ids)
            or {item[0] for item in self.prior_entity_state_fingerprints} != set(self.entity_ids)
            or any(item.graph_entity_uuid not in self.entity_ids for item in self.canonical_bindings)
            or any(not _UUID.fullmatch(item[0]) or item[1] is None
                   or not re.fullmatch(r"[0-9a-f]{64}", item[1])
                   for item in self.intended_entity_state_fingerprints)
            or any(not _UUID.fullmatch(item[0])
                   or (item[1] != (item[2] is not None))
                   or (item[2] is not None and not re.fullmatch(r"[0-9a-f]{64}", item[2]))
                   for item in self.prior_entity_state_fingerprints)
        ):
            raise ValueError("Canonical node receipt requires seeded exact entity IDs")
        if self.phase == "bulk_write_intent" and (
            not self.episode_state_fingerprint or not self.prior_episode_state_fingerprint
            or {item[0] for item in self.prior_entity_state_fingerprints} != set(self.entity_ids)
            or {item[0] for item in self.intended_entity_state_fingerprints} != set(self.entity_ids)
        ):
            raise ValueError("Bulk receipt requires exact prior and intended node and episode states")

    def _check_rebuild_absence(self) -> None:
        """Require a rebuild-absence marker to describe a real single-object current absence."""
        if self.rebuild_absence_observed is None:
            return
        if (self.rebuild_absence_observed not in {"node", "fact"}
            or self.phase != "cleanup_write_intent"
            or self.incident_links or self.mention_ids
            or (self.rebuild_absence_observed == "node" and (
                len(self.entity_ids) != 1 or self.fact_ids
                or self.prior_entity_state_fingerprints != ((self.entity_ids[0], False, None),)
                or self.intended_entity_state_fingerprints != ((self.entity_ids[0], None),)
            ))
            or (self.rebuild_absence_observed == "fact" and (
                len(self.fact_ids) != 1 or self.entity_ids
                or len(self.existing_fact_states) != 1 or len(self.intended_fact_support) != 1
                or (self.existing_fact_states[0].source_node_id, self.existing_fact_states[0].target_node_id)
                != (self.intended_fact_support[0].source_node_id, self.intended_fact_support[0].target_node_id)
                or any(item.existed or item.episode_ids or item.state_fingerprint != _ABSENT_FACT_FINGERPRINT
                       for item in self.existing_fact_states)
                or any(item.episode_ids or item.state_fingerprint != _ABSENT_FACT_FINGERPRINT
                       for item in self.intended_fact_support)
            ))):
            raise ValueError("Rebuild absence receipt must describe actual current exact absence")


def _fact_record_invalid(item: ExactFactState | ExactFactSupport) -> bool:
    """Report a fact record with malformed ids, over-bound or malformed support, or a non-SHA256 fingerprint."""
    return bool(
        not _UUID.fullmatch(item.fact_id)
        or not _UUID.fullmatch(item.source_node_id)
        or not _UUID.fullmatch(item.target_node_id)
        or len(item.episode_ids) > MAX_SUPPORT
        or any(not _UUID.fullmatch(value) for value in item.episode_ids)
        or not re.fullmatch(r"[0-9a-f]{64}", item.state_fingerprint)
    )


def _binding_outside_receipt(item: CanonicalEntityBinding, group_id: str, mapping_revision: int) -> bool:
    """Report a canonical binding foreign to the receipt's group/revision or with malformed seed fields."""
    return bool(
        item.group_id != group_id or item.mapping_revision != mapping_revision
        or item.canonical_revision < 1 or not item.membership_ids or not item.evidence
        or not _UUID.fullmatch(item.graph_entity_uuid)
        or (item.node_name is not None and (not item.node_name.strip() or len(item.node_name.encode()) > 4096))
        or (item.node_summary is not None and len(item.node_summary.encode()) > MAX_INPUT_BYTES)
        or any(not field or not re.fullmatch(r"[0-9a-f]{64}", value_hash)
               or not support_ids or not set(support_ids) <= set(item.membership_ids)
               for field, value_hash, support_ids in item.field_support)
    )


@dataclass(frozen=True)
class GraphReceiptInspection:
    """Report exact-ID presence and node, episode and fact fingerprints without graph text."""

    episode_present: bool
    episode_state_fingerprint: str | None
    entity_ids_present: tuple[str, ...]
    current_entity_state_fingerprints: tuple[tuple[str, str], ...]
    incident_links: tuple[ExactGraphLink, ...]
    mention_ids_present: tuple[str, ...]
    current_mention_links: tuple[ExactGraphLink, ...]
    fact_ids_present: tuple[str, ...]
    current_fact_states: tuple[ExactFactState, ...]
    current_fact_recovery_fingerprints: tuple[tuple[str, str], ...]


GraphWriteState = Literal["intent", "succeeded", "unknown"]


@dataclass(frozen=True)
class GraphModelPolicy:
    """Bind a gateway role to one resolved model and current capability policy."""

    role: str
    mapping: ModelMapping
    policy: RequestPolicy
    dimensions: int | None = None


@dataclass(frozen=True)
class TemporalSearchResult:
    """Represent a graph result only after canonical evidence validation."""

    fact: str
    valid_from: datetime | None
    valid_to: datetime | None
    observed_at: datetime
    episode_ids: tuple[str, ...]
    entity_ids: tuple[UUID, ...]
    evidence: tuple[EvidenceIdentity, ...]
    confidence: float | None = None


@dataclass(frozen=True)
class DispatchOwnership:
    """Durable identity of one journaled graph dispatch scope.

    server_run_id is a random 40-hex nonce and client_id a random positive 63-bit
    integer, both minted by the adapter (Neo4j exposes no comparable server-side
    identity), so no database migration is needed. Together they form the token
    stamped into every transaction's metadata; cessation finds and terminates
    exactly the transactions carrying it. A forged or reused token cannot match
    another operation because the nonce is unguessable. Persist before any graph
    command is sent.
    """

    operation_id: UUID
    group_id: str
    server_run_id: str
    client_id: int

    def __post_init__(self) -> None:
        """Reject unbounded graph identity and malformed nonce/client IDs."""
        if (not isinstance(self.operation_id, UUID) or not self.group_id
                or len(self.group_id.encode()) > 200
                or not re.fullmatch(r"[0-9a-f]{40}", self.server_run_id)
                or not isinstance(self.client_id, int) or isinstance(self.client_id, bool)
                or not 0 < self.client_id < 2**63):
            raise ValueError("Invalid graph dispatch ownership")


@dataclass(frozen=True)
class RecoveryReceiptAggregate:
    """Bind bounded actual witnesses to the complete immutable journal prefix.

    Owner certifies paged journal digest, exact witness sequence membership and
    first-prior/current-matching intermediate/latest and shared/stale obligations. IDs describe its
    complete effects, never a selected page. Receipts remain original immutable
    values; no synthetic phases or rewritten state fingerprints are authorized.
    """

    operation_id: UUID
    group_id: str
    lease_token: UUID
    mapping_revision: int
    ledger_count: int
    ledger_digest: str
    witness_sequences: tuple[int, ...]
    entity_ids: tuple[str, ...]
    mention_ids: tuple[str, ...]
    fact_ids: tuple[str, ...]
    incident_link_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject incomplete prefix identity, unordered witnesses and over-bound effects."""
        collections = (self.entity_ids, self.mention_ids, self.fact_ids, self.incident_link_ids)
        if (not isinstance(self.operation_id, UUID) or not isinstance(self.lease_token, UUID)
                or not self.group_id or len(self.group_id.encode()) > 200
                or self.mapping_revision < 0 or not isinstance(self.ledger_count, int)
                or isinstance(self.ledger_count, bool) or not 0 < self.ledger_count < 2**63
                or not re.fullmatch(r"[0-9a-f]{64}", self.ledger_digest)
                or not 1 <= len(self.witness_sequences) <= MAX_RECOVERY_WITNESSES
                or tuple(sorted(set(self.witness_sequences))) != self.witness_sequences
                or any(isinstance(item, bool) or not isinstance(item, int)
                       or not 0 <= item < self.ledger_count for item in self.witness_sequences)
                or any(tuple(sorted(set(items))) != items
                       or any(not _UUID.fullmatch(item) for item in items) for items in collections)
                or len(set().union(*collections)) > MAX_SUPPORT):
            raise ValueError("Invalid complete graph receipt aggregate")


@dataclass(frozen=True)
class OperationAuthorization:
    """Carry bounded support, a held owner fence, and fresh per-attempt checks.

    Construct this value through sources/documents/entities/relationships public
    contracts. The caller holds source then sorted document locks for the entire
    operation. partition_episode_ids is the fully paginated inventory for this
    bounded group_id/source-generation partition (at most 100); history_episode_ids
    is only the selected subset for a request (at most 10). authorize rereads current
    gateway policy, source generation, and exact support before each model request.
    validate_partition proves all episode support is complete and ready, with no
    unresolved deletion or partial-write state. Unknown state blocks model egress.
    A graph partition is never permission. record_write_intent stores every pre-mutation exact-ID
    ordered receipts under operation_id and lease_token; receipt_history is the bounded
    complete sequence for cleanup. mapping_revision and canonical_bindings
    identify owner-resolved endpoints only. The reconcile validation operation authorizes
    exact receipt inspection under the same source/document fence without enabling model egress.
    Canonical-node recovery additionally requires authorize_node_recovery to prove the live
    recovery lease, prior-dispatch cessation, exact receipt aggregate and per-node decision.
    Exact-fact correction uses authorize_fact_recovery for the same current owner support,
    lease, cessation and per-fact decision before inspection and before mutation.
    Delete retries may also carry bounded current node/fact actions; those detached
    values are authorized again and are never copied into durable receipts as text.
    record_dispatch_ownership commits the exact synchronous transport before graph
    commands; authorize_dispatch_cessation compares that stored ownership, live
    recovery lease and stopped local tasks before terminating its tagged transactions.
    record_dispatch_completion marks only this immutable journal entry after a
    normal fully replied scope whose transactions all completed; it does
    not mark the graph operation synchronized or erase other dispatch identities.
    receipt_aggregate binds actual bounded witnesses to the complete paged journal
    prefix; authorize_receipt_aggregate certifies all proof obligations without
    waiving graph comparisons. Receipt/node/fact action counts have independent
    bounds, while their complete physical effect union still shares the 100-ID cap.
    authorize_rebuild_absence proves the durable deleting-operation dependency,
    original exact cleanup witness, completed dispatch and current recovery lease
    after graph readback has found the object absent. Its proof chain is preserved
    by the owner alongside the new current-operation absence receipt.
    """

    group_id: str
    source_id: UUID
    source_generation: int
    operation_id: UUID
    lease_token: UUID
    evidence: tuple[EvidenceIdentity, ...]
    fence: Callable[[], AbstractAsyncContextManager[None]]
    authorize: Callable[[Literal["reasoning", "embedding"]], Awaitable[None]]
    canonicalize: Callable[[Sequence[Any]], Awaitable[list[TemporalSearchResult]]]
    validate_partition: Callable[[Literal["search", "upsert", "delete", "reconcile"], str | None], Awaitable[None]]
    record_write_intent: Callable[[GraphWriteReceipt], Awaitable[None]]
    reasoning: GraphModelPolicy
    small_reasoning: GraphModelPolicy
    embedding: GraphModelPolicy
    authorize_node_recovery: Callable[
        [tuple[GraphWriteReceipt, ...], tuple[CanonicalNodeRecoveryAction, ...]], Awaitable[None],
    ] | None = None
    authorize_fact_recovery: Callable[
        [tuple[GraphWriteReceipt, ...], tuple[ExactFactRecoveryAction, ...]], Awaitable[None],
    ] | None = None
    canonical_bindings: tuple[CanonicalEntityBinding, ...] = ()
    mapping_revision: int = 0
    episode_receipt: GraphWriteReceipt | None = None
    reranking: GraphModelPolicy | None = None
    partition_episode_ids: tuple[str, ...] = ()
    history_episode_ids: tuple[str, ...] = ()
    partition_inventory_complete: bool = False
    model_calls: list[int] | None = None
    receipt_history: tuple[GraphWriteReceipt, ...] = ()
    node_recovery_actions: tuple[CanonicalNodeRecoveryAction, ...] = ()
    fact_recovery_actions: tuple[ExactFactRecoveryAction, ...] = ()
    record_dispatch_ownership: Callable[[DispatchOwnership], Awaitable[None]] | None = None
    authorize_dispatch_cessation: Callable[[DispatchOwnership], Awaitable[None]] | None = None
    record_dispatch_completion: Callable[[DispatchOwnership], Awaitable[None]] | None = None
    authorize_rebuild_absence: Callable[[Literal["node", "fact"], str], Awaitable[None]] | None = None
    receipt_aggregate: RecoveryReceiptAggregate | None = None
    authorize_receipt_aggregate: Callable[
        [RecoveryReceiptAggregate, tuple[GraphWriteReceipt, ...], Literal["node", "fact", "delete"]],
        Awaitable[None],
    ] | None = None

    def __post_init__(self) -> None:
        """Reject incomplete support, invalid receipts, and cross-source authorization closures."""
        if (not self.group_id or len(self.group_id.encode()) > 200
                or not isinstance(self.operation_id, UUID) or not isinstance(self.lease_token, UUID)
                or self.source_generation < 1 or self.mapping_revision < 0
                or len(self.evidence) > MAX_SUPPORT
                or len(self.partition_episode_ids) > MAX_SUPPORT
                or len(self.history_episode_ids) > MAX_HISTORY
                or len(self.receipt_history) > MAX_RECOVERY_WITNESSES
                or len(self.node_recovery_actions) > MAX_SUPPORT
                or len(self.fact_recovery_actions) > MAX_SUPPORT
                or not self.partition_inventory_complete):
            raise ValueError("Graph authorization closure exceeds its bounds")
        refs = {(item.document_version_id, item.chunk_id) for item in self.evidence}
        if len(refs) != len(self.evidence) or any(
            item.source_id != self.source_id or item.source_generation != self.source_generation
            for item in self.evidence
        ):
            raise ValueError("Graph evidence must belong to one fenced source generation")
        if (len(set(self.partition_episode_ids)) != len(self.partition_episode_ids)
                or len(set(self.history_episode_ids)) != len(self.history_episode_ids)
                or any(not _UUID.fullmatch(item) for item in self.partition_episode_ids)
                or any(not _UUID.fullmatch(item) for item in self.history_episode_ids)
                or not set(self.history_episode_ids) <= set(self.partition_episode_ids)):
            raise ValueError("Graph episode inventory or selected history is invalid")
        if any(item.group_id != self.group_id or item.mapping_revision != self.mapping_revision
               or item.canonical_revision < 1 or not item.membership_ids or not item.evidence
               or len(set(item.membership_ids)) != len(item.membership_ids)
               or not _UUID.fullmatch(item.graph_entity_uuid)
               or (item.node_name is not None and (not item.node_name.strip() or len(item.node_name.encode()) > 4096))
               or (item.node_summary is not None and len(item.node_summary.encode()) > MAX_INPUT_BYTES)
               or any(not field or not re.fullmatch(r"[0-9a-f]{64}", value_hash)
                      or not support_ids or not set(support_ids) <= set(item.membership_ids)
                      for field, value_hash, support_ids in item.field_support)
               or not set(item.evidence) <= set(self.evidence) for item in self.canonical_bindings):
            raise ValueError("Canonical graph bindings must belong to this authorization closure")
        if self.episode_receipt is not None and (
            self.episode_receipt.group_id != self.group_id
            or str(self.episode_receipt.episode_id) not in self.partition_episode_ids
        ):
            raise ValueError("Episode receipt must belong to the authorized graph partition")
        if any(item.group_id != self.group_id or item.operation_id != self.operation_id
               or item.lease_token != self.lease_token or item.mapping_revision != self.mapping_revision
               or str(item.episode_id) not in self.partition_episode_ids
               for item in self.receipt_history):
            raise ValueError("Graph receipt history must belong to this operation and partition")
        if (tuple(sorted(self.node_recovery_actions, key=lambda item: item.graph_entity_uuid))
                != self.node_recovery_actions
                or len({item.graph_entity_uuid for item in self.node_recovery_actions})
                != len(self.node_recovery_actions)
                or tuple(sorted(self.fact_recovery_actions, key=lambda item: item.fact_id))
                != self.fact_recovery_actions
                or len({item.fact_id for item in self.fact_recovery_actions})
                != len(self.fact_recovery_actions)
                or (self.node_recovery_actions and self.authorize_node_recovery is None)
                or (self.fact_recovery_actions and self.authorize_fact_recovery is None)):
            raise ValueError("Current graph recovery actions require bounded ordered owner authorization")
        object.__setattr__(self, "model_calls", [0])


async def _authorize_receipt_witnesses(
    context: OperationAuthorization, receipts: tuple[GraphWriteReceipt, ...],
    purpose: Literal["node", "fact", "delete"],
) -> RecoveryReceiptAggregate | None:
    """Verify exact full effect inventory before using owner-selected journal witnesses.

    The owner rereads the complete paged prefix under its operation lease, proves
    digest/membership and all required state obligations. This does not waive any
    adapter graph fingerprint/support/incident comparison. New prewrite receipts
    are appended and authorized separately through existing owner callbacks.
    """
    aggregate = context.receipt_aggregate
    if aggregate is None:
        return None
    if (context.authorize_receipt_aggregate is None
            or aggregate.operation_id != context.operation_id
            or aggregate.group_id != context.group_id
            or aggregate.lease_token != context.lease_token
            or aggregate.mapping_revision != context.mapping_revision
            or len(receipts) != len(aggregate.witness_sequences)
            or tuple(sorted({value for item in receipts for value in item.entity_ids})) != aggregate.entity_ids
            or tuple(sorted({value for item in receipts for value in item.mention_ids})) != aggregate.mention_ids
            or tuple(sorted({value for item in receipts for value in item.fact_ids})) != aggregate.fact_ids
            or tuple(sorted({link.edge_id for item in receipts for link in item.incident_links}))
               != aggregate.incident_link_ids):
        raise GraphOperationError("graph_receipt_aggregate_incomplete")
    await context.authorize_receipt_aggregate(aggregate, receipts, purpose)
    return aggregate


@dataclass(frozen=True)
class EpisodeRequest:
    """Describe a stable episode whose reference_time anchors extraction, not canonical validity."""

    episode_id: UUID
    group_id: str
    name: str
    content: str
    reference_time: datetime
    evidence: tuple[EvidenceIdentity, ...]
    canonical_entity_ids: tuple[UUID, ...]
    mapping_revision: int
    canonical_bindings: tuple[CanonicalEntityBinding, ...] = ()

    def __post_init__(self) -> None:
        """Reject invalid IDs, naive times, unbounded provenance, or unproven canonical mappings."""
        if (not _UUID.fullmatch(str(self.episode_id)) or not self.group_id
                or len(self.group_id.encode()) > 200 or not self.name.strip()
                or len(self.content.encode("utf-8")) > MAX_INPUT_BYTES
                or self.reference_time.tzinfo is None or self.mapping_revision < 1
                or len(self.evidence) > MAX_SUPPORT
                or len(set(self.canonical_entity_ids)) != len(self.canonical_entity_ids)):
            raise ValueError("Graph episode is invalid or exceeds its bounds")
        binding_ids = {item.canonical_entity_id for item in self.canonical_bindings}
        if (len(binding_ids) != len(self.canonical_bindings)
                or binding_ids != set(self.canonical_entity_ids)
                or any(item.group_id != self.group_id or item.mapping_revision != self.mapping_revision
                       or item.canonical_revision < 1 or not item.membership_ids or not item.evidence
                       or len(set(item.membership_ids)) != len(item.membership_ids)
                       or not _UUID.fullmatch(item.graph_entity_uuid)
                       or not set(item.evidence) <= set(self.evidence)
                       for item in self.canonical_bindings)):
            raise ValueError("Graph entity bindings must prove requested canonical endpoints")


@dataclass(frozen=True)
class DeleteOutcome:
    """Separate episode removal from pending fact recomputation and node recovery IDs."""

    episode_id: str
    removed: bool
    affected_edge_ids: tuple[str, ...]
    shared_edge_ids: tuple[str, ...]
    outcome: Literal["succeeded", "unknown"]
    stale_edge_ids: tuple[str, ...] = ()
    node_recovery_required_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphConfiguration:
    """Separate optional graph credentials from the queue Redis connection."""

    enabled: bool = False
    host: str | None = None
    port: int = 7687
    username: str | None = None
    password: str | None = None
    database: str = "neo4j"

    @classmethod
    def from_settings(cls, settings: Settings) -> GraphConfiguration:
        """Resolve the optional dedicated graph connection from validated deployment settings."""
        return cls(
            enabled=settings.graph_enabled,
            host=settings.graph_host,
            port=settings.graph_port,
            username=settings.graph_username,
            password=settings.graph_password.get_secret_value() or None,
            database=settings.graph_database,
        )

