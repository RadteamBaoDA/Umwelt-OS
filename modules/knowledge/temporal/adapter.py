"""Bounded Graphiti adapter; PostgreSQL remains the canonical evidence authority."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import logging
import math
import os
import re
import struct
from functools import partial
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from core.model_gateway.client import ModelGateway, ModelGatewayError
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.config import Settings

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
_dispatch_transport: ContextVar[Any | None] = ContextVar("temporal_dispatch_transport", default=None)


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
        """Require bounded exact IDs and one before/after state pair for every planned fact write."""
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
                or len(set((*self.entity_ids, *self.mention_ids, *self.fact_ids,
                            *(item.edge_id for item in self.incident_links)))) > MAX_SUPPORT
                or len({item[0] for item in self.intended_entity_state_fingerprints})
                != len(self.intended_entity_state_fingerprints)
                or any(not _UUID.fullmatch(item[0]) or (item[1] is not None
                       and not re.fullmatch(r"[0-9a-f]{64}", item[1]))
                       for item in self.intended_entity_state_fingerprints)
                or len({item[0] for item in self.prior_entity_state_fingerprints})
                != len(self.prior_entity_state_fingerprints)
                or any(not _UUID.fullmatch(item[0]) or not isinstance(item[1], bool)
                       or (item[1] != (item[2] is not None))
                       or (item[2] is not None and not re.fullmatch(r"[0-9a-f]{64}", item[2]))
                       for item in self.prior_entity_state_fingerprints)
                or len({item.fact_id for item in self.existing_fact_states}) != len(self.existing_fact_states)
                or len({item.fact_id for item in self.intended_fact_support}) != len(self.intended_fact_support)
                or not re.fullmatch(r"[0-9a-f]{64}", self.desired_support_digest)
                or any(not _UUID.fullmatch(value) for value in (*self.entity_ids, *self.mention_ids, *self.fact_ids))
                or any(not _UUID.fullmatch(item.fact_id)
                       or not _UUID.fullmatch(item.source_node_id)
                       or not _UUID.fullmatch(item.target_node_id)
                       or len(item.episode_ids) > MAX_SUPPORT
                       or any(not _UUID.fullmatch(value) for value in item.episode_ids)
                       or not re.fullmatch(r"[0-9a-f]{64}", item.state_fingerprint)
                       for item in self.existing_fact_states)
                or any(not _UUID.fullmatch(item.fact_id)
                       or not _UUID.fullmatch(item.source_node_id)
                       or not _UUID.fullmatch(item.target_node_id)
                       or len(item.episode_ids) > MAX_SUPPORT
                       or any(not _UUID.fullmatch(value) for value in item.episode_ids)
                       or not re.fullmatch(r"[0-9a-f]{64}", item.state_fingerprint)
                       for item in self.intended_fact_support)
                or any(item.group_id != self.group_id or item.mapping_revision != self.mapping_revision
                       or item.canonical_revision < 1 or not item.membership_ids or not item.evidence
                       or not _UUID.fullmatch(item.graph_entity_uuid)
                       or (item.node_name is not None and (not item.node_name.strip() or len(item.node_name.encode()) > 4096))
                       or (item.node_summary is not None and len(item.node_summary.encode()) > MAX_INPUT_BYTES)
                       or any(not field or not re.fullmatch(r"[0-9a-f]{64}", value_hash)
                              or not support_ids or not set(support_ids) <= set(item.membership_ids)
                              for field, value_hash, support_ids in item.field_support)
                       for item in self.canonical_bindings)):
            raise ValueError("Graph receipt identifiers exceed their bounds")
        if any(not _UUID.fullmatch(node_id) or not field or len(field) > 64
               or not re.fullmatch(r"[0-9a-f]{64}", digest)
               or not support_ids or len(support_ids) > MAX_SUPPORT
               or tuple(sorted(set(support_ids))) != support_ids
               or any(not _UUID.fullmatch(value) for value in support_ids)
               for node_id, field, digest, support_ids in self.candidate_field_support):
            raise ValueError("Candidate receipt field support exceeds its bounds")
        if any(node_id not in self.entity_ids for node_id, _, _, _ in self.candidate_field_support):
            raise ValueError("Candidate receipt proof must identify a journaled node")
        if self.phase != "shell_write_intent" and (
            {item.fact_id for item in self.existing_fact_states} != set(self.fact_ids)
            or {item.fact_id for item in self.intended_fact_support} != set(self.fact_ids)
        ):
            raise ValueError("Graph receipt fact IDs require complete prior and intended state")
        if self.phase == "bulk_ids_intent" and (
            set(item[0] for item in self.prior_entity_state_fingerprints) != set(self.entity_ids)
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
        if self.rebuild_absence_observed is not None and (
            self.rebuild_absence_observed not in {"node", "fact"}
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
            ))
        ):
            raise ValueError("Rebuild absence receipt must describe actual current exact absence")
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
    """Bind a gateway alias to one resolved model and current capability policy."""

    alias: str
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
    """Durable identity of one non-reconnecting synchronous graph transport.

    Pair Redis process run_id with client_id so restart cannot authorize killing
    an unrelated reused client ID. Persist before any graph command is sent.
    """

    operation_id: UUID
    group_id: str
    server_run_id: str
    client_id: int

    def __post_init__(self) -> None:
        """Reject unbounded graph identity and invalid Redis process/client IDs."""
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
    recovery lease and stopped local tasks before disconnecting its Redis client.
    record_dispatch_completion marks only this immutable journal entry after a
    normal fully replied synchronous scope and successful socket closure; it does
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
    port: int = 6379
    username: str | None = None
    password: str | None = None
    database: str = "bbd_temporal"

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


def _safe_error(exc: BaseException) -> GraphOperationError:
    """Map dependency failures to stable redacted graph error categories."""
    if "already indexed" in str(exc).lower():
        return GraphOperationError("graph_index_exists")
    return GraphOperationUnknown("graph_transport_outcome_unknown")


def _fact_state(edge: Any, dimensions: int | None) -> ExactFactState:
    """Fingerprint complete exact edge state, including its persisted float32 vector, without retaining text."""
    episode_ids = tuple(sorted(str(value) for value in edge.episodes))
    fingerprint = _fact_recovery_fingerprint(edge, dimensions)
    return ExactFactState(
        fact_id=str(edge.uuid), source_node_id=str(edge.source_node_uuid),
        target_node_id=str(edge.target_node_uuid), episode_ids=episode_ids,
        state_fingerprint=fingerprint, existed=True,
        valid_at=edge.valid_at, invalid_at=edge.invalid_at, expired_at=edge.expired_at,
    )


def _episode_state_fingerprint(episode: Any) -> str:
    """Hash exact episode shell fields and attached fact IDs without retaining source content."""
    source = getattr(episode.source, "value", episode.source)
    payload = {
        "uuid": str(episode.uuid),
        "group_id": str(episode.group_id),
        "name": episode.name,
        "source": str(source),
        "source_description": episode.source_description,
        "content": episode.content,
        "valid_at": str(episode.valid_at),
        "created_at": str(episode.created_at),
        "entity_edges": sorted(str(value) for value in episode.entity_edges),
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _entity_state_fingerprint(
    entity: Any, dimensions: int | None = None, *, writer_intended: bool = False,
) -> str:
    """Hash exact node state; only writer intents synthesize Entity, while malformed persisted labels fail closed."""
    if isinstance(entity, dict):
        properties = entity
        reserved = {"uuid", "group_id", "name", "summary", "created_at", "name_embedding", "labels"}
        attributes = {key: value for key, value in properties.items() if key not in reserved}
        labels = properties.get("labels", ())
        created_at = properties.get("created_at")
        identity = {key: properties.get(key) for key in ("uuid", "group_id", "name", "summary")}
        embedding = properties.get("name_embedding")
    else:
        attributes = getattr(entity, "attributes", {}) or {}
        labels = getattr(entity, "labels", ()) or ()
        created_at = getattr(entity, "created_at", None)
        identity = {
            "uuid": str(entity.uuid), "group_id": str(entity.group_id),
            "name": entity.name, "summary": entity.summary,
        }
        embedding = getattr(entity, "name_embedding", None)

    if not isinstance(attributes, dict) or not isinstance(labels, list | tuple | set):
        raise GraphOperationError("graph_entity_state_unsupported")
    if any(not isinstance(label, str) or not label for label in labels):
        raise GraphOperationError("graph_entity_state_unsupported")
    label_set = set(labels)
    if writer_intended:
        label_set.add("Entity")
    elif "Entity" not in label_set:
        raise GraphOperationError("graph_entity_state_unsupported")
    if created_at is not None:
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                raise GraphOperationError("graph_entity_state_unsupported")
            created_at = created_at.astimezone(UTC).isoformat()
        elif isinstance(created_at, str):
            try:
                parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                raise GraphOperationError("graph_entity_state_unsupported") from None
            if parsed.tzinfo is None:
                raise GraphOperationError("graph_entity_state_unsupported")
            created_at = parsed.astimezone(UTC).isoformat()
        else:
            raise GraphOperationError("graph_entity_state_unsupported")
    if embedding is not None:
        if (dimensions is None or not isinstance(embedding, list | tuple)
                or len(embedding) != dimensions):
            raise GraphOperationError("graph_entity_embedding_state_unsupported")
        normalized_embedding: list[float] = []
        for value in embedding:
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise GraphOperationError("graph_entity_embedding_state_unsupported")
            try:
                normalized = struct.unpack("!f", struct.pack("!f", float(value)))[0]
            except (OverflowError, struct.error):
                raise GraphOperationError("graph_entity_embedding_state_unsupported") from None
            if not math.isfinite(normalized):
                raise GraphOperationError("graph_entity_embedding_state_unsupported")
            normalized_embedding.append(normalized)
    else:
        normalized_embedding = None
    payload = {
        **identity,
        "created_at": created_at,
        # EntityNode.save always applies the mandatory Entity label on FalkorDB.
        "labels": sorted(label_set),
        "attributes": _normalize_graph_values(attributes),
        "name_embedding": normalized_embedding,
    }
    return hashlib.sha256(_canonical_json(_normalize_graph_values(payload))).hexdigest()


def _normalize_graph_values(value: Any) -> Any:
    """Project values through Falkor's UTC datetime and NUL-string send normalization, rejecting lossy objects."""
    from graphiti_core.driver.falkordb_driver import _strip_nul_bytes
    from graphiti_core.utils.datetime_utils import convert_datetimes_to_strings

    def reject_naive_datetime(item: Any) -> None:
        """Reject naive datetime leaves before the pinned serializer would assume a timezone."""
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise GraphOperationError("graph_state_datetime_unsupported")
        elif isinstance(item, dict):
            for nested in item.values():
                reject_naive_datetime(nested)
        elif isinstance(item, list | tuple):
            for nested in item:
                reject_naive_datetime(nested)

    def validate(item: Any) -> None:
        """Allow only normalized JSON primitives so fingerprints never use lossy object stringification."""
        if item is None or isinstance(item, str | bool | int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise GraphOperationError("graph_state_value_unsupported")
            return
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise GraphOperationError("graph_state_value_unsupported")
            for nested in item.values():
                validate(nested)
            return
        if isinstance(item, list | tuple):
            for nested in item:
                validate(nested)
            return
        raise GraphOperationError("graph_state_value_unsupported")

    reject_naive_datetime(value)
    normalized = _strip_nul_bytes(convert_datetimes_to_strings(value))
    validate(normalized)
    return normalized


def _normalized_fact_attributes(edge: Any) -> dict[str, Any]:
    """Validate pinned transport endpoint aliases and preserve all genuine fact attributes."""
    raw_attributes = edge.attributes or {}
    if not isinstance(raw_attributes, dict):
        raise GraphOperationError("graph_fact_attributes_unsupported")
    attributes = dict(raw_attributes)
    source_id = str(edge.source_node_uuid)
    target_id = str(edge.target_node_uuid)
    aliases = {
        "source_node_uuid": source_id, "source_uuid": source_id,
        "target_node_uuid": target_id, "target_uuid": target_id,
    }
    for key, expected in aliases.items():
        if key in attributes:
            actual = attributes[key]
            if not isinstance(actual, str | UUID) or str(actual) != expected:
                raise GraphOperationError("graph_fact_endpoint_alias_mismatch")
            attributes.pop(key)
    return _normalize_graph_values(attributes)


def _canonical_json(payload: Any) -> bytes:
    """Serialize an already normalized graph projection without stringifying unsupported values."""
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise GraphOperationError("graph_state_value_unsupported") from None


def _normalized_graph_timestamp(value: datetime | str | None) -> str | None:
    """Normalize a persisted Graphiti timestamp to one UTC ISO representation, rejecting naive input."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise GraphOperationError("graph_state_datetime_unsupported") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise GraphOperationError("graph_state_datetime_unsupported")
    return _normalize_graph_values(value)


def _fact_recovery_fingerprint(edge: Any, dimensions: int | None) -> str:
    """Hash full normalized persisted fact state, validating transport aliases and retaining domain attributes/vector."""
    vector = getattr(edge, "fact_embedding", None)
    if (dimensions is None or not isinstance(vector, list | tuple)
            or len(vector) != dimensions):
        raise GraphOperationError("graph_fact_embedding_state_unsupported")
    normalized_vector: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise GraphOperationError("graph_fact_embedding_state_unsupported")
        try:
            normalized = struct.unpack("!f", struct.pack("!f", float(value)))[0]
        except (OverflowError, struct.error):
            raise GraphOperationError("graph_fact_embedding_state_unsupported") from None
        if not math.isfinite(normalized):
            raise GraphOperationError("graph_fact_embedding_state_unsupported")
        normalized_vector.append(normalized)
    payload = {
        "uuid": str(edge.uuid), "group_id": str(edge.group_id),
        "source_node_uuid": str(edge.source_node_uuid),
        "target_node_uuid": str(edge.target_node_uuid), "name": edge.name,
        "fact": edge.fact, "episodes": sorted(str(value) for value in edge.episodes),
        "created_at": _normalized_graph_timestamp(edge.created_at),
        "reference_time": _normalized_graph_timestamp(edge.reference_time),
        "valid_at": _normalized_graph_timestamp(edge.valid_at),
        "invalid_at": _normalized_graph_timestamp(edge.invalid_at),
        "expired_at": _normalized_graph_timestamp(edge.expired_at),
        "attributes": _normalized_fact_attributes(edge),
        "fact_embedding": normalized_vector,
    }
    return hashlib.sha256(_canonical_json(_normalize_graph_values(payload))).hexdigest()


_ABSENT_FACT_FINGERPRINT = hashlib.sha256(b"absent").hexdigest()


def _receipt_bindings(
    bindings: Sequence[CanonicalEntityBinding],
) -> tuple[CanonicalEntityBinding, ...]:
    """Keep bounded field hashes and support identities while dropping source seed text."""
    return tuple(replace(
        item, node_name=None, node_summary=None,
    ) for item in bindings)


async def _authorize_cross_rebuild_absence(
    context: OperationAuthorization, kind: Literal["node", "fact"], effect_id: str,
    *, episode_id: UUID, endpoints: tuple[str, str] | None = None,
) -> bool:
    """Authorize an already-read exact absence through another operation's rebuild ledger.

    Owner must prove deletion-operation dependency, original cleanup witness,
    completed exact mutation/dispatch and current recovery lease. No callback
    waives a present graph object, current readback or ordinary receipt identity.
    """
    if context.authorize_rebuild_absence is None:
        return False
    await context.authorize_rebuild_absence(kind, effect_id)
    # Journal this operation's actual readback, never relabel the source deletion
    # receipt. Owner preserves the dependency/proof chain alongside this marker.
    receipt_kwargs: dict[str, Any] = {
        "operation_id": context.operation_id, "lease_token": context.lease_token,
        "episode_id": episode_id, "group_id": context.group_id,
        "mapping_revision": context.mapping_revision, "phase": "cleanup_write_intent",
        "desired_support_digest": hashlib.sha256(f"rebuild-absence:{kind}:{effect_id}".encode()).hexdigest(),
        "rebuild_absence_observed": kind,
    }
    if kind == "node":
        receipt_kwargs.update(
            entity_ids=(effect_id,), prior_entity_state_fingerprints=((effect_id, False, None),),
            intended_entity_state_fingerprints=((effect_id, None),),
        )
    else:
        if endpoints is None:
            raise GraphOperationError("graph_rebuild_absence_endpoints")
        receipt_kwargs.update(
            fact_ids=(effect_id,),
            existing_fact_states=(ExactFactState(
                effect_id, endpoints[0], endpoints[1], (), _ABSENT_FACT_FINGERPRINT, False,
            ),),
            intended_fact_support=(ExactFactSupport(
                fact_id=effect_id, episode_ids=(), source_node_id=endpoints[0], target_node_id=endpoints[1],
                state_fingerprint=_ABSENT_FACT_FINGERPRINT,
            ),),
        )
    await context.record_write_intent(GraphWriteReceipt(**receipt_kwargs))
    return True


async def _incident_ids_absent(driver: Any, edge_ids: Sequence[str]) -> bool:
    """Read exact formerly incident edge IDs after typed rebuild deletion.

    No broad graph traversal or text is loaded. Every observed UUID must be
    absent before node cleanup converges, including edges supported elsewhere.
    """
    if len(set(edge_ids)) > MAX_SUPPORT:
        raise GraphOperationError("graph_rebuild_incident_bound")
    if not edge_ids:
        return True
    rows, _, _ = await driver.execute_query(
        "MATCH ()-[edge]->() WHERE edge.uuid IN $edge_ids "
        "RETURN edge.uuid AS edge_id LIMIT $limit",
        edge_ids=list(edge_ids), limit=MAX_SUPPORT + 1,
    )
    return isinstance(rows, list) and not rows


async def _delete_rebuild_node(driver: Any, action: CanonicalNodeRecoveryAction, group_id: str) -> bool:
    """Delete an exact node and its complete owner-authorized incident inventory atomically.

    Prior fingerprint and scheduling are checked by the caller. The single graph
    write rechecks every edge UUID/type/endpoints/support plus inventory equality;
    a new or changed link makes it a no-op. No DETACH or unknown edge is deleted.
    Exact edge absence is read back independently before the caller checks node.
    """
    expected = [{
        "edge_id": link.edge_id, "relationship_type": link.relationship_type,
        "source_node_id": link.source_node_id, "target_node_id": link.target_node_id,
        "episode_ids": list(link.episode_ids),
    } for link in action.expected_incident_links]
    rows, _, _ = await driver.execute_query(
        "MATCH (entity:Entity {uuid: $entity_id, group_id: $group_id}) "
        "OPTIONAL MATCH (entity)-[edge]-() "
        "WITH entity, collect(edge) AS edges "
        "WHERE size(edges) = size($expected) AND all(edge IN edges WHERE "
        "any(link IN $expected WHERE edge.uuid = link.edge_id "
        "AND type(edge) = link.relationship_type "
        "AND startNode(edge).uuid = link.source_node_id "
        "AND endNode(edge).uuid = link.target_node_id "
        "AND size(coalesce(edge.episodes, [])) = size(link.episode_ids) "
        "AND all(episode IN coalesce(edge.episodes, []) WHERE episode IN link.episode_ids))) "
        "WITH entity, edges, entity.uuid AS entity_id "
        "FOREACH (edge IN edges | DELETE edge) DELETE entity RETURN entity_id",
        entity_id=action.graph_entity_uuid, group_id=group_id, expected=expected,
    )
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get("entity_id") != action.graph_entity_uuid):
        return False
    return await _incident_ids_absent(driver, [link.edge_id for link in action.expected_incident_links])


async def _send_owned_command(connection: Any, *args: Any) -> None:
    """Send only on the already-owned socket without reconnect or health retries.

    No await separates the connected check from the pinned send implementation's
    own connected check. Disable health probes that could reconnect transparently.
    """
    if not connection.is_connected:
        raise GraphOperationUnknown("graph_dispatch_connection_lost")
    await connection.send_command(*args, check_health=False)


class _OwnedDispatchTransport:
    """Serialize graph commands on one journaled socket without retry/reconnection.

    Every graph command executes under Redis MULTI/EXEC, forcing pinned FalkorDB
    to finish on the Redis main thread. This excludes detached query-worker queues.
    """

    def __init__(self, connection: Any, ownership: DispatchOwnership) -> None:
        """Bind an already-connected socket and immutable persisted identity."""
        self.connection = connection
        self.ownership = ownership
        self.lock = asyncio.Lock()
        self.poisoned = False
        self.pending_commands = 0

    async def command(self, *args: Any, **kwargs: Any) -> Any:
        """Run one owned graph command synchronously and poison ambiguous transport.

        Raw send/read deliberately bypass Redis client's reconnect/retry logic.
        Query parsing and schema lookups remain in the pinned FalkorDB client;
        their subsequent graph commands use this same transport and lock.
        """
        if (kwargs or len(args) < 3 or str(args[0]).upper() not in {"GRAPH.QUERY", "GRAPH.RO_QUERY"}
                or args[1] != self.ownership.group_id):
            raise GraphOperationError("graph_dispatch_command_denied")
        self.pending_commands += 1
        try:
            async with self.lock:
                if self.poisoned or not self.connection.is_connected:
                    raise GraphOperationUnknown("graph_dispatch_connection_lost")
                completed = False
                try:
                    await _send_owned_command(self.connection, "MULTI")
                    if await self.connection.read_response() != "OK":
                        raise GraphOperationUnknown("graph_dispatch_protocol")
                    await _send_owned_command(self.connection, *args)
                    if await self.connection.read_response() != "QUEUED":
                        raise GraphOperationUnknown("graph_dispatch_protocol")
                    await _send_owned_command(self.connection, "EXEC")
                    response = await self.connection.read_response()
                    if not isinstance(response, list) or len(response) != 1:
                        raise GraphOperationUnknown("graph_dispatch_protocol")
                    completed = True
                    if isinstance(response[0], Exception):
                        raise response[0]
                    return response[0]
                except BaseException:
                    # Never reuse a socket whose EXEC/result ownership became ambiguous.
                    if not completed:
                        self.poisoned = True
                    raise
        finally:
            self.pending_commands -= 1


def _make_driver_type() -> type[Any]:
    """Load Graphiti after telemetry is disabled and wrap every Falkor query path."""
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    from graphiti_core.driver.falkordb_driver import FalkorDriver, FalkorDriverSession
    from graphiti_core.utils.datetime_utils import convert_datetimes_to_strings

    class SafeGraph:
        """Apply timeouts and redacted failures to all AsyncGraph query callers."""

        def __init__(self, graph: Any, write_intent_hook: Callable[..., Awaitable[None]] | None = None) -> None:
            """Wrap one graph with the request-scoped prewrite journal callback."""
            self._graph = graph
            self.write_intent_hook = write_intent_hook

        async def query(self, query: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            """Run one parameterized query with bounded client and server deadlines."""
            try:
                async with asyncio.timeout(QUERY_SECONDS):
                    return await self._graph.query(
                        query, params or {}, timeout=QUERY_SECONDS * 1000, **kwargs
                    )
            except Exception as exc:
                raise _safe_error(exc) from None

        def __getattr__(self, name: str) -> Any:
            """Forward non-query methods while keeping the protected query override authoritative."""
            if name == "query":
                raise AttributeError(name)
            return getattr(self._graph, name)

    class SafeSession(FalkorDriverSession):
        """Preserve upstream session operations while routing queries through SafeGraph."""

        def __init__(self, graph: Any) -> None:
            """Attach the already-protected graph without losing its operation callback."""
            super().__init__(graph)
            self._bulk_capture: list[tuple[Any, dict[str, Any]]] | None = None
            self._bulk_capture_size = 0

        def _validated_bulk_payloads(
            self, calls: Sequence[tuple[Any, dict[str, Any]]], args: tuple[Any, ...], kwargs: dict[str, Any],
        ) -> list[dict[str, Any]]:
            """Validate the pinned four-call Falkor shape and return its final hydrated node payloads."""
            from graphiti_core.driver.driver import GraphProvider
            from graphiti_core.models.edges.edge_db_queries import (
                get_entity_edge_save_bulk_query, get_episodic_edge_save_bulk_query,
            )
            from graphiti_core.models.nodes.node_db_queries import (
                get_entity_node_save_bulk_query, get_episode_node_save_bulk_query,
            )
            from graphiti_core.driver.falkordb_driver import _strip_nul_bytes

            driver = kwargs["driver"]
            if (driver.provider != GraphProvider.FALKORDB
                    or getattr(driver, "graph_operations_interface", None) is not None
                    or len(calls) != 4 or len(args) != 5):
                raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            provider = driver.provider
            (episode_query, episode_params), (node_query, node_params), \
                (mention_query, mention_params), (fact_query, fact_params) = calls
            if (episode_query != get_episode_node_save_bulk_query(provider)
                    or not isinstance(episode_params, dict) or set(episode_params) != {"episodes"}
                    or not isinstance(episode_params["episodes"], list)
                    or len(episode_params["episodes"]) != len(args[0])
                    or not isinstance(node_query, list)
                    or not isinstance(node_params, dict) or set(node_params) != {"nodes"}
                    or not isinstance(node_params["nodes"], list)
                    or len(node_query) != len(node_params["nodes"])
                    or not isinstance(mention_query, str)
                    or mention_query != get_episodic_edge_save_bulk_query(provider)
                    or not isinstance(mention_params, dict) or set(mention_params) != {"episodic_edges"}
                    or not isinstance(mention_params["episodic_edges"], list)
                    or not isinstance(fact_query, str)
                    or fact_query != get_entity_edge_save_bulk_query(provider)
                    or not isinstance(fact_params, dict) or set(fact_params) != {"entity_edges"}
                    or not isinstance(fact_params["entity_edges"], list)):
                raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")

            node_payloads = node_params["nodes"]
            expected_node_ids = {str(item.uuid) for item in args[2]}
            expected_episode_ids = {str(item.uuid) for item in args[0]}
            expected_mention_ids = {str(item.uuid) for item in args[1]}
            expected_fact_ids = {str(item.uuid) for item in args[3]}
            expected_node_queries = get_entity_node_save_bulk_query(provider, node_payloads)
            if len(expected_node_queries) != len(node_query):
                raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            for actual, expected in zip(node_query, expected_node_queries, strict=True):
                if (not isinstance(actual, tuple) or len(actual) != 2
                        or actual[0] != expected[0]
                        or actual[1] != _strip_nul_bytes(convert_datetimes_to_strings(expected[1]))):
                    raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            ids = [str(item.get("uuid")) for item in node_payloads if isinstance(item, dict)]
            if (len(ids) != len(node_payloads) or len(ids) != len(set(ids))
                    or any(not _UUID.fullmatch(value) for value in ids)
                    or set(ids) != expected_node_ids
                    or any(not isinstance(item.get("labels"), list) or "Entity" not in item["labels"]
                           for item in node_payloads)):
                raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            if len(expected_node_ids | expected_episode_ids | expected_mention_ids | expected_fact_ids) > MAX_SUPPORT:
                raise GraphOperationError("graph_bulk_capture_limit")
            for payload_key, values in (("episodes", episode_params["episodes"]),
                                        ("episodic_edges", mention_params["episodic_edges"]),
                                        ("entity_edges", fact_params["entity_edges"])):
                if (len(values) > MAX_SUPPORT or any(not isinstance(item, dict) for item in values)
                        or len({str(item.get("uuid")) for item in values}) != len(values)
                        or any(not _UUID.fullmatch(str(item.get("uuid"))) for item in values)
                        or any(item.get("group_id") != args[0][0].group_id for item in values)
                        or {str(item.get("uuid")) for item in values} != (
                            expected_episode_ids if payload_key == "episodes" else
                            expected_mention_ids if payload_key == "episodic_edges" else expected_fact_ids
                        )):
                    raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            if any(not isinstance(item, dict) for item in node_payloads):
                raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            if any(item.get("group_id") != args[0][0].group_id for item in node_payloads):
                raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            for data, edge in zip(mention_params["episodic_edges"], args[1], strict=True):
                if (str(data.get("source_node_uuid")) != str(edge.source_node_uuid)
                        or str(data.get("target_node_uuid")) != str(edge.target_node_uuid)):
                    raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            for data, edge in zip(fact_params["entity_edges"], args[3], strict=True):
                if (str(data.get("source_node_uuid")) != str(edge.source_node_uuid)
                        or str(data.get("target_node_uuid")) != str(edge.target_node_uuid)
                        or tuple(sorted(str(value) for value in data.get("episodes", ())))
                        != tuple(sorted(str(value) for value in edge.episodes))):
                    raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
            return node_payloads

        async def execute_write(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
            """Journal exact hydrated state before replaying the pinned structured bulk calls."""
            from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk_tx

            if func is add_nodes_and_edges_bulk_tx:
                hook = getattr(self.graph, "write_intent_hook", None)
                if hook is None:
                    raise GraphOperationError("graph_write_intent_unavailable")
                if len(args) != 5 or "driver" not in kwargs or self._bulk_capture is not None:
                    raise GraphOperationError("graph_write_intent_arguments_invalid")
                from graphiti_core.driver.driver import GraphProvider
                bulk_driver = kwargs["driver"]
                if (bulk_driver.provider != GraphProvider.FALKORDB
                        or getattr(bulk_driver, "graph_operations_interface", None) is not None):
                    raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
                # Initial receipt proves the full ID/prior inventory; hydration vectors are not
                # available until the pinned writer has materialized all four structured calls.
                await hook(args[0], args[1], args[2], args[3], kwargs["driver"])
                self._bulk_capture = []
                self._bulk_capture_size = 0
                try:
                    result = await func(self, *args, **kwargs)
                    calls = self._bulk_capture
                    if calls is None:
                        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
                    nodes = self._validated_bulk_payloads(calls, args, kwargs)
                    await hook(
                        args[0], args[1], args[2], args[3], kwargs["driver"],
                        final_node_payloads=nodes,
                    )
                    # No Graphiti bulk write reaches Falkor before the final-vector receipt commits.
                    self._bulk_capture = None
                    for query, params in calls:
                        await self.run(query, **params)
                    return result
                finally:
                    self._bulk_capture = None
                    self._bulk_capture_size = 0
            return await func(self, *args, **kwargs)

        async def run(self, query: str | list[Any], **kwargs: Any) -> Any:
            """Capture bounded normalized bulk calls or dispatch through the protected graph."""
            from graphiti_core.driver.falkordb_driver import _strip_nul_bytes

            try:
                if self._bulk_capture is not None:
                    if isinstance(query, list):
                        normalized_query = [
                            (cypher, _strip_nul_bytes(convert_datetimes_to_strings(params)))
                            for cypher, params in query
                        ]
                    else:
                        normalized_query = query
                    normalized_params = _strip_nul_bytes(convert_datetimes_to_strings(dict(kwargs)))
                    serialized = json.dumps(
                        {"query": normalized_query, "params": normalized_params},
                        sort_keys=True, separators=(",", ":"), default=str,
                    ).encode()
                    if (len(self._bulk_capture) >= 4
                            or self._bulk_capture_size + len(serialized) > MAX_CAPTURE_BYTES):
                        raise GraphOperationError("graph_bulk_capture_limit")
                    self._bulk_capture.append((copy.deepcopy(normalized_query), copy.deepcopy(normalized_params)))
                    self._bulk_capture_size += len(serialized)
                    return None
                async with asyncio.timeout(QUERY_SECONDS):
                    if isinstance(query, list):
                        for cypher, params in query:
                            values = _strip_nul_bytes(convert_datetimes_to_strings(params))
                            await self.graph.query(str(cypher), values)
                    else:
                        values = _strip_nul_bytes(convert_datetimes_to_strings(dict(kwargs)))
                        await self.graph.query(str(query), values)
                return None
            except Exception as exc:
                raise _safe_error(exc) from None

    class SafeDriver(FalkorDriver):
        """Prevent upstream raw Cypher logs and share one bounded client lifecycle."""

        def _get_graph(self, graph_name: str | None) -> Any:
            """Return a protected graph that retains this operation clone's receipt hook."""
            graph = super()._get_graph(graph_name)
            transport = _dispatch_transport.get()
            if transport is not None:
                if graph.name != transport.ownership.group_id:
                    raise GraphOperationError("graph_dispatch_partition_mismatch")
                # Schema refresh/procedure paths call AsyncGraph.execute_command too;
                # overriding this boundary prevents those reads bypassing ownership.
                graph.execute_command = transport.command
            return SafeGraph(graph, getattr(self, "_write_intent_hook", None))

        async def execute_query(self, cypher_query_: str, **kwargs: Any) -> Any:
            """Normalize Falkor values, execute safely, and translate rows for Graphiti."""
            from graphiti_core.driver.falkordb_driver import _strip_nul_bytes

            params = _strip_nul_bytes(convert_datetimes_to_strings(dict(kwargs)))
            try:
                result = await self._get_graph(self._database).query(cypher_query_, params)
            except GraphOperationError as exc:
                if str(exc) == "graph_index_exists":
                    return None
                raise
            if len(result.result_set) > MAX_SUPPORT + 1:
                raise GraphOperationError("graph_query_result_limit")
            headers = [column[1] for column in result.header]
            records = [
                {name: row[index] if index < len(row) else None for index, name in enumerate(headers)}
                for row in result.result_set
            ]
            return records, headers, None

        def session(self, database: str | None = None) -> Any:
            """Return a protected session retaining the request-scoped prewrite hook."""
            return SafeSession(self._get_graph(database))

        def clone(self, database: str) -> Any:
            """Copy driver state for a partition without creating a new client or index task."""
            import copy

            clone = copy.copy(self)
            clone._database = self.default_group_id if database == self.default_group_id else database
            clone._init_task = None
            clone._owns_client = False
            return clone

        async def health_check(self) -> None:
            """Run one connectivity query without printing upstream exceptions."""
            await self.execute_query("RETURN 1 AS healthy")

        async def close(self) -> None:
            """Await the owning shared transport close and mark it closed only on success.

            Lifecycle callers shield and join this coroutine under heavy ownership;
            no local timeout cancels upstream cleanup or masks a retryable failure.
            """
            if getattr(self, "_owns_client", True) and not getattr(self, "_transport_closed", False):
                await super().close()
                self._transport_closed = True

    return SafeDriver


def _make_clients(context: OperationAuthorization, gateway: ModelGateway) -> tuple[Any, Any, Any]:
    """Adapt Graphiti clients to existing policy-aware gateway methods."""
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient

    class GatewayLLM(LLMClient):
        """Route all Graphiti generation attempts through the current gateway policy."""

        def __init__(self) -> None:
            """Disable Graphiti's disk cache and pin generation bounds for gateway-routed requests."""
            from graphiti_core.llm_client.config import LLMConfig

            super().__init__(config=LLMConfig(temperature=0, max_tokens=4096), cache=False)

        async def _generate_response(
            self, messages: list[Any], response_model: type[Any] | None = None,
            max_tokens: int = 4096, model_size: Any = None,
        ) -> dict[str, Any]:
            """Translate messages/schema while keeping raw model output out of errors and logs."""
            payload = [{"role": item.role, "content": item.content} for item in messages]
            model = (
                context.small_reasoning
                if getattr(model_size, "value", "medium") == "small"
                else context.reasoning
            )
            token_limit = min(max_tokens or 4096, 4096)

            async def before_send() -> None:
                """Revalidate exact support and reasoning consent before each provider attempt."""
                await _authorize(context, "reasoning")

            try:
                if response_model is not None:
                    schema = {
                        "name": response_model.__name__, "strict": True,
                        "schema": response_model.model_json_schema(),
                    }
                    raw = await gateway.structured(
                        model.alias, model.mapping,
                        model.policy, payload, schema, max_tokens=token_limit,
                        temperature=0, before_send=before_send,
                    )
                    content = _completion_content(raw)
                    parsed = json.loads(content)
                else:
                    raw = await gateway.chat(
                        model.alias, model.mapping,
                        model.policy, payload, max_tokens=token_limit,
                        temperature=0, before_send=before_send,
                    )
                    parsed = _completion_content(raw)
                    parsed = json.loads(parsed)
                if response_model is not None:
                    response_model.model_validate(parsed)
                return parsed
            except (ModelGatewayError, ValueError, TypeError, json.JSONDecodeError):
                raise GraphOperationError("graph_model_unavailable") from None

        async def _generate_response_with_retry(
            self, messages: list[Any], response_model: type[Any] | None = None,
            max_tokens: int = 4096, model_size: Any = None,
        ) -> dict[str, Any]:
            """Keep Graphiti prompt normalization while leaving retry ownership to ModelGateway."""
            return await self._generate_response(
                messages, response_model, max_tokens or self.max_tokens, model_size,
            )

    class GatewayEmbedder(EmbedderClient):
        """Use configured gateway embeddings with exact dimensions and input bounds."""

        async def create(self, input_data: Any) -> list[float]:
            """Embed bounded text and reject token iterables lacking stable source provenance."""
            if not isinstance(input_data, str) or len(input_data.encode()) > MAX_INPUT_BYTES:
                raise GraphOperationError("graph_embedding_input_unsupported")
            return (await self.create_batch([input_data]))[0]

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            """Validate ordered indexed vectors, exact dimensions, finite values, and nonzero norm."""
            model = context.embedding
            dimensions = model.dimensions
            if dimensions is None or not 1 <= dimensions <= 4096:
                raise GraphOperationError("graph_embedding_dimension_unconfigured")
            if not 1 <= len(input_data_list) <= MAX_CANDIDATES or any(
                not isinstance(item, str) or len(item.encode()) > MAX_INPUT_BYTES for item in input_data_list
            ):
                raise GraphOperationError("graph_embedding_input_invalid")

            async def before_send() -> None:
                """Revalidate the held source/document support and embedding grant per attempt."""
                await _authorize(context, "embedding")

            raw = await gateway.embed(
                model.alias, model.mapping, model.policy, input_data_list, before_send=before_send,
            )
            data = raw.get("data") if isinstance(raw, dict) else None
            if not isinstance(data, list) or len(data) != len(input_data_list):
                raise GraphOperationError("graph_embedding_response_invalid")
            indexes = [item.get("index") for item in data if isinstance(item, dict)]
            if indexes != list(range(len(data))):
                raise GraphOperationError("graph_embedding_order_invalid")
            vectors = [item.get("embedding") for item in data]
            if any(not isinstance(vector, list) or len(vector) != dimensions for vector in vectors):
                raise GraphOperationError("graph_embedding_dimension_invalid")
            result = [[float(value) for value in vector] for vector in vectors]
            if any(not math.isfinite(value) for vector in result for value in vector) or any(
                not any(value != 0 for value in vector) for vector in result
            ):
                raise GraphOperationError("graph_embedding_value_invalid")
            return result

    class GatewayCrossEncoder(CrossEncoderClient):
        """Use only an explicitly configured gateway reranking capability."""

        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            """Validate unique bounded passage indexes and return deterministic score order."""
            model = context.reranking
            if model is None or not passages or len(passages) > MAX_CANDIDATES:
                raise GraphOperationError("graph_reranking_unavailable")

            async def before_send() -> None:
                """Revalidate complete operation support and embedding consent per attempt."""
                await _authorize(context, "embedding")

            raw = await gateway.rerank(
                model.alias, model.mapping, model.policy, query, passages, before_send=before_send,
            )
            rows = raw.get("results") if isinstance(raw, dict) else None
            if not isinstance(rows, list) or len(rows) > len(passages):
                raise GraphOperationError("graph_reranking_response_invalid")
            ranked: list[tuple[int, str, float]] = []
            seen: set[int] = set()
            for row in rows:
                index = row.get("index") if isinstance(row, dict) else None
                score = row.get("relevance_score", row.get("score")) if isinstance(row, dict) else None
                if (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(passages)
                        or index in seen or not isinstance(score, int | float) or not math.isfinite(score)):
                    raise GraphOperationError("graph_reranking_response_invalid")
                seen.add(index)
                ranked.append((index, passages[index], float(score)))
            return [(text, score) for _, text, score in sorted(ranked, key=lambda item: (-item[2], item[0]))]

    return GatewayLLM(), GatewayEmbedder(), GatewayCrossEncoder()


async def _authorize(context: OperationAuthorization, capability: Literal["reasoning", "embedding"]) -> None:
    """Enforce owner freshness and aggregate outbound model-call limits."""
    assert context.model_calls is not None
    context.model_calls[0] += 1
    if context.model_calls[0] > MAX_MODEL_CALLS:
        raise GraphOperationError("graph_model_call_budget_exhausted")
    await context.authorize(capability)


def _completion_content(value: Any) -> str:
    """Extract bounded OpenAI-compatible completion text without exposing invalid output."""
    choices = value.get("choices") if isinstance(value, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or len(content.encode()) > MAX_INPUT_BYTES:
        raise ValueError("invalid completion")
    return content


class TemporalGraph:
    """Own disabled-by-default Graphiti lifecycle and fenced graph operations."""

    def __init__(self, configuration: GraphConfiguration) -> None:
        """Store a separate graph endpoint without importing packages or opening sockets."""
        self.configuration = configuration
        self.state = GraphState.DISABLED if not configuration.enabled else GraphState.UNCONFIGURED
        self._driver: Any | None = None
        self._graphiti_type: Any | None = None
        self._lock = asyncio.Lock()
        self._partition_index_lock = asyncio.Lock()
        self._indexed_partitions: OrderedDict[str, None] = OrderedDict()

    async def initialize(self) -> GraphState:
        """Adopt a usable client after owned construction, cleanup and index setup.

        Startup budget bounds normal construction/index admission; timeout or
        cancellation still joins the noncancellable constructor thread and closes
        its returned driver before returning. That join has no hard wall-clock
        bound, so callers must retain heavy-work ownership through this await.
        """
        config = self.configuration
        if not config.enabled:
            await self.close()
            return self.state
        if not config.host or not config.password or not 1 <= config.port <= 65535:
            await self.close()
            return self.state
        async with self._lock:
            if self.state == GraphState.READY and self._driver is not None:
                return self.state
            # A failed health check can leave an adopted transport behind; dispose it before retrying.
            stale_driver = self._driver
            self._graphiti_type = None
            if stale_driver is not None:
                try:
                    await self._close_owned_driver(stale_driver)
                except Exception as exc:
                    logger.warning("Graph retry cleanup incomplete category=%s", type(exc).__name__)
                    self._driver = stale_driver
                    return GraphState.UNAVAILABLE
            self.state = GraphState.UNAVAILABLE
            os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
            try:
                driver_type = _make_driver_type()

                def construct() -> Any:
                    """Own synchronous INFO off-loop with bounded transport and no retries."""
                    module = importlib.import_module("falkordb.asyncio")
                    client = module.FalkorDB(
                        host=config.host, port=config.port, username=config.username,
                        password=config.password, socket_connect_timeout=2,
                        socket_timeout=QUERY_SECONDS, retry_on_timeout=False,
                        max_connections=4, protocol=2,
                    )
                    return driver_type(falkor_db=client, database=config.database)

                loop = asyncio.get_running_loop()
                deadline = loop.time() + STARTUP_SECONDS
                construction = asyncio.create_task(asyncio.to_thread(construct))
                try:
                    self._driver = await asyncio.wait_for(
                        asyncio.shield(construction), timeout=max(0.0, deadline - loop.time())
                    )
                except (TimeoutError, asyncio.CancelledError) as interrupted:
                    # Cancellation cannot stop a Python thread. Join it under the
                    # caller's capacity ownership, then dispose its returned driver.
                    try:
                        self._driver, cancelled_during_join = await self._join_owned_task(construction)
                    except Exception as construction_error:
                        if isinstance(interrupted, asyncio.CancelledError):
                            raise interrupted from construction_error
                        raise
                    if cancelled_during_join:
                        raise asyncio.CancelledError from interrupted
                    raise
                self._graphiti_type = importlib.import_module("graphiti_core.graphiti").Graphiti
                async with asyncio.timeout_at(deadline):
                    await self._driver.build_indices_and_constraints(delete_existing=False)
                self.state = GraphState.READY
            except asyncio.CancelledError:
                self.state = GraphState.UNAVAILABLE
                failed_driver = self._driver
                self._graphiti_type = None
                if failed_driver is not None:
                    try:
                        await self._close_owned_driver(failed_driver)
                    except Exception as exc:
                        logger.warning("Graph initialization cleanup incomplete category=%s", type(exc).__name__)
                raise
            except Exception as exc:
                logger.warning("Graph initialization unavailable category=%s", type(exc).__name__)
                failed_driver = self._driver
                self._graphiti_type = None
                if failed_driver is not None:
                    try:
                        await self._close_owned_driver(failed_driver)
                    except Exception as cleanup_exc:
                        logger.warning("Graph initialization cleanup incomplete category=%s", type(cleanup_exc).__name__)
                self.state = GraphState.UNAVAILABLE
            return self.state

    async def _join_owned_task(self, task: asyncio.Task[Any]) -> tuple[Any, bool]:
        """Join local work despite repeated caller cancellation, without orphan tasks.

        Return the result and whether the caller was cancelled while waiting.
        Callers adopt/close resources before propagating that cancellation. There
        is no hard thread join deadline; completed-task exceptions still surface.
        """
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                # Retrieve the finished error below after preserving cancellation.
                break
        try:
            result = task.result()
        except Exception as exc:
            if cancelled:
                raise asyncio.CancelledError from exc
            raise
        return result, cancelled

    async def _close_owned_driver(self, driver: Any) -> None:
        """Await local driver cleanup to completion before releasing lifecycle ownership.

        Shield only protects the joined cleanup from caller cancellation. No
        background callback owns late cleanup and no elapsed budget permits an
        unfinished task to escape; propagate cancellation after successful close.
        A close exception retains the driver handle for a later explicit retry.
        """
        cleanup = asyncio.create_task(driver.close())
        _, cancelled = await self._join_owned_task(cleanup)
        if self._driver is driver:
            self._driver = None
        if cancelled:
            raise asyncio.CancelledError

    async def health(self) -> GraphState:
        """Check the adopted transport and join local failure cleanup under its lock.

        Health query budget does not bound cleanup completion; cancellation while
        closing is deferred until the owned close task finishes.
        """
        async with self._lock:
            if self.state != GraphState.READY or self._driver is None:
                return self.state
            try:
                async with asyncio.timeout(QUERY_SECONDS):
                    await self._driver.health_check()
                return GraphState.READY
            except Exception as exc:
                logger.warning("Graph health unavailable category=%s", type(exc).__name__)
                self.state = GraphState.UNAVAILABLE
                failed_driver = self._driver
                if failed_driver is not None:
                    try:
                        await self._close_owned_driver(failed_driver)
                        self._graphiti_type = None
                    except Exception as cleanup_exc:
                        logger.warning("Graph health cleanup incomplete category=%s", type(cleanup_exc).__name__)
                return self.state

    async def close(self) -> None:
        """Serialize shutdown and await adopted local cleanup under caller ownership.

        Initialization holds the same lock until constructor/index work and failure
        cleanup finish. Close cannot return with a late constructor/cleanup task;
        cancellation propagates only after an active driver close is joined.
        Driver-close errors are reported and retain its handle for explicit retry.
        """
        async with self._lock:
            driver = self._driver
            self._graphiti_type = None
            self.state = GraphState.UNCONFIGURED if self.configuration.enabled else GraphState.DISABLED
            if driver is not None:
                try:
                    await self._close_owned_driver(driver)
                except Exception as exc:
                    logger.warning("Graph close incomplete category=%s", type(exc).__name__)

    def _graphiti(self, context: OperationAuthorization, gateway: ModelGateway, driver: Any | None = None) -> Any:
        """Inject controlled clients and an operation driver so Graphiti cannot use provider defaults."""
        if self.state != GraphState.READY or self._driver is None or self._graphiti_type is None:
            raise GraphOperationError("graph_unavailable")
        llm, embedder, cross_encoder = _make_clients(context, gateway)
        return self._graphiti_type(
            graph_driver=driver or self._driver, llm_client=llm, embedder=embedder,
            cross_encoder=cross_encoder, max_coroutines=1,
        )

    @asynccontextmanager
    async def dispatch_session(self, context: OperationAuthorization) -> AsyncIterator[DispatchOwnership]:
        """Journal a dedicated Redis socket before all operation graph commands.

        The owner callback must commit exact identity under its live operation
        lease; no graph command precedes that durable commit. Parent holds this
        scope around the complete awaited adapter operation. Raw socket use never
        reconnects; cancellation disconnects it and leaves remote outcome to exact
        cessation verification. Setup probes contain no private evidence.
        Requires positive modern server TIMEOUT_DEFAULT/MAX configuration for
        bounded write commands, verifies it read-only, and never changes settings.
        Completion callback runs only after normal fully replied scope/socket
        closure, updating this exact dispatch rather than any earlier unknown one.
        """
        if (self.state != GraphState.READY or self._driver is None
                or context.record_dispatch_ownership is None
                or _dispatch_transport.get() is not None):
            raise GraphOperationError("graph_dispatch_ownership_required")
        pool = self._driver.client.connection.connection_pool
        connection = None
        context_token = None
        replied = False
        try:
            async with asyncio.timeout(QUERY_SECONDS):
                connection = await pool.get_connection()
                await _send_owned_command(connection, "INFO", "server")
                info = await connection.read_response()
                run_id = next((line.split(":", 1)[1].strip() for line in str(info).splitlines()
                               if line.startswith("run_id:")), "")
                await _send_owned_command(connection, "CLIENT", "ID")
                client_id = await connection.read_response()
                await _send_owned_command(connection, "GRAPH.CONFIG", "GET", "TIMEOUT_DEFAULT")
                default_timeout = await connection.read_response()
                await _send_owned_command(connection, "GRAPH.CONFIG", "GET", "TIMEOUT_MAX")
                maximum_timeout = await connection.read_response()
                if (not isinstance(default_timeout, list) or len(default_timeout) != 2
                        or not isinstance(maximum_timeout, list) or len(maximum_timeout) != 2
                        or max(int(default_timeout[1]), int(maximum_timeout[1])) <= 0
                        or (int(maximum_timeout[1]) > 0 and int(maximum_timeout[1]) < QUERY_SECONDS * 1000)):
                    raise GraphOperationError("graph_write_timeout_unconfigured")
                ownership = DispatchOwnership(context.operation_id, context.group_id, run_id, client_id)
                await context.record_dispatch_ownership(ownership)
            transport = _OwnedDispatchTransport(connection, ownership)
            context_token = _dispatch_transport.set(transport)
            yield ownership
            replied = not transport.poisoned and transport.pending_commands == 0
        except GraphOperationError:
            raise
        except Exception as exc:
            raise _safe_error(exc) from None
        finally:
            if context_token is not None:
                transport = _dispatch_transport.get()
                if transport is not None:
                    transport.poisoned = True
                _dispatch_transport.reset(context_token)
            if connection is not None:
                # Closing a socket is not itself proof of prior remote completion.
                await connection.disconnect()
                await pool.release(connection)
        if replied and context.record_dispatch_completion is not None:
            # Exact synchronous reply proof closes only this dispatch journal row.
            async with asyncio.timeout(QUERY_SECONDS):
                await context.record_dispatch_completion(ownership)

    async def verify_dispatch_cessation(
        self, ownership: DispatchOwnership, context: OperationAuthorization,
    ) -> bool:
        """Disconnect exactly an owner-journaled synchronous dispatch and prove cessation.

        Owner callback must compare stored ownership, current recovery lease and
        stopped local tasks before any CLIENT KILL. Same-process Redis main-thread
        kill reply follows every earlier synchronous EXEC and prevents buffered
        commands on the killed socket. A changed process run_id proves old process
        cessation without killing a potentially reused ID. No graph/model context
        is loaded; this says nothing about success, requiring exact-ID reconciliation.
        Legacy asynchronous dispatch without this ownership cannot use this proof.
        """
        if (self.state != GraphState.READY or self._driver is None
                or ownership.group_id != context.group_id
                or ownership.operation_id != context.operation_id
                or context.authorize_dispatch_cessation is None):
            raise GraphOperationError("graph_dispatch_cessation_denied")
        await context.authorize_dispatch_cessation(ownership)
        pool = self._driver.client.connection.connection_pool
        connection = None
        try:
            async with asyncio.timeout(QUERY_SECONDS):
                connection = await pool.get_connection()
                await _send_owned_command(connection, "INFO", "server")
                info = await connection.read_response()
                run_id = next((line.split(":", 1)[1].strip() for line in str(info).splitlines()
                               if line.startswith("run_id:")), "")
                if not re.fullmatch(r"[0-9a-f]{40}", run_id):
                    raise GraphOperationError("graph_dispatch_server_identity")
                if run_id != ownership.server_run_id:
                    return True
                await _send_owned_command(connection, "CLIENT", "ID")
                if await connection.read_response() == ownership.client_id:
                    raise GraphOperationError("graph_dispatch_cessation_connection")
                await context.authorize_dispatch_cessation(ownership)
                if not connection.is_connected:
                    raise GraphOperationError("graph_dispatch_connection_lost")
                # Same raw socket for INFO and KILL: no reconnect can change the
                # server between identity validation and killing an exact client.
                await _send_owned_command(connection, "CLIENT", "KILL", "ID", ownership.client_id, "SKIPME", "NO")
                killed = await connection.read_response()
                if killed not in (0, 1):
                    raise GraphOperationError("graph_dispatch_cessation_result")
                return True
        finally:
            if connection is not None:
                await connection.disconnect()
                await pool.release(connection)

    async def _partition_driver(
        self, group_id: str,
        write_intent_hook: Callable[..., Awaitable[None]] | None = None,
    ) -> Any:
        """Return a partition clone only inside a journaled owned dispatch scope.

        Index setup and all graph query/session/schema paths consume that same
        non-reconnecting transport; receipt hooks retain their original ordering.
        """
        if self._driver is None:
            raise GraphOperationError("graph_unavailable")
        transport = _dispatch_transport.get()
        if transport is None or transport.ownership.group_id != group_id:
            raise GraphOperationError("graph_dispatch_ownership_required")
        driver = self._driver.clone(group_id)
        async with self._partition_index_lock:
            if group_id not in self._indexed_partitions:
                await driver.build_indices_and_constraints(delete_existing=False)
                self._indexed_partitions[group_id] = None
                if len(self._indexed_partitions) > MAX_SUPPORT:
                    self._indexed_partitions.popitem(last=False)
            else:
                self._indexed_partitions.move_to_end(group_id)
        if write_intent_hook is not None:
            driver._write_intent_hook = write_intent_hook
        return driver

    async def _record_bulk_write_intent(
        self, request: EpisodeRequest, context: OperationAuthorization,
        shell_receipt: GraphWriteReceipt, latest_receipt: list[GraphWriteReceipt | None],
        episodes: Sequence[Any], mentions: Sequence[Any], nodes: Sequence[Any],
        facts: Sequence[Any], driver: Any, *,
        final_node_payloads: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """Journal IDs first, then final hydrated node state before any captured bulk write dispatch."""
        from graphiti_core.edges import EntityEdge
        from graphiti_core.nodes import EntityNode

        episode_ids = tuple(str(item.uuid) for item in episodes)
        entity_ids = tuple(sorted({str(item.uuid) for item in nodes}))
        mention_ids = tuple(sorted({str(item.uuid) for item in mentions}))
        fact_ids = tuple(sorted({str(item.uuid) for item in facts}))
        if (episode_ids != (str(request.episode_id),)
                or any(item.group_id != request.group_id for item in (*episodes, *mentions, *nodes, *facts))
                or any(not _UUID.fullmatch(value) for value in (*entity_ids, *mention_ids, *fact_ids))
                or len(entity_ids) > MAX_SUPPORT or len(mention_ids) > MAX_SUPPORT
                or len(fact_ids) > MAX_SUPPORT or len(episodes) > 1
                or len(set((*entity_ids, *mention_ids, *fact_ids, *episode_ids))) > MAX_SUPPORT):
            raise GraphOperationError("graph_write_intent_identifiers_invalid")
        if len(fact_ids) != len(facts) or len(entity_ids) != len(nodes) or len(mention_ids) != len(mentions):
            raise GraphOperationError("graph_write_intent_identifiers_ambiguous")
        prior_entities = await EntityNode.get_by_uuids(driver, list(entity_ids)) if entity_ids else []
        prior_entity_by_id = {str(item.uuid): item for item in prior_entities}
        if (len(prior_entity_by_id) != len(prior_entities) or not set(prior_entity_by_id) <= set(entity_ids)
                or any(item.group_id != request.group_id for item in prior_entities)):
            raise GraphOperationError("graph_write_intent_prior_state_invalid")
        for entity in prior_entities:
            await entity.load_name_embedding(driver)
        prior_entity_states = tuple(sorted((
            entity_id,
            entity_id in prior_entity_by_id,
            (_entity_state_fingerprint(prior_entity_by_id[entity_id], context.embedding.dimensions)
             if entity_id in prior_entity_by_id else None),
        ) for entity_id in entity_ids))
        previous_receipt = latest_receipt[0]
        if (final_node_payloads is not None and previous_receipt is not None
                and previous_receipt.phase == "bulk_ids_intent"
                and previous_receipt.prior_entity_state_fingerprints != prior_entity_states):
            raise GraphOperationError("graph_write_intent_prior_state_changed")
        intended_entity_states: tuple[tuple[str, str | None], ...] = ()
        if final_node_payloads is not None:
            payload_by_id = {str(item.get("uuid")): item for item in final_node_payloads}
            if len(payload_by_id) != len(final_node_payloads) or set(payload_by_id) != set(entity_ids):
                raise GraphOperationError("graph_write_intent_identifiers_ambiguous")
            intended_entity_states = tuple(sorted((
                entity_id,
                _entity_state_fingerprint(
                    payload_by_id[entity_id], context.embedding.dimensions, writer_intended=True,
                ),
            ) for entity_id in entity_ids))
        prior_edges = await EntityEdge.get_by_uuids(driver, list(fact_ids))
        prior_by_id = {str(item.uuid): item for item in prior_edges}
        if set(prior_by_id) - set(fact_ids):
            raise GraphOperationError("graph_write_intent_prior_state_invalid")
        states: list[ExactFactState] = []
        support: list[ExactFactSupport] = []
        for edge in facts:
            fact_id = str(edge.uuid)
            previous = prior_by_id.get(fact_id)
            if previous is not None:
                await previous.load_fact_embedding(driver)
                states.append(_fact_state(previous, context.embedding.dimensions))
            else:
                states.append(ExactFactState(
                    fact_id=fact_id, source_node_id=str(edge.source_node_uuid),
                    target_node_id=str(edge.target_node_uuid), episode_ids=(),
                    state_fingerprint=hashlib.sha256(b"null").hexdigest(), existed=False,
                ))
            desired = tuple(sorted(str(value) for value in edge.episodes))
            if len(set(desired)) != len(desired):
                raise GraphOperationError("graph_write_intent_support_invalid")
            adds_episode_support = str(request.episode_id) in desired
            invalidates_existing_fact = previous is not None and (
                previous.valid_at != edge.valid_at
                or previous.invalid_at != edge.invalid_at
                or previous.expired_at != edge.expired_at
            )
            if (not desired or not (adds_episode_support or invalidates_existing_fact)
                    or not set(desired) <= set(context.partition_episode_ids)):
                raise GraphOperationError("graph_write_intent_support_invalid")
            support.append(ExactFactSupport(
                fact_id=fact_id, episode_ids=desired,
                source_node_id=str(edge.source_node_uuid), target_node_id=str(edge.target_node_uuid),
                valid_at=edge.valid_at, invalid_at=edge.invalid_at, expired_at=edge.expired_at,
                state_fingerprint=_fact_state(edge, context.embedding.dimensions).state_fingerprint,
            ))

        desired_support_digest = hashlib.sha256(json.dumps(
            [(item.fact_id, item.episode_ids, item.source_node_id, item.target_node_id,
              str(item.valid_at), str(item.invalid_at), str(item.expired_at), item.state_fingerprint)
             for item in support],
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        receipt = GraphWriteReceipt(
            operation_id=context.operation_id,
            lease_token=context.lease_token,
            episode_id=request.episode_id,
            group_id=request.group_id,
            mapping_revision=request.mapping_revision,
            desired_support_digest=desired_support_digest,
            phase="bulk_write_intent" if final_node_payloads is not None else "bulk_ids_intent",
            episode_state_fingerprint=_episode_state_fingerprint(episodes[0]),
            prior_episode_state_fingerprint=shell_receipt.episode_state_fingerprint,
            entity_ids=entity_ids,
            intended_entity_state_fingerprints=intended_entity_states,
            prior_entity_state_fingerprints=prior_entity_states,
            mention_ids=mention_ids,
            fact_ids=fact_ids,
            existing_fact_states=tuple(states),
            intended_fact_support=tuple(support),
            canonical_bindings=_receipt_bindings(request.canonical_bindings),
        )
        try:
            await context.validate_partition("upsert", str(request.episode_id))
            await context.record_write_intent(receipt)
        except Exception:
            raise GraphOperationError("graph_write_intent_unavailable") from None
        latest_receipt[0] = receipt
        if final_node_payloads is not None:
            # Recheck after durable intent and immediately before SafeSession flushes captured calls.
            await context.validate_partition("upsert", str(request.episode_id))

    async def _validate_partition(
        self, context: OperationAuthorization,
        operation: Literal["search", "upsert", "delete", "reconcile"], episode_id: str | None = None,
    ) -> None:
        """Prove owner readiness and inventory, allowing only this authorized target to be absent."""
        # Owner validation checks full canonical support and cleanup state, not recent history.
        await context.validate_partition(operation, episode_id)
        driver = await self._partition_driver(context.group_id)
        try:
            records, _, _ = await driver.execute_query(
                "MATCH (episode:Episodic {group_id: $group_id}) "
                "RETURN episode.uuid AS episode_id ORDER BY episode.uuid LIMIT $limit",
                group_id=context.group_id,
                limit=MAX_SUPPORT + 1,
            )
        except Exception as exc:
            raise _safe_error(exc) from None
        if not isinstance(records, list) or len(records) > MAX_SUPPORT:
            raise GraphOperationError("graph_partition_inventory_invalid")
        graph_episode_ids = {row.get("episode_id") for row in records if isinstance(row, dict)}
        expected_episode_ids = set(context.partition_episode_ids)
        inventory_matches = graph_episode_ids == expected_episode_ids
        if operation in {"upsert", "delete", "reconcile"} and episode_id is not None:
            inventory_matches = inventory_matches or (
                episode_id in expected_episode_ids
                and graph_episode_ids == expected_episode_ids - {episode_id}
            )
        if len(graph_episode_ids) != len(records) or not inventory_matches:
            raise GraphOperationError("graph_partition_inventory_mismatch")

    async def _ensure_canonical_nodes(
        self, request: EpisodeRequest, context: OperationAuthorization, driver: Any,
        write_started: list[bool], latest_receipt: list[GraphWriteReceipt | None],
    ) -> tuple[str, ...]:
        """Journal and materialize source-proved canonical seeds, then verify each exact UUID."""
        from graphiti_core.nodes import EntityNode

        verified: list[str] = []
        for binding in request.canonical_bindings:
            try:
                current = await EntityNode.get_by_uuid(driver, binding.graph_entity_uuid)
            except Exception as exc:
                if type(exc).__name__ != "NodeNotFoundError":
                    raise _safe_error(exc) from None
                current = None
            if current is not None:
                if current.group_id != request.group_id:
                    raise GraphOperationError("graph_canonical_entity_partition_mismatch")
                await current.load_name_embedding(driver)
                if (binding.prior_node_state_fingerprint is None
                        or _entity_state_fingerprint(current, context.embedding.dimensions)
                        != binding.prior_node_state_fingerprint):
                    raise GraphOperationError("graph_canonical_entity_state_changed")
                verified.append(binding.graph_entity_uuid)
                continue
            if (binding.node_name is None or not binding.node_name.strip()
                    or not any(field == "name" for field, _, _ in binding.field_support)):
                raise GraphOperationError("graph_canonical_entity_seed_unavailable")
            entity = EntityNode(
                uuid=binding.graph_entity_uuid,
                group_id=request.group_id,
                name=binding.node_name,
                summary=binding.node_summary or "",
                attributes={},
                labels=["Entity"],
            )
            intended = _entity_state_fingerprint(
                entity, context.embedding.dimensions, writer_intended=True,
            )
            receipt = GraphWriteReceipt(
                operation_id=context.operation_id,
                lease_token=context.lease_token,
                episode_id=request.episode_id,
                group_id=request.group_id,
                mapping_revision=request.mapping_revision,
                desired_support_digest=intended,
                phase="canonical_node_write_intent",
                entity_ids=(binding.graph_entity_uuid,),
                intended_entity_state_fingerprints=((binding.graph_entity_uuid, intended),),
                prior_entity_state_fingerprints=((binding.graph_entity_uuid, False, None),),
                canonical_bindings=_receipt_bindings((binding,)),
            )
            try:
                await context.record_write_intent(receipt)
            except Exception:
                raise GraphOperationError("graph_write_intent_unavailable") from None
            latest_receipt[0] = receipt
            try:
                write_started[0] = True
                await entity.save(driver)
                saved = await EntityNode.get_by_uuid(driver, binding.graph_entity_uuid)
                await saved.load_name_embedding(driver)
            except (TimeoutError, asyncio.CancelledError):
                raise GraphOperationUnknown("graph_canonical_entity_outcome_unknown", receipt) from None
            except Exception as exc:
                raise GraphOperationUnknown("graph_canonical_entity_outcome_unknown", receipt) from None
            if (saved.group_id != request.group_id
                    or _entity_state_fingerprint(saved, context.embedding.dimensions) != intended):
                raise GraphOperationUnknown("graph_canonical_entity_outcome_unknown", receipt) from None
            verified.append(binding.graph_entity_uuid)
        return tuple(verified)

    async def upsert_episode(
        self, request: EpisodeRequest, context: OperationAuthorization, gateway: ModelGateway,
    ) -> dict[str, Any]:
        """Upsert a stable episode under its full source/evidence fence and durable write receipts.

        T3 commits mapping and intent first. Falkor writes span multiple
        non-atomic statements; a timeout returns unknown and must be reconciled
        by exact UUID before retrying.
        """
        if (request.group_id != context.group_id
                or str(request.episode_id) not in context.partition_episode_ids
                or not set(request.evidence) <= set(context.evidence)):
            raise GraphOperationError("graph_episode_outside_authorized_support")
        graph_write_started = [False]
        latest_receipt: list[GraphWriteReceipt | None] = [None]
        shell_receipt: GraphWriteReceipt | None = None
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await self._validate_partition(context, "upsert", str(request.episode_id))
                    await context.authorize("reasoning")
                    from graphiti_core.nodes import EpisodicNode, EpisodeType

                    driver = await self._partition_driver(request.group_id)
                    verified_binding_ids = await self._ensure_canonical_nodes(
                        request, context, driver, graph_write_started, latest_receipt,
                    )

                    try:
                        existing = await EpisodicNode.get_by_uuid(driver, str(request.episode_id))
                    except Exception as exc:
                        if type(exc).__name__ != "NodeNotFoundError":
                            raise
                    else:
                        if existing.group_id != request.group_id:
                            raise GraphOperationError("graph_episode_id_partition_conflict")
                        # Graphiti writes several non-atomic statements; never re-extract an existing ID.
                        raise GraphOperationUnknown("graph_episode_existing_requires_reconciliation")
                    episode = EpisodicNode(
                        uuid=str(request.episode_id), name=request.name, group_id=request.group_id,
                        source=EpisodeType.text,
                        source_description="BBD-OS evidence-backed temporal episode",
                        content=request.content, valid_at=request.reference_time.astimezone(UTC),
                        created_at=datetime.now(UTC), entity_edges=[],
                    )
                    # Stable-ID shell exists before Graphiti extraction so partial writes remain identifiable.
                    shell_receipt = GraphWriteReceipt(
                        operation_id=context.operation_id,
                        lease_token=context.lease_token,
                        episode_id=request.episode_id,
                        group_id=request.group_id,
                        mapping_revision=request.mapping_revision,
                        desired_support_digest=hashlib.sha256(b"[]").hexdigest(),
                        phase="shell_write_intent",
                        episode_state_fingerprint=_episode_state_fingerprint(episode),
                        canonical_bindings=_receipt_bindings(request.canonical_bindings),
                    )
                    try:
                        await context.record_write_intent(shell_receipt)
                    except Exception:
                        raise GraphOperationError("graph_write_intent_unavailable") from None
                    latest_receipt[0] = shell_receipt
                    driver._write_intent_hook = partial(
                        self._record_bulk_write_intent, request, context, shell_receipt, latest_receipt,
                    )
                    graphiti = self._graphiti(context, gateway, driver)
                    graph_write_started[0] = True
                    await episode.save(driver)
                    result = await graphiti.add_episode(
                        name=request.name, episode_body=request.content, source=EpisodeType.text,
                        source_description="BBD-OS evidence-backed temporal episode",
                        reference_time=request.reference_time.astimezone(UTC),
                        group_id=request.group_id, uuid=str(request.episode_id),
                        previous_episode_uuids=list(context.history_episode_ids),
                    )
                    return {
                        "episode_id": str(request.episode_id), "mapping_revision": request.mapping_revision,
                        "canonical_entity_ids": request.canonical_entity_ids,
                        "verified_canonical_bindings": tuple(
                            (item.canonical_entity_id, item.graph_entity_uuid,
                             item.canonical_revision, item.mapping_revision)
                            for item in request.canonical_bindings
                        ),
                        "verified_canonical_graph_entity_ids": verified_binding_ids,
                        "graph_entity_ids": tuple(node.uuid for node in getattr(result, "nodes", [])),
                        "outcome": "succeeded",
                    }
            except (TimeoutError, asyncio.CancelledError):
                raise GraphOperationUnknown("graph_episode_outcome_unknown", latest_receipt[0]) from None
            except GraphOperationUnknown as exc:
                raise GraphOperationUnknown(str(exc), exc.receipt or latest_receipt[0]) from None
            except GraphOperationError:
                if graph_write_started[0]:
                    raise GraphOperationUnknown("graph_episode_outcome_unknown", latest_receipt[0]) from None
                raise
            except Exception as exc:
                error = _safe_error(exc)
                if graph_write_started[0] and isinstance(error, GraphOperationUnknown):
                    raise GraphOperationUnknown(str(error), latest_receipt[0]) from None
                raise error from None

    async def search_at(
        self, query: str, valid_at: datetime, context: OperationAuthorization,
        gateway: ModelGateway, *, limit: int = MAX_RESULTS,
    ) -> list[TemporalSearchResult]:
        """Search one source-generation partition and canonicalize every graph result."""
        if (not query.strip() or len(query.encode()) > MAX_QUERY_BYTES or valid_at.tzinfo is None
                or not 1 <= limit <= MAX_RESULTS):
            raise ValueError("Temporal graph search is invalid or exceeds its bounds")
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await self._validate_partition(context, "search")
                    if not context.partition_episode_ids:
                        return []
                    await context.authorize("embedding")
                    driver = await self._partition_driver(context.group_id)
                    graphiti = self._graphiti(context, gateway, driver)
                    from graphiti_core.search.search_filters import (
                        ComparisonOperator, DateFilter, SearchFilters,
                    )

                    instant = valid_at.astimezone(UTC)

                    raw = await graphiti.search(
                        query, group_ids=[context.group_id], num_results=limit,
                        search_filter=SearchFilters(
                            valid_at=[[
                                DateFilter(
                                    date=instant,
                                    comparison_operator=ComparisonOperator.less_than_equal,
                                ),
                            ]],
                            invalid_at=[[
                                DateFilter(
                                    date=instant,
                                    comparison_operator=ComparisonOperator.greater_than,
                                ),
                            ], [
                                DateFilter(comparison_operator=ComparisonOperator.is_null),
                            ]],
                        ),
                    )
                    if len(raw) > MAX_CANDIDATES:
                        raise GraphOperationError("graph_search_candidate_limit")
                    canonical = await context.canonicalize(raw)
                    allowed_episode_ids = set(context.partition_episode_ids)
                    if (len(canonical) > limit or any(
                        not item.episode_ids or not set(item.episode_ids) <= allowed_episode_ids
                        for item in canonical
                    )):
                        raise GraphOperationError("graph_result_outside_authorized_partition")
                    return canonical
            except (TimeoutError, asyncio.CancelledError):
                raise GraphOperationUnknown("graph_search_outcome_unknown") from None
            except GraphOperationError:
                raise
            except Exception as exc:
                raise _safe_error(exc) from None

    async def inspect_write_receipt(
        self, receipt: GraphWriteReceipt | tuple[GraphWriteReceipt, ...],
        context: OperationAuthorization,
        actions: tuple[CanonicalNodeRecoveryAction, ...] = (),
    ) -> GraphReceiptInspection:
        """Inspect one normal receipt or an owner-approved bounded recovery aggregate by exact IDs."""
        receipts = (receipt,) if isinstance(receipt, GraphWriteReceipt) else receipt
        if not 1 <= len(receipts) <= MAX_RECOVERY_WITNESSES:
            raise GraphOperationError("graph_receipt_outside_authorized_partition")
        node_recovery = bool(actions) or any(
            item.phase == "canonical_node_write_intent"
            or (item.phase == "cleanup_write_intent" and not item.prior_episode_state_fingerprint)
            for item in receipts
        )
        receipt_episode_ids = {str(item.episode_id) for item in receipts}
        if (len(receipt_episode_ids) != 1
                or any(item.group_id != context.group_id or item.operation_id != context.operation_id
                       or item.lease_token != context.lease_token
                       for item in receipts)
                or not receipt_episode_ids <= set(context.partition_episode_ids)
                or (node_recovery and (context.authorize_node_recovery is None or not actions))
                or (not node_recovery and len(receipts) != 1 and context.receipt_aggregate is None)):
            raise GraphOperationError("graph_receipt_outside_authorized_partition")
        episode_id = next(iter(receipt_episode_ids))
        entity_ids = tuple(sorted({value for item in receipts for value in item.entity_ids} | {
            binding.graph_entity_uuid for item in receipts for binding in item.canonical_bindings
        }))
        recoverable_bulk_ids = {
            entity_id for item in receipts if item.phase == "bulk_write_intent"
            for entity_id, _, state in item.prior_entity_state_fingerprints
            if any(candidate_id == entity_id and candidate_state is not None
                   for candidate_id, candidate_state in item.intended_entity_state_fingerprints)
        }
        canonical_receipt_ids = {
            binding.graph_entity_uuid for item in receipts for binding in item.canonical_bindings
        }
        mention_ids = tuple(sorted({value for item in receipts for value in item.mention_ids}))
        fact_ids = tuple(sorted({value for item in receipts for value in item.fact_ids}))
        ids = (*entity_ids, *mention_ids, *fact_ids)
        if (len(ids) > MAX_SUPPORT * 3 or any(not _UUID.fullmatch(value) for value in ids)
                or (node_recovery and not {item.graph_entity_uuid for item in actions}
                    <= (canonical_receipt_ids | recoverable_bulk_ids))):
            raise GraphOperationError("graph_receipt_outside_authorized_partition")
        if not node_recovery and not (
            (receipts[0].operation_id == context.operation_id
             and receipts[0].lease_token == context.lease_token)
            or context.episode_receipt == receipts[0]
        ):
            raise GraphOperationError("graph_receipt_outside_authorized_partition")
        if self.state != GraphState.READY or self._driver is None:
            raise GraphOperationError("graph_unavailable")
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await _authorize_receipt_witnesses(context, receipts, "node")
                    if node_recovery:
                        await context.authorize_node_recovery(receipts, actions)
                    await self._validate_partition(context, "reconcile", episode_id)
                    from graphiti_core.edges import EntityEdge, EpisodicEdge
                    from graphiti_core.nodes import EntityNode, EpisodicNode

                    driver = await self._partition_driver(context.group_id)
                    episodes = await EpisodicNode.get_by_uuids(driver, [episode_id])
                    entities = await EntityNode.get_by_uuids(driver, list(entity_ids), context.group_id) if entity_ids else []
                    for entity in entities:
                        await entity.load_name_embedding(driver)
                    mentions = await EpisodicEdge.get_by_uuids(driver, list(mention_ids)) if mention_ids else []
                    facts = await EntityEdge.get_by_uuids(driver, list(fact_ids)) if fact_ids else []
                    for fact in facts:
                        await fact.load_fact_embedding(driver)
                    link_records, _, _ = await driver.execute_query(
                        "MATCH (source)-[edge]-(entity:Entity) WHERE entity.uuid IN $entity_ids "
                        "RETURN DISTINCT edge.uuid AS edge_id, type(edge) AS relationship_type, "
                        "startNode(edge).uuid AS source_node_id, endNode(edge).uuid AS target_node_id, "
                        "edge.episodes AS episode_ids ORDER BY edge.uuid LIMIT $limit",
                        entity_ids=list(entity_ids), limit=MAX_SUPPORT + 1,
                    ) if entity_ids else ([], [], None)
                    incident_links: list[ExactGraphLink] = []
                    if not isinstance(link_records, list) or len(link_records) > MAX_SUPPORT:
                        raise GraphOperationError("graph_receipt_link_inventory_invalid")
                    for row in link_records:
                        support_ids = row.get("episode_ids") or () if isinstance(row, dict) else ()
                        if (not isinstance(row, dict) or not isinstance(support_ids, list | tuple)
                                or not isinstance(row.get("relationship_type"), str)):
                            raise GraphOperationError("graph_receipt_link_inventory_invalid")
                        incident_links.append(ExactGraphLink(
                            edge_id=str(row.get("edge_id")),
                            relationship_type=row["relationship_type"],
                            source_node_id=str(row.get("source_node_id")),
                            target_node_id=str(row.get("target_node_id")),
                            episode_ids=tuple(sorted(str(value) for value in support_ids)),
                        ))
                    mention_links = tuple(sorted((ExactGraphLink(
                        edge_id=str(item.uuid), relationship_type="MENTIONS",
                        source_node_id=str(item.source_node_uuid),
                        target_node_id=str(item.target_node_uuid), episode_ids=(),
                    ) for item in mentions), key=lambda item: item.edge_id))
                    if ((episodes and episodes[0].group_id != context.group_id)
                            or len(episodes) > 1
                            or len({str(item.uuid) for item in entities}) != len(entities)
                            or len({str(item.uuid) for item in mentions}) != len(mentions)
                            or len({str(item.uuid) for item in facts}) != len(facts)
                            or {str(item.uuid) for item in entities} - set(entity_ids)
                            or {str(item.uuid) for item in mentions} - set(mention_ids)
                            or {str(item.uuid) for item in facts} - set(fact_ids)
                            or any(str(item.source_node_uuid) != episode_id
                                   or str(item.target_node_uuid) not in set(entity_ids) for item in mentions)
                            or any(not set(str(value) for value in item.episodes)
                                   <= set(context.partition_episode_ids) for item in facts)
                            or any(not set(item.episode_ids) <= set(context.partition_episode_ids)
                                   for item in incident_links)
                            or len({item.edge_id for item in incident_links}) != len(incident_links)
                            or any(item.group_id != context.group_id for item in (*entities, *mentions, *facts))):
                        raise GraphOperationError("graph_receipt_partition_mismatch")
                    return GraphReceiptInspection(
                        episode_present=bool(episodes),
                        episode_state_fingerprint=(
                            _episode_state_fingerprint(episodes[0]) if episodes else None
                        ),
                        entity_ids_present=tuple(sorted(str(item.uuid) for item in entities)),
                        current_entity_state_fingerprints=tuple(sorted(
                            (str(item.uuid), _entity_state_fingerprint(item, context.embedding.dimensions))
                            for item in entities
                        )),
                        incident_links=tuple(incident_links),
                        mention_ids_present=tuple(sorted(str(item.uuid) for item in mentions)),
                        current_mention_links=mention_links,
                        fact_ids_present=tuple(sorted(str(item.uuid) for item in facts)),
                        current_fact_states=tuple(sorted(
                            (_fact_state(item, context.embedding.dimensions) for item in facts),
                            key=lambda item: item.fact_id,
                        )),
                        current_fact_recovery_fingerprints=tuple(sorted(
                            (str(item.uuid), _fact_recovery_fingerprint(item, context.embedding.dimensions))
                            for item in facts
                        )),
                    )
            except GraphOperationError:
                raise
            except Exception as exc:
                raise _safe_error(exc) from None

    async def reconcile_canonical_nodes(
        self, receipts: tuple[GraphWriteReceipt, ...],
        actions: tuple[CanonicalNodeRecoveryAction, ...], context: OperationAuthorization,
    ) -> CanonicalNodeRecoveryOutcome:
        """Apply owner-authorized node recovery after exact inventory and fingerprint checks.

        Receipt witnesses and actions have independent bounds; their physical
        effect union remains capped at 100 IDs. Typed rebuild deletion requires
        owner-committed dependent rebuild schedules and the complete exact incident
        inventory. Missing objects require own absence evidence or certified
        cross-operation absence, journaled under this operation's live lease.
        """
        if (self.state != GraphState.READY or self._driver is None
                or context.authorize_node_recovery is None
                or not 1 <= len(receipts) <= MAX_RECOVERY_WITNESSES or not actions
                or len(actions) > MAX_SUPPORT
                or len({item.graph_entity_uuid for item in actions}) != len(actions)):
            raise GraphOperationError("graph_node_recovery_unavailable")
        episode_ids = {str(item.episode_id) for item in receipts}
        mapping_revisions = {item.mapping_revision for item in receipts}
        authorized_node_ids = {
            binding.graph_entity_uuid for receipt in receipts for binding in receipt.canonical_bindings
        }
        authorized_node_ids.update(
            entity_id for receipt in receipts if receipt.phase == "bulk_write_intent"
            for entity_id, _, state in receipt.prior_entity_state_fingerprints
            if any(candidate_id == entity_id and candidate_state is not None
                   for candidate_id, candidate_state in receipt.intended_entity_state_fingerprints)
        )
        total_ids = {
            value for item in receipts
            for value in (*item.entity_ids, *item.mention_ids, *item.fact_ids)
        }
        total_ids.update(link.edge_id for item in receipts for link in item.incident_links)
        action_ids = {item.graph_entity_uuid for item in actions}
        expected_link_count = sum(len(item.expected_incident_links) for item in actions)
        if (len(episode_ids) != 1 or not episode_ids <= set(context.partition_episode_ids)
                or mapping_revisions != {context.mapping_revision}
                or any(item.group_id != context.group_id or item.operation_id != context.operation_id
                       for item in receipts)
                or len(total_ids | action_ids | {
                    link.edge_id for action in actions for link in action.expected_incident_links
                }) > MAX_SUPPORT or expected_link_count > MAX_SUPPORT
                or not action_ids <= authorized_node_ids):
            raise GraphOperationError("graph_node_recovery_outside_authorized_partition")
        episode_id = next(iter(episode_ids))
        retained: list[str] = []
        deleted: list[str] = []
        replaced: list[str] = []
        unwritten: list[str] = []
        unresolved: list[str] = []
        retained_receipts = list(receipts)

        async def incident_links(driver: Any, entity_id: str) -> tuple[ExactGraphLink, ...]:
            """Read the complete bounded exact-ID link inventory for one entity node."""
            rows, _, _ = await driver.execute_query(
                "MATCH (source)-[edge]-(entity:Entity {uuid: $entity_id}) "
                "RETURN edge.uuid AS edge_id, type(edge) AS relationship_type, "
                "startNode(edge).uuid AS source_node_id, endNode(edge).uuid AS target_node_id, "
                "edge.episodes AS episode_ids ORDER BY edge.uuid LIMIT $limit",
                entity_id=entity_id, limit=MAX_SUPPORT + 1,
            )
            if not isinstance(rows, list) or len(rows) > MAX_SUPPORT:
                raise GraphOperationError("graph_node_link_inventory_invalid")
            links: list[ExactGraphLink] = []
            for row in rows:
                support = row.get("episode_ids") or () if isinstance(row, dict) else ()
                if (not isinstance(row, dict) or not isinstance(support, list | tuple)
                        or not isinstance(row.get("relationship_type"), str)):
                    raise GraphOperationError("graph_node_link_inventory_invalid")
                links.append(ExactGraphLink(
                    edge_id=str(row.get("edge_id")), relationship_type=row["relationship_type"],
                    source_node_id=str(row.get("source_node_id")),
                    target_node_id=str(row.get("target_node_id")),
                    episode_ids=tuple(sorted(str(value) for value in support)),
                ))
            if len({item.edge_id for item in links}) != len(links):
                raise GraphOperationError("graph_node_link_inventory_invalid")
            return tuple(links)

        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await _authorize_receipt_witnesses(context, receipts, "node")
                    await context.authorize_node_recovery(receipts, actions)
                    await self._validate_partition(context, "reconcile", episode_id)
                    from graphiti_core.nodes import EntityNode

                    driver = await self._partition_driver(context.group_id)
                    for action in actions:
                        entity = None
                        try:
                            entity = await EntityNode.get_by_uuid(driver, action.graph_entity_uuid)
                            await entity.load_name_embedding(driver)
                        except Exception as exc:
                            if type(exc).__name__ != "NodeNotFoundError":
                                raise _safe_error(exc) from None
                        links = await incident_links(driver, action.graph_entity_uuid)
                        if (links != action.expected_incident_links
                                and not (entity is None and not links and action.action == "delete_for_rebuild")):
                            unresolved.append(action.graph_entity_uuid)
                            continue
                        current_fingerprint = (
                            _entity_state_fingerprint(entity, context.embedding.dimensions)
                            if entity is not None else None
                        )
                        if current_fingerprint != action.expected_current_state_fingerprint:
                            unresolved.append(action.graph_entity_uuid)
                            continue
                        if action.action == "retain":
                            if entity is None:
                                unwritten.append(action.graph_entity_uuid)
                            else:
                                retained.append(action.graph_entity_uuid)
                            continue
                        if action.action in {"delete_orphan", "delete_for_rebuild"} and entity is None:
                            prior_node_state = next((
                                (exists, fingerprint)
                                for receipt in receipts
                                for candidate_id, exists, fingerprint in receipt.prior_entity_state_fingerprints
                                if candidate_id == action.graph_entity_uuid
                            ), None)
                            cleanup_deleted = any(
                                receipt.phase == "cleanup_write_intent"
                                and (action.graph_entity_uuid, None)
                                in receipt.intended_entity_state_fingerprints
                                and (receipt.rebuild_absence_observed == "node"
                                     or any(candidate_id == action.graph_entity_uuid and exists
                                            and fingerprint is not None
                                            for candidate_id, exists, fingerprint
                                            in receipt.prior_entity_state_fingerprints))
                                for receipt in receipts
                            )
                            if cleanup_deleted:
                                deleted.append(action.graph_entity_uuid)
                            elif prior_node_state is not None and not prior_node_state[0]:
                                unwritten.append(action.graph_entity_uuid)
                            elif await _authorize_cross_rebuild_absence(
                                context, "node", action.graph_entity_uuid, episode_id=UUID(str(episode_id)),
                            ):
                                deleted.append(action.graph_entity_uuid)
                            else:
                                unresolved.append(action.graph_entity_uuid)
                            if action.action == "delete_for_rebuild" and not await _incident_ids_absent(
                                driver, [link.edge_id for item in receipts
                                         if action.graph_entity_uuid in item.entity_ids
                                         for link in item.incident_links]
                                + [link.edge_id for link in action.expected_incident_links],
                            ):
                                deleted[:] = [value for value in deleted if value != action.graph_entity_uuid]
                                unwritten[:] = [value for value in unwritten if value != action.graph_entity_uuid]
                                unresolved.append(action.graph_entity_uuid)
                            continue
                        if action.action == "delete_orphan" and links:
                            unresolved.append(action.graph_entity_uuid)
                            continue
                        binding = action.replacement_binding or next((
                            item for receipt in receipts for item in receipt.canonical_bindings
                            if item.graph_entity_uuid == action.graph_entity_uuid
                        ), None)
                        candidate = action.replacement_candidate
                        if (candidate is not None and (
                            candidate.group_id != context.group_id
                            or episode_id in candidate.support_episode_ids
                            or not set(candidate.support_episode_ids) <= set(context.partition_episode_ids)
                        )):
                            unresolved.append(action.graph_entity_uuid)
                            continue
                        if action.action == "replace_from_current_support" and binding is None and candidate is None:
                            unresolved.append(action.graph_entity_uuid)
                            continue
                        replacement = None
                        intended_fingerprint = None
                        if action.action == "replace_from_current_support":
                            if entity is None and action.replacement_created_at is None:
                                unresolved.append(action.graph_entity_uuid)
                                continue
                            node_name = binding.node_name if binding is not None else candidate.node_name
                            node_summary = (
                                (binding.node_summary or "") if binding is not None else candidate.node_summary
                            )
                            replacement = EntityNode(
                                uuid=action.graph_entity_uuid, group_id=context.group_id,
                                name=node_name, summary=node_summary, attributes={},
                                # Falkor's SET n: labels are additive, so preserve the exact current physical labels.
                                labels=(list(entity.labels) if entity is not None else ["Entity"]),
                                created_at=(entity.created_at if entity is not None
                                            else action.replacement_created_at),
                            )
                            intended_fingerprint = _entity_state_fingerprint(
                                replacement, context.embedding.dimensions, writer_intended=True,
                            )
                        cleanup_receipt = GraphWriteReceipt(
                            operation_id=context.operation_id, lease_token=context.lease_token,
                            episode_id=UUID(episode_id), group_id=context.group_id,
                            mapping_revision=context.mapping_revision,
                            desired_support_digest=hashlib.sha256(json.dumps(
                                [action.graph_entity_uuid, action.action, current_fingerprint,
                                 intended_fingerprint], separators=(",", ":"),
                            ).encode()).hexdigest(),
                            phase="cleanup_write_intent",
                            entity_ids=(action.graph_entity_uuid,),
                            intended_entity_state_fingerprints=((action.graph_entity_uuid, intended_fingerprint),),
                            prior_entity_state_fingerprints=((
                                action.graph_entity_uuid, entity is not None, current_fingerprint,
                            ),),
                            canonical_bindings=_receipt_bindings((binding,)) if binding is not None else (),
                            candidate_field_support=(tuple(
                                (action.graph_entity_uuid, field, digest, support_ids)
                                for field, digest, support_ids in candidate.field_support
                            ) if candidate is not None else ()),
                            incident_links=action.expected_incident_links,
                        )
                        try:
                            await context.record_write_intent(cleanup_receipt)
                            retained_receipts.append(cleanup_receipt)
                            await context.authorize_node_recovery(tuple(retained_receipts), actions)
                            latest = None
                            try:
                                latest = await EntityNode.get_by_uuid(driver, action.graph_entity_uuid)
                                await latest.load_name_embedding(driver)
                            except Exception as exc:
                                if type(exc).__name__ != "NodeNotFoundError":
                                    raise
                            if ((_entity_state_fingerprint(latest, context.embedding.dimensions)
                                 if latest is not None else None)
                                    != current_fingerprint
                                    or await incident_links(driver, action.graph_entity_uuid)
                                    != action.expected_incident_links):
                                unresolved.append(action.graph_entity_uuid)
                                continue
                            if action.action in {"delete_orphan", "delete_for_rebuild"}:
                                if action.action == "delete_for_rebuild":
                                    if not await _delete_rebuild_node(driver, action, context.group_id):
                                        unresolved.append(action.graph_entity_uuid)
                                        continue
                                else:
                                    await EntityNode.delete_by_uuids(driver, [action.graph_entity_uuid])
                                try:
                                    await EntityNode.get_by_uuid(driver, action.graph_entity_uuid)
                                except Exception as exc:
                                    if type(exc).__name__ != "NodeNotFoundError":
                                        raise
                                    deleted.append(action.graph_entity_uuid)
                                else:
                                    unresolved.append(action.graph_entity_uuid)
                            else:
                                assert replacement is not None
                                await replacement.save(driver)
                                # Seed replacement deliberately drops old vectors derived from deleted support.
                                records, _, _ = await driver.execute_query(
                                    "MATCH (entity:Entity {uuid: $entity_id, group_id: $group_id}) "
                                    "SET entity.name_embedding = null RETURN entity.uuid AS entity_id",
                                    entity_id=action.graph_entity_uuid, group_id=context.group_id,
                                )
                                if (not isinstance(records, list) or len(records) != 1
                                        or not isinstance(records[0], dict)
                                        or records[0].get("entity_id") != action.graph_entity_uuid):
                                    unresolved.append(action.graph_entity_uuid)
                                    continue
                                saved = await EntityNode.get_by_uuid(driver, action.graph_entity_uuid)
                                await saved.load_name_embedding(driver)
                                if (_entity_state_fingerprint(saved, context.embedding.dimensions)
                                        == intended_fingerprint
                                        and saved.group_id == context.group_id
                                        and await incident_links(driver, action.graph_entity_uuid)
                                        == action.expected_incident_links):
                                    replaced.append(action.graph_entity_uuid)
                                else:
                                    unresolved.append(action.graph_entity_uuid)
                        except Exception:
                            unresolved.append(action.graph_entity_uuid)
                    return CanonicalNodeRecoveryOutcome(
                        converged=not unresolved,
                        retained_ids=tuple(sorted(retained)), deleted_ids=tuple(sorted(deleted)),
                        replaced_ids=tuple(sorted(replaced)), unwritten_ids=tuple(sorted(unwritten)),
                        unresolved_ids=tuple(sorted(set(unresolved))),
                        reason="converged" if not unresolved else "exact_node_state_unresolved",
                    )
            except GraphOperationError:
                raise
            except (TimeoutError, asyncio.CancelledError):
                return CanonicalNodeRecoveryOutcome(
                    converged=False, retained_ids=tuple(sorted(retained)), deleted_ids=tuple(sorted(deleted)),
                    replaced_ids=tuple(sorted(replaced)), unwritten_ids=tuple(sorted(unwritten)),
                    unresolved_ids=tuple(sorted(set(unresolved) | action_ids)),
                    reason="graph_node_recovery_outcome_unknown",
                )
            except Exception as exc:
                logger.warning("Graph canonical-node recovery incomplete category=%s", type(exc).__name__)
                return CanonicalNodeRecoveryOutcome(
                    converged=False, retained_ids=tuple(sorted(retained)), deleted_ids=tuple(sorted(deleted)),
                    replaced_ids=tuple(sorted(replaced)), unwritten_ids=tuple(sorted(unwritten)),
                    unresolved_ids=tuple(sorted(set(unresolved) | action_ids)),
                    reason="graph_node_recovery_outcome_unknown",
                )

    async def reconcile_fact_recovery(
        self, receipts: tuple[GraphWriteReceipt, ...],
        actions: tuple[ExactFactRecoveryAction, ...], context: OperationAuthorization,
    ) -> ExactFactRecoveryOutcome:
        """Apply fresh owner-authorized fact corrections with exact vector-aware pre/post fences.

        Bound original receipt witnesses independently from the 100-action/effect
        cap and certify any complete journal aggregate before graph inspection.
        Typed rebuild deletion requires owner-committed dependent rebuild schedules.
        Exact absence is resolved before endpoint presence, allowing completed node
        rebuilds to remove facts while preserving original endpoint evidence.
        """
        if (self.state != GraphState.READY or self._driver is None
                or context.authorize_fact_recovery is None
                or not 1 <= len(receipts) <= MAX_RECOVERY_WITNESSES or not actions
                or len(actions) > MAX_SUPPORT
                or len({item.fact_id for item in actions}) != len(actions)
                or tuple(sorted(actions, key=lambda item: item.fact_id)) != actions):
            raise GraphOperationError("graph_fact_recovery_unavailable")
        episode_ids = {str(item.episode_id) for item in receipts}
        if (len(episode_ids) != 1 or not episode_ids <= set(context.partition_episode_ids)
                or any(item.group_id != context.group_id or item.operation_id != context.operation_id
                       or item.lease_token != context.lease_token
                       or item.mapping_revision != context.mapping_revision
                       or item.phase not in {"canonical_node_write_intent", "shell_write_intent", "bulk_ids_intent", "bulk_write_intent", "cleanup_write_intent"}
                       for item in receipts)):
            raise GraphOperationError("graph_fact_recovery_outside_authorized_partition")
        if [item.phase for item in receipts] != sorted(
            (item.phase for item in receipts),
            key=lambda phase: {"canonical_node_write_intent": 0, "shell_write_intent": 1,
                "bulk_ids_intent": 2, "bulk_write_intent": 3, "cleanup_write_intent": 4}[phase],
        ):
            raise GraphOperationError("graph_fact_recovery_receipt_invalid")
        episode_id = next(iter(episode_ids))
        absent_fingerprint = _ABSENT_FACT_FINGERPRINT
        proof_by_id: dict[str, tuple[str, str, bool]] = {}
        for receipt in receipts:
            prior_by_id = {item.fact_id: item for item in receipt.existing_fact_states}
            intended_by_id = {item.fact_id: item for item in receipt.intended_fact_support}
            for fact_id in set(prior_by_id) & set(intended_by_id):
                prior = prior_by_id[fact_id]
                intended = intended_by_id[fact_id]
                proof_by_id[fact_id] = (
                    prior.source_node_id, prior.target_node_id, prior.existed,
                )
                if (intended.source_node_id != prior.source_node_id
                        or intended.target_node_id != prior.target_node_id):
                    raise GraphOperationError("graph_fact_recovery_receipt_invalid")
        if any(action.fact_id not in proof_by_id
               or proof_by_id[action.fact_id][:2] != (action.source_node_id, action.target_node_id)
               or (action.replacement is not None and (
                   action.replacement.group_id != context.group_id
                   or episode_id in action.replacement.episode_ids
                   or not set(action.replacement.episode_ids) <= set(context.partition_episode_ids)
                   or (context.embedding.dimensions is not None
                       and len(action.replacement.fact_embedding) != context.embedding.dimensions)
               )) for action in actions):
            raise GraphOperationError("graph_fact_recovery_receipt_invalid")
        total_recovery_ids = {
            value for item in receipts
            for value in (*item.entity_ids, *item.mention_ids, *item.fact_ids,
                          *(link.edge_id for link in item.incident_links))
        } | {item.fact_id for item in actions}
        if len(total_recovery_ids) > MAX_SUPPORT:
            raise GraphOperationError("graph_fact_recovery_receipt_invalid")

        replaced: list[str] = []
        deleted: list[str] = []
        unresolved: list[str] = []
        retained_receipts = list(receipts)
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await _authorize_receipt_witnesses(context, receipts, "fact")
                    await context.authorize_fact_recovery(receipts, actions)
                    await self._validate_partition(context, "reconcile", episode_id)
                    from graphiti_core.edges import EntityEdge

                    driver = await self._partition_driver(context.group_id)
                    for action in actions:
                        current_rows = await EntityEdge.get_by_uuids(driver, [action.fact_id])
                        current_by_id = {str(item.uuid): item for item in current_rows}
                        if len(current_by_id) != len(current_rows):
                            unresolved.append(action.fact_id)
                            continue
                        current = current_by_id.get(action.fact_id)
                        if current is not None:
                            await current.load_fact_embedding(driver)
                            current_fingerprint = _fact_recovery_fingerprint(
                                current, context.embedding.dimensions,
                            )
                            if (current.group_id != context.group_id
                                    or str(current.source_node_uuid) != action.source_node_id
                                    or str(current.target_node_uuid) != action.target_node_id
                                    or not set(str(value) for value in current.episodes)
                                    <= set(context.partition_episode_ids)):
                                unresolved.append(action.fact_id)
                                continue
                        else:
                            current_fingerprint = None
                            recorded_delete = any(
                                receipt.phase == "cleanup_write_intent"
                                and any(item.fact_id == action.fact_id
                                        and item.state_fingerprint == absent_fingerprint
                                        and not item.episode_ids
                                        for item in receipt.intended_fact_support)
                                and any(item.fact_id == action.fact_id and item.existed
                                        for item in receipt.existing_fact_states)
                                for receipt in receipts
                            )
                            recorded_initial_absence = any(
                                any(item.fact_id == action.fact_id and not item.existed
                                    and item.source_node_id == action.source_node_id
                                    and item.target_node_id == action.target_node_id
                                    for item in receipt.existing_fact_states)
                                and any(item.fact_id == action.fact_id
                                        and item.source_node_id == action.source_node_id
                                        and item.target_node_id == action.target_node_id
                                        for item in receipt.intended_fact_support)
                                for receipt in receipts
                            )
                            if action.action in {"delete_unsupported", "delete_for_rebuild"} and (
                                recorded_delete or recorded_initial_absence
                                or await _authorize_cross_rebuild_absence(
                                        context, "fact", action.fact_id, episode_id=UUID(str(episode_id)),
                                        endpoints=(action.source_node_id, action.target_node_id),
                                    )
                            ):
                                deleted.append(action.fact_id)
                            else:
                                unresolved.append(action.fact_id)
                            continue

                        endpoint_ids = sorted({action.source_node_id, action.target_node_id})
                        endpoint_rows, _, _ = await driver.execute_query(
                            "MATCH (node:Entity) WHERE node.uuid IN $node_ids "
                            "RETURN node.uuid AS node_id, node.group_id AS group_id "
                            "ORDER BY node.uuid LIMIT $limit",
                            node_ids=endpoint_ids, limit=len(endpoint_ids) + 1,
                        )
                        if (not isinstance(endpoint_rows, list) or len(endpoint_rows) != len(endpoint_ids)
                                or len({row.get("node_id") for row in endpoint_rows if isinstance(row, dict)})
                                != len(endpoint_ids)
                                or any(not isinstance(row, dict) or row.get("group_id") != context.group_id
                                       for row in endpoint_rows)):
                            unresolved.append(action.fact_id)
                            continue

                        replacement_edge = None
                        intended_fingerprint = absent_fingerprint
                        if action.replacement is not None:
                            snapshot = action.replacement
                            replacement_edge = EntityEdge(
                                uuid=snapshot.fact_id, group_id=snapshot.group_id,
                                source_node_uuid=snapshot.source_node_id,
                                target_node_uuid=snapshot.target_node_id,
                                name=snapshot.name, fact=snapshot.fact,
                                episodes=list(snapshot.episode_ids), created_at=snapshot.created_at,
                                reference_time=snapshot.reference_time, valid_at=snapshot.valid_at,
                                invalid_at=snapshot.invalid_at, expired_at=snapshot.expired_at,
                                fact_embedding=list(snapshot.fact_embedding),
                                attributes=dict(snapshot.attributes),
                            )
                            intended_fingerprint = _fact_recovery_fingerprint(
                                replacement_edge, context.embedding.dimensions,
                            )
                        if current_fingerprint == intended_fingerprint:
                            (replaced if replacement_edge is not None else deleted).append(action.fact_id)
                            continue
                        # Graph episode references can be stale; the owner callback proves current support absence.
                        if (current is None or current_fingerprint != action.expected_current_state_fingerprint):
                            unresolved.append(action.fact_id)
                            continue

                        prior_state = ExactFactState(
                            fact_id=action.fact_id, source_node_id=action.source_node_id,
                            target_node_id=action.target_node_id,
                            episode_ids=tuple(sorted(str(value) for value in current.episodes)),
                            state_fingerprint=current_fingerprint, existed=True,
                            valid_at=current.valid_at, invalid_at=current.invalid_at,
                            expired_at=current.expired_at,
                        )
                        intended_support = ExactFactSupport(
                            fact_id=action.fact_id,
                            episode_ids=(action.replacement.episode_ids if action.replacement else ()),
                            source_node_id=action.source_node_id, target_node_id=action.target_node_id,
                            valid_at=(action.replacement.valid_at if action.replacement else None),
                            invalid_at=(action.replacement.invalid_at if action.replacement else None),
                            expired_at=(action.replacement.expired_at if action.replacement else None),
                            state_fingerprint=intended_fingerprint,
                        )
                        cleanup_receipt = GraphWriteReceipt(
                            operation_id=context.operation_id, lease_token=context.lease_token,
                            episode_id=UUID(episode_id), group_id=context.group_id,
                            mapping_revision=context.mapping_revision,
                            desired_support_digest=hashlib.sha256(json.dumps(
                                [action.fact_id, action.action, current_fingerprint, intended_fingerprint],
                                separators=(",", ":"),
                            ).encode()).hexdigest(),
                            phase="cleanup_write_intent",
                            prior_episode_state_fingerprint=next((
                                item.episode_state_fingerprint for item in reversed(receipts)
                                if item.episode_state_fingerprint is not None
                            ), None),
                            fact_ids=(action.fact_id,),
                            existing_fact_states=(prior_state,),
                            intended_fact_support=(intended_support,),
                            canonical_bindings=_receipt_bindings(context.canonical_bindings),
                        )
                        await context.record_write_intent(cleanup_receipt)
                        retained_receipts.append(cleanup_receipt)
                        await context.authorize_fact_recovery(tuple(retained_receipts), actions)
                        fresh_rows = await EntityEdge.get_by_uuids(driver, [action.fact_id])
                        fresh = next((item for item in fresh_rows if str(item.uuid) == action.fact_id), None)
                        if fresh is None:
                            unresolved.append(action.fact_id)
                            continue
                        await fresh.load_fact_embedding(driver)
                        fresh_endpoint_rows, _, _ = await driver.execute_query(
                            "MATCH (node:Entity) WHERE node.uuid IN $node_ids "
                            "RETURN node.uuid AS node_id, node.group_id AS group_id "
                            "ORDER BY node.uuid LIMIT $limit",
                            node_ids=endpoint_ids, limit=len(endpoint_ids) + 1,
                        )
                        if (_fact_recovery_fingerprint(fresh, context.embedding.dimensions)
                                != current_fingerprint
                                or not isinstance(fresh_endpoint_rows, list)
                                or len(fresh_endpoint_rows) != len(endpoint_ids)
                                or len({row.get("node_id") for row in fresh_endpoint_rows
                                        if isinstance(row, dict)}) != len(endpoint_ids)
                                or any(not isinstance(row, dict) or row.get("group_id") != context.group_id
                                       for row in fresh_endpoint_rows)):
                            unresolved.append(action.fact_id)
                            continue
                        if replacement_edge is not None:
                            await replacement_edge.save(driver)
                            saved_rows = await EntityEdge.get_by_uuids(driver, [action.fact_id])
                            saved = next((item for item in saved_rows if str(item.uuid) == action.fact_id), None)
                            if saved is None:
                                unresolved.append(action.fact_id)
                                continue
                            await saved.load_fact_embedding(driver)
                            if _fact_recovery_fingerprint(saved, context.embedding.dimensions) == intended_fingerprint:
                                replaced.append(action.fact_id)
                            else:
                                unresolved.append(action.fact_id)
                        else:
                            await EntityEdge.delete_by_uuids(driver, [action.fact_id])
                            remaining = await EntityEdge.get_by_uuids(driver, [action.fact_id])
                            if not remaining:
                                deleted.append(action.fact_id)
                            else:
                                unresolved.append(action.fact_id)
                    return ExactFactRecoveryOutcome(
                        converged=not unresolved, replaced_ids=tuple(sorted(replaced)),
                        deleted_ids=tuple(sorted(deleted)), unresolved_ids=tuple(sorted(unresolved)),
                        reason="converged" if not unresolved else "exact_fact_state_unresolved",
                    )
            except GraphOperationError:
                raise
            except (TimeoutError, asyncio.CancelledError):
                return ExactFactRecoveryOutcome(
                    converged=False, replaced_ids=tuple(sorted(replaced)),
                    deleted_ids=tuple(sorted(deleted)),
                    unresolved_ids=tuple(sorted(set(unresolved) | {item.fact_id for item in actions})),
                    reason="graph_fact_recovery_outcome_unknown",
                )
            except Exception as exc:
                logger.warning("Graph fact recovery incomplete category=%s", type(exc).__name__)
                return ExactFactRecoveryOutcome(
                    converged=False, replaced_ids=tuple(sorted(replaced)),
                    deleted_ids=tuple(sorted(deleted)),
                    unresolved_ids=tuple(sorted(set(unresolved) | {item.fact_id for item in actions})),
                    reason="graph_fact_recovery_outcome_unknown",
                )

    async def delete_episode(
        self, episode_id: UUID, context: OperationAuthorization,
        known_owned: bool, tombstone_recorded: bool,
    ) -> DeleteOutcome:
        """Remove a tombstoned episode and prove current authorized recovery or return exact pending IDs.

        The caller holds the source/document fence. Receipt history and detached
        recovery actions must match its live lease; graph reads remain partitioned,
        vector-aware and exact-ID bounded. Episode removal is reported separately
        from fresh fact recomputation and node recovery completion. Typed rebuild
        deletion requires exact incident absence; cross-operation absence is
        accepted only after readback and a durable owner proof, then journaled as
        this operation's actual observation.
        """
        if (not known_owned or not tombstone_recorded or not _UUID.fullmatch(str(episode_id))
                or not context.partition_episode_ids
                or str(episode_id) not in context.partition_episode_ids):
            raise GraphOperationError("graph_delete_requires_owned_tombstone")
        if self.state != GraphState.READY or self._driver is None:
            return DeleteOutcome(str(episode_id), False, (), (), "unknown")
        affected_ids: tuple[str, ...] = ()
        shared_ids: tuple[str, ...] = ()
        stale_ids: tuple[str, ...] = ()
        recovery_ids: tuple[str, ...] = ()
        pending_fact_ids: set[str] = set()
        try:
            async with context.fence():
                async with asyncio.timeout(OPERATION_SECONDS):
                    receipts = context.receipt_history or (
                        (context.episode_receipt,) if context.episode_receipt is not None else ()
                    )
                    prior_receipt = next((item for item in receipts if item.phase in {
                        "shell_write_intent", "bulk_ids_intent", "bulk_write_intent",
                    }), None)
                    if (prior_receipt is None or prior_receipt.episode_id != episode_id
                            or any(item.phase not in {
                                "canonical_node_write_intent", "shell_write_intent",
                                "bulk_ids_intent", "bulk_write_intent",
                                "cleanup_write_intent",
                            } or item.episode_id != episode_id or item.group_id != context.group_id
                                 or item.operation_id != context.operation_id
                                 or item.lease_token != context.lease_token
                                 or item.mapping_revision != context.mapping_revision for item in receipts)):
                        return DeleteOutcome(str(episode_id), False, (), (), "unknown")
                    phase_order = {
                        "canonical_node_write_intent": 0, "shell_write_intent": 1,
                        "bulk_ids_intent": 2, "bulk_write_intent": 3, "cleanup_write_intent": 4,
                    }
                    ordered_phases = [phase_order[item.phase] for item in receipts]
                    if ordered_phases != sorted(ordered_phases):
                        return DeleteOutcome(str(episode_id), False, (), (), "unknown")
                    aggregate = await _authorize_receipt_witnesses(context, receipts, "delete")
                    known_entity_ids = tuple(sorted({value for item in receipts for value in item.entity_ids}))
                    known_mention_ids = tuple(sorted({value for item in receipts for value in item.mention_ids}))
                    known_fact_ids = tuple(sorted({value for item in receipts for value in item.fact_ids}))
                    if aggregate is not None:
                        # Physical readback covers the complete owner-certified journal effects.
                        known_entity_ids, known_mention_ids, known_fact_ids = (
                            aggregate.entity_ids, aggregate.mention_ids, aggregate.fact_ids,
                        )
                    recovery_ids = known_entity_ids
                    aggregate_ids = set(known_entity_ids) | set(known_mention_ids) | set(known_fact_ids)
                    aggregate_ids.update(link.edge_id for item in receipts for link in item.incident_links)
                    node_actions = context.node_recovery_actions
                    fact_actions = context.fact_recovery_actions
                    fact_receipts = tuple(item for item in receipts if item.phase in {
                        "bulk_write_intent", "cleanup_write_intent",
                    })
                    if (len(aggregate_ids | {item.graph_entity_uuid for item in node_actions}
                            | {link.edge_id for item in node_actions for link in item.expected_incident_links}
                            | {item.fact_id for item in fact_actions}) > MAX_SUPPORT
                            or any(item.graph_entity_uuid not in set(known_entity_ids)
                                   for item in node_actions)
                            or any(item.fact_id not in set(known_fact_ids) for item in fact_actions)):
                        return DeleteOutcome(str(episode_id), False, (), (), "unknown")
                    fact_proofs: dict[str, set[tuple[str, str]]] = {}
                    for item in fact_receipts:
                        for state in (*item.existing_fact_states, *item.intended_fact_support):
                            fact_proofs.setdefault(state.fact_id, set()).add(
                                (state.source_node_id, state.target_node_id),
                            )
                    if any((item.source_node_id, item.target_node_id) not in fact_proofs.get(item.fact_id, set())
                           or not any(state.state_fingerprint == item.expected_current_state_fingerprint
                                      for receipt in fact_receipts
                                      for state in (*receipt.existing_fact_states,
                                                    *receipt.intended_fact_support)
                                      if state.fact_id == item.fact_id)
                           or (item.replacement is not None and str(episode_id) in item.replacement.episode_ids)
                           for item in fact_actions):
                        return DeleteOutcome(str(episode_id), False, (), (), "unknown")
                    if node_actions:
                        await context.authorize_node_recovery(receipts, node_actions)
                    if fact_actions:
                        await context.authorize_fact_recovery(fact_receipts, fact_actions)
                    await self._validate_partition(context, "delete", str(episode_id))
                    from graphiti_core.edges import EntityEdge, EpisodicEdge
                    from graphiti_core.nodes import EntityNode, EpisodicNode
                    driver = await self._partition_driver(context.group_id)

                    episode = None
                    try:
                        episode = await EpisodicNode.get_by_uuid(driver, str(episode_id))
                    except Exception as exc:
                        if type(exc).__name__ != "NodeNotFoundError":
                            raise
                    if episode is not None and episode.group_id != context.group_id:
                        raise GraphOperationError("graph_episode_partition_mismatch")
                    if episode is not None and prior_receipt.episode_state_fingerprint:
                        allowed_episode_states = {
                            value for item in receipts
                            for value in (item.episode_state_fingerprint, item.prior_episode_state_fingerprint)
                            if value
                        }
                        if _episode_state_fingerprint(episode) not in allowed_episode_states:
                            raise GraphOperationError("graph_episode_state_changed")
                    episode_fact_ids = set(str(value) for value in episode.entity_edges) if episode else set()
                    if not episode_fact_ids <= set(known_fact_ids):
                        raise GraphOperationError("graph_episode_receipt_mismatch")
                    candidate_fact_ids = tuple(sorted(set(known_fact_ids) | {
                        str(value) for value in episode_fact_ids
                    }))
                    if len(candidate_fact_ids) > MAX_SUPPORT:
                        raise GraphOperationError("graph_episode_receipt_mismatch")
                    edges = await EntityEdge.get_by_uuids(driver, list(candidate_fact_ids))
                    observed_fact_fingerprints: dict[str, str] = {}
                    cross_rebuild_absent_fact_ids: set[str] = set()
                    for edge in edges:
                        await edge.load_fact_embedding(driver)
                    present_fact_ids = {str(edge.uuid) for edge in edges}
                    for missing_fact_id in set(candidate_fact_ids) - present_fact_ids:
                        latest_intent = next((support_item for item in reversed(receipts)
                            for support_item in item.intended_fact_support
                            if support_item.fact_id == missing_fact_id), None)
                        remaining_support = (
                            set(latest_intent.episode_ids) - {str(episode_id)}
                            if latest_intent is not None else set()
                        )
                        if remaining_support:
                            # Current exact absence is required before consulting another operation's rebuild proof.
                            if not await _authorize_cross_rebuild_absence(
                                context, "fact", missing_fact_id, episode_id=UUID(str(episode_id)),
                                endpoints=(latest_intent.source_node_id, latest_intent.target_node_id),
                            ):
                                raise GraphOperationError("graph_delete_fact_absence_unproved")
                            cross_rebuild_absent_fact_ids.add(missing_fact_id)
                    affected_ids = candidate_fact_ids
                    if len(edges) > MAX_SUPPORT or any(
                        edge.group_id != context.group_id
                        or any(not _UUID.fullmatch(str(value)) for value in edge.episodes)
                        or len(set(str(value) for value in edge.episodes)) != len(edge.episodes)
                        or not set(str(value) for value in edge.episodes) <= set(context.partition_episode_ids)
                        for edge in edges
                    ):
                        raise GraphOperationError("graph_delete_support_inventory_invalid")
                    mentions = await EpisodicEdge.get_by_uuids(
                        driver, list(known_mention_ids),
                    ) if known_mention_ids else []
                    records, _, _ = await driver.execute_query(
                        "MATCH (episode:Episodic {uuid: $episode_id})-"
                        "[mention:MENTIONS]->() RETURN mention.uuid AS mention_id "
                        "ORDER BY mention.uuid LIMIT $limit",
                        episode_id=str(episode_id), limit=MAX_SUPPORT + 1,
                    )
                    if not isinstance(records, list) or len(records) > MAX_SUPPORT:
                        raise GraphOperationError("graph_episode_mention_inventory_invalid")
                    live_mention_ids = {
                        row.get("mention_id") for row in records if isinstance(row, dict)
                    }
                    if (len(live_mention_ids) > MAX_SUPPORT
                            or len(live_mention_ids) != len(records)
                            or not live_mention_ids <= set(known_mention_ids)
                            or {str(item.uuid) for item in mentions} != live_mention_ids
                            or any(item.group_id != context.group_id
                                   or str(item.source_node_uuid) != str(episode_id) for item in mentions)):
                        raise GraphOperationError("graph_episode_mention_receipt_mismatch")
                    for edge in edges:
                        fact_id = str(edge.uuid)
                        current_fingerprint = _fact_state(edge, context.embedding.dimensions).state_fingerprint
                        observed_fact_fingerprints[fact_id] = current_fingerprint
                        allowed = {state.state_fingerprint for item in receipts
                                   for state in (*item.existing_fact_states, *item.intended_fact_support)
                                   if state.fact_id == fact_id}
                        if current_fingerprint not in allowed:
                            raise GraphOperationError("graph_episode_fact_state_changed")
                    shared: list[str] = []
                    remove: list[str] = []
                    support: list[ExactFactSupport] = []
                    states: list[ExactFactState] = []
                    stale: set[str] = set()
                    receipt_shared_obligations: set[str] = set()
                    receipt_stale_obligations: set[str] = set()
                    # Rebuild pending work from owned cleanup history, even after support was already removed.
                    for receipt_item in fact_receipts:
                        prior_states = {item.fact_id: item for item in receipt_item.existing_fact_states}
                        intended_states = {item.fact_id: item for item in receipt_item.intended_fact_support}
                        for fact_id in set(prior_states) & set(intended_states):
                            prior_state = prior_states[fact_id]
                            intended_state = intended_states[fact_id]
                            target_was_supported = str(episode_id) in prior_state.episode_ids
                            target_is_supported = str(episode_id) in intended_state.episode_ids
                            if (receipt_item.phase == "cleanup_write_intent" and prior_state.existed
                                    and target_was_supported and not target_is_supported
                                    and intended_state.episode_ids):
                                receipt_shared_obligations.add(fact_id)
                            # A retry must preserve recomputation required by an earlier owned cleanup.
                            if (receipt_item.phase == "cleanup_write_intent" and prior_state.existed
                                    and not target_was_supported and not target_is_supported
                                    and (prior_state.valid_at, prior_state.invalid_at, prior_state.expired_at)
                                    != (intended_state.valid_at, intended_state.invalid_at,
                                        intended_state.expired_at)):
                                receipt_stale_obligations.add(fact_id)
                    for edge in edges:
                        remaining = [item for item in edge.episodes if item != str(episode_id)]
                        fact_id = str(edge.uuid)
                        historical_prior = next((state for item in receipts
                            for state in item.existing_fact_states if state.fact_id == fact_id and state.existed), None)
                        historical_intended = next((state for item in receipts
                            for state in item.intended_fact_support if state.fact_id == fact_id), None)
                        if len(remaining) == len(edge.episodes):
                            if (historical_prior is not None and historical_intended is not None
                                    and (historical_prior.valid_at, historical_prior.invalid_at,
                                         historical_prior.expired_at)
                                    != (historical_intended.valid_at, historical_intended.invalid_at,
                                        historical_intended.expired_at)):
                                # Support subtraction cannot undo a separate extraction-time invalidation.
                                stale.add(fact_id)
                            continue
                        states.append(_fact_state(edge, context.embedding.dimensions))
                        edge.episodes = remaining
                        intended_fingerprint = (
                            _ABSENT_FACT_FINGERPRINT if not remaining
                            else _fact_state(edge, context.embedding.dimensions).state_fingerprint
                        )
                        support.append(ExactFactSupport(
                            fact_id=str(edge.uuid), episode_ids=tuple(sorted(str(value) for value in remaining)),
                            source_node_id=str(edge.source_node_uuid), target_node_id=str(edge.target_node_uuid),
                            valid_at=edge.valid_at, invalid_at=edge.invalid_at, expired_at=edge.expired_at,
                            state_fingerprint=intended_fingerprint,
                        ))
                        if remaining:
                            # T3 suppresses stale fact text until surviving evidence recomputes it.
                            shared.append(edge.uuid)
                        else:
                            remove.append(edge.uuid)
                    stale.update(receipt_stale_obligations)
                    shared_ids = tuple(sorted(
                        set(str(value) for value in shared) | stale | receipt_shared_obligations,
                    ))
                    pending_fact_ids = set(shared_ids)
                    pending_fact_ids.update(item.fact_id for item in fact_actions)
                    pending_node_ids = set(known_entity_ids)
                    digest = hashlib.sha256(json.dumps(
                        [(item.fact_id, item.episode_ids, item.source_node_id, item.target_node_id,
                          str(item.valid_at), str(item.invalid_at), str(item.expired_at), item.state_fingerprint)
                         for item in support],
                        sort_keys=True, separators=(",", ":"),
                    ).encode()).hexdigest()
                    receipt = GraphWriteReceipt(
                        operation_id=context.operation_id,
                        lease_token=context.lease_token,
                        episode_id=episode_id,
                        group_id=context.group_id,
                        mapping_revision=context.mapping_revision,
                        desired_support_digest=digest,
                        phase="cleanup_write_intent",
                        prior_episode_state_fingerprint=(
                            _episode_state_fingerprint(episode) if episode is not None
                            else prior_receipt.episode_state_fingerprint
                        ),
                        entity_ids=known_entity_ids,
                        mention_ids=known_mention_ids,
                        fact_ids=tuple(sorted(item.fact_id for item in states)),
                        existing_fact_states=tuple(states),
                        intended_fact_support=tuple(support),
                        canonical_bindings=_receipt_bindings(context.canonical_bindings),
                    )
                    try:
                        await self._validate_partition(context, "delete", str(episode_id))
                        await context.record_write_intent(receipt)
                        receipts = (*receipts, receipt)
                        fact_receipts = tuple(item for item in receipts if item.phase in {
                            "bulk_write_intent", "cleanup_write_intent",
                        })
                    except Exception:
                        return DeleteOutcome(
                            str(episode_id), False, affected_ids, shared_ids, "unknown", stale_ids, recovery_ids,
                        )
                    prior_by_id = {item.fact_id: item for item in states}
                    intended_by_id = {item.fact_id: item for item in support}
                    for edge_id in shared_ids:
                        intended = intended_by_id.get(edge_id)
                        if intended is None or not intended.episode_ids:
                            continue
                        await self._validate_partition(context, "delete", str(episode_id))
                        fresh_rows = await EntityEdge.get_by_uuids(driver, [edge_id])
                        fresh = next((item for item in fresh_rows if str(item.uuid) == edge_id), None)
                        prior = prior_by_id.get(edge_id)
                        if (fresh is None or prior is None or fresh.group_id != context.group_id):
                            raise GraphOperationError("graph_delete_fact_recheck_failed")
                        await fresh.load_fact_embedding(driver)
                        if _fact_state(fresh, context.embedding.dimensions).state_fingerprint != prior.state_fingerprint:
                            raise GraphOperationError("graph_delete_fact_recheck_failed")
                        mutation_rows, _, _ = await driver.execute_query(
                            "MATCH (source:Entity {uuid: $source_id, group_id: $group_id})-"
                            "[edge:RELATES_TO {uuid: $fact_id, group_id: $group_id}]->"
                            "(target:Entity {uuid: $target_id, group_id: $group_id}) "
                            "SET edge.episodes = $episode_ids RETURN edge.uuid AS fact_id",
                            source_id=intended.source_node_id, target_id=intended.target_node_id,
                            group_id=context.group_id, fact_id=edge_id,
                            episode_ids=list(intended.episode_ids),
                        )
                        if (not isinstance(mutation_rows, list) or len(mutation_rows) != 1
                                or not isinstance(mutation_rows[0], dict)
                                or mutation_rows[0].get("fact_id") != edge_id):
                            raise GraphOperationError("graph_delete_fact_support_write_unconfirmed")
                        saved_rows = await EntityEdge.get_by_uuids(driver, [edge_id])
                        saved = next((item for item in saved_rows if str(item.uuid) == edge_id), None)
                        if saved is None or saved.group_id != context.group_id:
                            raise GraphOperationError("graph_delete_fact_support_readback_missing")
                        await saved.load_fact_embedding(driver)
                        if (_fact_state(saved, context.embedding.dimensions).state_fingerprint
                                != intended.state_fingerprint
                                or set(str(value) for value in saved.episodes) != set(intended.episode_ids)):
                            raise GraphOperationError("graph_delete_fact_support_readback_mismatch")
                    for edge_id in remove:
                        fact_id = str(edge_id)
                        await self._validate_partition(context, "delete", str(episode_id))
                        fresh_rows = await EntityEdge.get_by_uuids(driver, [fact_id])
                        fresh = next((item for item in fresh_rows if str(item.uuid) == fact_id), None)
                        prior = prior_by_id.get(fact_id)
                        if (fresh is None or prior is None or fresh.group_id != context.group_id):
                            raise GraphOperationError("graph_delete_fact_recheck_failed")
                        await fresh.load_fact_embedding(driver)
                        if _fact_state(fresh, context.embedding.dimensions).state_fingerprint != prior.state_fingerprint:
                            raise GraphOperationError("graph_delete_fact_recheck_failed")
                        await EntityEdge.delete_by_uuids(driver, [fact_id])
                    if episode is not None:
                        await self._validate_partition(context, "delete", str(episode_id))
                        latest_episode = await EpisodicNode.get_by_uuid(driver, str(episode_id))
                        if (_episode_state_fingerprint(latest_episode)
                                not in {item.episode_state_fingerprint for item in receipts
                                        if item.episode_state_fingerprint is not None}):
                            raise GraphOperationError("graph_delete_episode_recheck_failed")
                        latest_mentions = await EpisodicEdge.get_by_uuids(driver, list(known_mention_ids)) \
                            if known_mention_ids else []
                        live_links, _, _ = await driver.execute_query(
                            "MATCH (episode:Episodic {uuid: $episode_id})-"
                            "[mention:MENTIONS]->() RETURN mention.uuid AS mention_id "
                            "ORDER BY mention.uuid LIMIT $limit",
                            episode_id=str(episode_id), limit=MAX_SUPPORT + 1,
                        )
                        live_ids = {row.get("mention_id") for row in live_links if isinstance(row, dict)} \
                            if isinstance(live_links, list) else set()
                        if (not isinstance(live_links, list) or len(live_links) > MAX_SUPPORT
                                or len(live_ids) != len(live_links)
                                or not live_ids <= set(known_mention_ids)
                                or {str(item.uuid) for item in latest_mentions} != live_ids
                                or any(item.group_id != context.group_id
                                       or str(item.source_node_uuid) != str(episode_id)
                                       for item in latest_mentions)):
                            raise GraphOperationError("graph_delete_mention_recheck_failed")
                        await latest_episode.delete(driver)
                    try:
                        await EpisodicNode.get_by_uuid(driver, str(episode_id))
                        episode_absent = False
                    except Exception as exc:
                        if type(exc).__name__ != "NodeNotFoundError":
                            raise
                        episode_absent = True
                    remaining_mentions = await EpisodicEdge.get_by_uuids(driver, list(known_mention_ids)) \
                        if known_mention_ids else []
                    reverse_support, _, _ = await driver.execute_query(
                        "MATCH ()-[edge]->() WHERE $episode_id IN coalesce(edge.episodes, []) "
                        "RETURN edge.uuid AS fact_id ORDER BY edge.uuid LIMIT $limit",
                        episode_id=str(episode_id), limit=MAX_SUPPORT + 1,
                    )
                    if (not isinstance(reverse_support, list) or len(reverse_support) > MAX_SUPPORT
                            or any(not isinstance(row, dict) or not _UUID.fullmatch(str(row.get("fact_id")))
                                   for row in reverse_support)):
                        raise GraphOperationError("graph_delete_reverse_support_invalid")
                    await self._validate_partition(context, "delete", str(episode_id))
                    if node_actions:
                        await context.authorize_node_recovery(receipts, node_actions)
                    if fact_actions:
                        fact_receipts = tuple(item for item in receipts if item.phase in {
                            "bulk_write_intent", "cleanup_write_intent",
                        })
                        await context.authorize_fact_recovery(fact_receipts, fact_actions)

                    async def read_node_links(entity_id: str) -> tuple[ExactGraphLink, ...]:
                        """Read and validate the complete bounded incident-link set for terminal node proof."""
                        rows, _, _ = await driver.execute_query(
                            "MATCH (source)-[edge]-(entity:Entity {uuid: $entity_id}) "
                            "RETURN edge.uuid AS edge_id, type(edge) AS relationship_type, "
                            "startNode(edge).uuid AS source_node_id, endNode(edge).uuid AS target_node_id, "
                            "edge.episodes AS episode_ids ORDER BY edge.uuid LIMIT $limit",
                            entity_id=entity_id, limit=MAX_SUPPORT + 1,
                        )
                        if not isinstance(rows, list) or len(rows) > MAX_SUPPORT:
                            raise GraphOperationError("graph_delete_node_links_invalid")
                        links: list[ExactGraphLink] = []
                        for row in rows:
                            episodes = row.get("episode_ids") or () if isinstance(row, dict) else ()
                            if (not isinstance(row, dict) or not isinstance(episodes, list | tuple)
                                    or not isinstance(row.get("relationship_type"), str)):
                                raise GraphOperationError("graph_delete_node_links_invalid")
                            links.append(ExactGraphLink(
                                edge_id=str(row.get("edge_id")),
                                relationship_type=row["relationship_type"],
                                source_node_id=str(row.get("source_node_id")),
                                target_node_id=str(row.get("target_node_id")),
                                episode_ids=tuple(sorted(str(value) for value in episodes)),
                            ))
                        if len({item.edge_id for item in links}) != len(links):
                            raise GraphOperationError("graph_delete_node_links_invalid")
                        return tuple(links)

                    node_converged: set[str] = set()
                    for action in node_actions:
                        entity = None
                        try:
                            entity = await EntityNode.get_by_uuid(driver, action.graph_entity_uuid)
                            await entity.load_name_embedding(driver)
                        except Exception as exc:
                            if type(exc).__name__ != "NodeNotFoundError":
                                raise _safe_error(exc) from None
                        links = await read_node_links(action.graph_entity_uuid)
                        fingerprint = (
                            _entity_state_fingerprint(entity, context.embedding.dimensions)
                            if entity is not None else None
                        )
                        if (links != action.expected_incident_links
                                and not (entity is None and not links and action.action == "delete_for_rebuild")):
                            continue
                        if action.action == "retain":
                            if (entity is not None
                                    and fingerprint == action.expected_current_state_fingerprint):
                                node_converged.add(action.graph_entity_uuid)
                            continue
                        if action.action == "replace_from_current_support":
                            if (entity is None or fingerprint != action.expected_current_state_fingerprint
                                    or action.graph_entity_uuid not in set(known_entity_ids)):
                                continue
                            latest_node_intent = next((value for item in reversed(receipts)
                                if item.phase == "cleanup_write_intent"
                                for node_id, value in item.intended_entity_state_fingerprints
                                if node_id == action.graph_entity_uuid), None)
                            if latest_node_intent != fingerprint:
                                continue
                            latest_node_receipt = next((item for item in reversed(receipts)
                                if item.phase == "cleanup_write_intent"
                                and any(node_id == action.graph_entity_uuid
                                        for node_id, _ in item.intended_entity_state_fingerprints)), None)
                            if latest_node_receipt is None:
                                continue
                            binding = action.replacement_binding or next((item for receipt_item in receipts
                                for item in receipt_item.canonical_bindings
                                if item.graph_entity_uuid == action.graph_entity_uuid), None)
                            candidate = action.replacement_candidate
                            if (binding is None and candidate is None):
                                continue
                            if binding is not None and (
                                binding.graph_entity_uuid != action.graph_entity_uuid
                                or binding.group_id != context.group_id
                            ):
                                continue
                            if candidate is not None and (
                                candidate.graph_entity_uuid != action.graph_entity_uuid
                                or candidate.group_id != context.group_id
                                or str(episode_id) in candidate.support_episode_ids
                                or not set(candidate.support_episode_ids) <= set(context.partition_episode_ids)
                            ):
                                continue
                            if candidate is not None:
                                candidate_proof = {
                                    (field, digest, support_ids)
                                    for node_id, field, digest, support_ids
                                    in latest_node_receipt.candidate_field_support
                                    if node_id == action.graph_entity_uuid
                                }
                                if candidate_proof != set(candidate.field_support):
                                    continue
                            if binding is not None:
                                binding_proof = next((item for item in latest_node_receipt.canonical_bindings
                                    if item.graph_entity_uuid == action.graph_entity_uuid), None)
                                if (binding_proof is None
                                        or binding_proof.group_id != binding.group_id
                                        or binding_proof.canonical_revision != binding.canonical_revision
                                        or binding_proof.field_support != binding.field_support):
                                    continue
                            node_converged.add(action.graph_entity_uuid)
                            continue
                        if action.action in {"delete_orphan", "delete_for_rebuild"} and entity is None:
                            deletion_intent = any(
                                item.phase == "cleanup_write_intent"
                                and (action.graph_entity_uuid, None) in item.intended_entity_state_fingerprints
                                and (item.rebuild_absence_observed == "node"
                                     or any(node_id == action.graph_entity_uuid and existed and state is not None
                                            for node_id, existed, state in item.prior_entity_state_fingerprints))
                                for item in receipts
                            )
                            no_dispatch_absence = (
                                any(item.phase == "bulk_ids_intent"
                                    and any(node_id == action.graph_entity_uuid and not existed
                                            and state is None
                                            for node_id, existed, state in item.prior_entity_state_fingerprints)
                                    for item in receipts)
                                and not any(item.phase in {
                                    "canonical_node_write_intent", "bulk_write_intent",
                                } and action.graph_entity_uuid in item.entity_ids for item in receipts)
                                and not any(item.phase == "cleanup_write_intent"
                                            and action.graph_entity_uuid in item.entity_ids for item in receipts)
                            )
                            if (deletion_intent or no_dispatch_absence
                                    or await _authorize_cross_rebuild_absence(
                                        context, "node", action.graph_entity_uuid,
                                        episode_id=UUID(str(episode_id)),
                                    )):
                                if (action.action != "delete_for_rebuild" or await _incident_ids_absent(
                                    driver, [link.edge_id for item in receipts
                                             if action.graph_entity_uuid in item.entity_ids
                                             for link in item.incident_links]
                                    + [link.edge_id for link in action.expected_incident_links],
                                )):
                                    node_converged.add(action.graph_entity_uuid)

                    latest_support: dict[str, ExactFactSupport] = {}
                    for receipt_item in receipts:
                        for support_item in receipt_item.intended_fact_support:
                            latest_support[support_item.fact_id] = support_item
                    authorized_absent_fact_ids = {
                        action.fact_id for action in fact_actions
                        if action.action in {"delete_unsupported", "delete_for_rebuild"} and (
                            any(item.phase == "cleanup_write_intent"
                                and any(value.fact_id == action.fact_id
                                        and value.state_fingerprint == _ABSENT_FACT_FINGERPRINT
                                        and not value.episode_ids
                                        and value.source_node_id == action.source_node_id
                                        and value.target_node_id == action.target_node_id
                                        for value in item.intended_fact_support)
                                and any(value.fact_id == action.fact_id and value.existed
                                        and value.source_node_id == action.source_node_id
                                        and value.target_node_id == action.target_node_id
                                        and value.state_fingerprint == action.expected_current_state_fingerprint
                                        for value in item.existing_fact_states)
                                for item in receipts)
                            or any(
                                any(value.fact_id == action.fact_id and not value.existed
                                    and value.source_node_id == action.source_node_id
                                    and value.target_node_id == action.target_node_id
                                    for value in item.existing_fact_states)
                                and any(value.fact_id == action.fact_id
                                        and value.source_node_id == action.source_node_id
                                        and value.target_node_id == action.target_node_id
                                        for value in item.intended_fact_support)
                                for item in receipts
                            )
                        )
                    }
                    authorized_absent_fact_ids.update(cross_rebuild_absent_fact_ids)
                    remaining_facts = await EntityEdge.get_by_uuids(driver, list(known_fact_ids)) \
                        if known_fact_ids else []
                    current_fact_by_id = {str(edge.uuid): edge for edge in remaining_facts}
                    if (len(current_fact_by_id) != len(remaining_facts)
                            or not set(current_fact_by_id) <= set(known_fact_ids)):
                        raise GraphOperationError("graph_delete_fact_final_inventory_invalid")
                    for edge in remaining_facts:
                        await edge.load_fact_embedding(driver)
                    expected_survivors: dict[str, ExactFactSupport] = {}
                    final_fact_set_matches = True
                    for fact_id in known_fact_ids:
                        expected = latest_support.get(fact_id)
                        if fact_id in authorized_absent_fact_ids:
                            if fact_id in current_fact_by_id:
                                final_fact_set_matches = False
                            continue
                        if expected is None:
                            expected_fingerprint = observed_fact_fingerprints.get(fact_id)
                            current = current_fact_by_id.get(fact_id)
                            if expected_fingerprint is None:
                                if current is not None:
                                    final_fact_set_matches = False
                            elif (current is None or current.group_id != context.group_id
                                  or _fact_state(current, context.embedding.dimensions).state_fingerprint
                                  != expected_fingerprint):
                                final_fact_set_matches = False
                            else:
                                expected_survivors[fact_id] = ExactFactSupport(
                                    fact_id=fact_id,
                                    episode_ids=tuple(sorted(str(value) for value in current.episodes)),
                                    source_node_id=str(current.source_node_uuid),
                                    target_node_id=str(current.target_node_uuid),
                                    state_fingerprint=expected_fingerprint,
                                )
                            continue
                        if expected.episode_ids:
                            expected_survivors[fact_id] = expected
                            current = current_fact_by_id.get(fact_id)
                            if (current is None or current.group_id != context.group_id
                                    or str(current.source_node_uuid) != expected.source_node_id
                                    or str(current.target_node_uuid) != expected.target_node_id
                                    or tuple(sorted(str(value) for value in current.episodes)) != expected.episode_ids
                                    or str(episode_id) in expected.episode_ids
                                    or _fact_state(current, context.embedding.dimensions).state_fingerprint
                                    != expected.state_fingerprint):
                                final_fact_set_matches = False
                        else:
                            if (expected.state_fingerprint != _ABSENT_FACT_FINGERPRINT
                                    or fact_id in current_fact_by_id):
                                final_fact_set_matches = False
                    if set(current_fact_by_id) != set(expected_survivors):
                        final_fact_set_matches = False

                    fact_converged: set[str] = set()
                    for action in fact_actions:
                        current = current_fact_by_id.get(action.fact_id)
                        if action.action == "replace_from_current_support" and action.replacement is not None:
                            snapshot = action.replacement
                            replacement = EntityEdge(
                                uuid=snapshot.fact_id, group_id=snapshot.group_id,
                                source_node_uuid=snapshot.source_node_id,
                                target_node_uuid=snapshot.target_node_id, name=snapshot.name,
                                fact=snapshot.fact, episodes=list(snapshot.episode_ids),
                                created_at=snapshot.created_at, reference_time=snapshot.reference_time,
                                valid_at=snapshot.valid_at, invalid_at=snapshot.invalid_at,
                                expired_at=snapshot.expired_at,
                                fact_embedding=list(snapshot.fact_embedding),
                                attributes=dict(snapshot.attributes),
                            )
                            snapshot_fingerprint = _fact_recovery_fingerprint(
                                replacement, context.embedding.dimensions,
                            )
                            latest = latest_support.get(action.fact_id)
                            if (current is not None and current.group_id == context.group_id
                                    and str(current.source_node_uuid) == action.source_node_id
                                    and str(current.target_node_uuid) == action.target_node_id
                                    and tuple(sorted(str(value) for value in current.episodes)) == snapshot.episode_ids
                                    and str(episode_id) not in snapshot.episode_ids
                                    and _fact_recovery_fingerprint(current, context.embedding.dimensions)
                                    == snapshot_fingerprint
                                    and latest is not None and latest.episode_ids == snapshot.episode_ids
                                    and latest.state_fingerprint == snapshot_fingerprint):
                                fact_converged.add(action.fact_id)
                        elif action.action in {"delete_unsupported", "delete_for_rebuild"} and current is None:
                            if action.fact_id in authorized_absent_fact_ids:
                                fact_converged.add(action.fact_id)

                    pending_node_ids.difference_update(node_converged)
                    pending_fact_ids.difference_update(fact_converged)
                    episode_cleanup_verified = (
                        episode_absent and not remaining_mentions and not reverse_support
                        and final_fact_set_matches
                    )
                    recovery_ids = tuple(sorted(pending_node_ids))
                    stale_ids = tuple(sorted(pending_fact_ids & stale))
                    shared_ids = tuple(sorted(pending_fact_ids))
                    outcome = (
                        "succeeded" if episode_cleanup_verified and not pending_fact_ids and not pending_node_ids
                        else "unknown"
                    )
                    return DeleteOutcome(
                        str(episode_id), episode_cleanup_verified, affected_ids, shared_ids,
                        outcome, stale_ids, recovery_ids,
                    )
        except (TimeoutError, asyncio.CancelledError, GraphOperationError):
            return DeleteOutcome(str(episode_id), False, affected_ids, shared_ids, "unknown", stale_ids, recovery_ids)
        except Exception as exc:
            logger.warning("Graph deletion incomplete category=%s", type(exc).__name__)
            return DeleteOutcome(str(episode_id), False, affected_ids, shared_ids, "unknown", stale_ids, recovery_ids)
