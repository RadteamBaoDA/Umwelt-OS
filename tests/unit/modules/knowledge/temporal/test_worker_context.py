"""Characterization tests for worker._context: the owner callbacks it hands to the graph adapter.

All database-bound collaborators of ``worker`` are replaced with spies, so the tests
pin which owner checks each callback performs, in what order, and which stable
errors they raise.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.model_gateway.client import PrivacyPolicyDenied
from modules.knowledge.temporal import worker
from modules.knowledge.temporal.adapter import (
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    DispatchOwnership,
    EvidenceIdentity,
    ExactFactRecoveryAction,
    GraphOperationError,
    GraphWriteReceipt,
)


class _Factory:
    """Session factory whose every ``async with factory()`` yields one shared mock session."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.scalar = AsyncMock(return_value=None)
        self.session.add = MagicMock()
        self.session.scalars = AsyncMock(return_value=SimpleNamespace(all=list))

    def __call__(self) -> Any:
        """Return an async context manager over the shared session."""

        @contextlib.asynccontextmanager
        async def manager() -> AsyncIterator[Any]:
            yield self.session

        return manager()


class Env:
    """Wire fake owner state and patch every database-bound collaborator of ``worker._context``."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, cleanup: bool = False, source_ok: bool = True
    ) -> None:
        workspace = uuid4()
        self.adm = worker._Admission(
            scope=SimpleNamespace(workspace_id=workspace, actor_user_id=1, membership_revision=1),
            multi=False,
            fence=SimpleNamespace(),
        )
        self.cleanup = cleanup
        self.partition_id = uuid4()
        self.source_id = uuid4()
        self.mapping = self._row(status="synchronized", external_state="present")
        self.other = self._row(status="synchronized", external_state="present")
        self.rows = [self.mapping, self.other]
        self.support = SimpleNamespace(
            removed=False,
            document_version_id=self.mapping.document_version_id,
            chunk_id=uuid4(),
            source_id=self.source_id,
            source_generation=1,
            document_id=self.mapping.document_id,
        )
        self.operation = SimpleNamespace(id=uuid4(), desired_revision=3, receipt_token=uuid4())
        self.token = uuid4()
        self.source = SimpleNamespace(
            status="active" if source_ok else "deleted", generation=1, local_only=False
        )
        self.bindings: tuple[Any, ...] = ()
        self.factory = _Factory()
        self.session = MagicMock()
        self.config = SimpleNamespace(
            defaults=SimpleNamespace(
                resolve=lambda role: (
                    SimpleNamespace(model="m", version="v", dimensions=3)
                    if role in {"background", "embedding"}
                    else None
                ),
                model_dump=lambda mode: {},
            ),
            privacy=SimpleNamespace(
                allow_remote_reasoning=True,
                allow_remote_embeddings=True,
                reasoning_destinations=[],
                embedding_destinations=[],
                model_dump=lambda mode: {},
            ),
            configuration_revision=1,
            gateway_identity="a" * 64,
            endpoint_destination_id="dest",
            model_identity="b" * 64,
            gateway_base_url="http://gw",
            gateway_api_key="k",
            request_timeout_seconds=10,
            endpoint_allowed_cidrs=(),
            allow_public_providers=False,
            gateway_kind="openai",
        )
        self.calls: dict[str, AsyncMock] = {}
        self._patch(monkeypatch)

    def _row(self, **extra: Any) -> Any:
        """Return a fake mapping row."""
        values: dict[str, Any] = {
            "id": uuid4(),
            "document_id": uuid4(),
            "document_version_id": uuid4(),
            "episode_id": uuid4(),
            "tombstoned": False,
            "desired_revision": 3,
            "embedding_identity": None,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "source_id": self.source_id,
            "source_generation": 1,
            "partition_id": self.partition_id,
            "workspace_id": uuid4(),
            "applied_revision": 3,
            "desired_digest": "d",
            "applied_digest": "d",
            "error_code": None,
        }
        values.update(extra)
        return SimpleNamespace(**values)

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace worker collaborators with spies recording their call order in ``calls``."""
        self.mapping.source_generation = 1
        self.mapping.tombstoned = False
        self.mapping.embedding_identity = {}
        names = {
            "_admit": None,
            "_lease": None,
            "_commit": None,
            "_get": SimpleNamespace(id=self.operation.id),
            "_receipt_inventory": ((), None),
        }
        for name, value in names.items():
            self.calls[name] = AsyncMock(return_value=value)
            monkeypatch.setattr(worker, name, self.calls[name])
        self.calls["_inventory"] = AsyncMock(return_value=(self.rows, [self.support]))
        monkeypatch.setattr(worker, "_inventory", self.calls["_inventory"])
        self.calls["_bindings"] = AsyncMock(side_effect=lambda *a, **k: self.bindings)
        monkeypatch.setattr(worker, "_bindings", self.calls["_bindings"])
        monkeypatch.setattr(worker.sources, "lock_source", AsyncMock(return_value=self.source))
        monkeypatch.setattr(worker.documents, "lock_document_ids", AsyncMock())
        monkeypatch.setattr(
            worker.documents,
            "read_evidence_refs",
            AsyncMock(return_value=[SimpleNamespace(source_id=self.source_id)]),
        )
        monkeypatch.setattr(worker.entities, "lock_entity_ids", AsyncMock())
        monkeypatch.setattr(
            worker.settings_public, "get_ai_execution_config", AsyncMock(return_value=self.config)
        )
        monkeypatch.setattr(worker.settings_public, "derive_for_role", lambda config, role: config)
        self.gateway_type = MagicMock()
        monkeypatch.setattr(worker, "ModelGateway", self.gateway_type)
        self.calls["_recovery_authorization"] = AsyncMock()
        monkeypatch.setattr(
            worker, "_recovery_authorization", self.calls["_recovery_authorization"]
        )
        self.calls["_authorize_scheduled_rebuild"] = AsyncMock()
        monkeypatch.setattr(
            worker, "_authorize_scheduled_rebuild", self.calls["_authorize_scheduled_rebuild"]
        )

    async def build(self) -> tuple[Any, Any, list[Any]]:
        """Call worker._context with the fake owner state."""
        ctx = {"settings": SimpleNamespace(graph_embedding_dimensions=3), "redis": object()}
        return await worker._context(
            self.factory,
            self.adm,
            self.session,
            ctx,
            self.operation,
            self.mapping,
            self.token,
            cleanup=self.cleanup,
        )


@pytest.mark.asyncio
async def test_context_exposes_partition_inventory_and_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authorization closure mirrors the owner inventory, bindings and resolved model roles."""
    env = Env(monkeypatch)
    context, gateway, rows = await env.build()
    assert rows is env.rows and gateway is env.gateway_type.return_value
    assert context.group_id == str(env.partition_id) and context.operation_id == env.operation.id
    assert context.lease_token == env.operation.receipt_token and context.mapping_revision == 3
    assert set(context.partition_episode_ids) == {
        str(env.mapping.episode_id),
        str(env.other.episode_id),
    }
    assert set(context.history_episode_ids) == set(context.partition_episode_ids)
    assert (context.reasoning.role, context.small_reasoning.role, context.embedding.role) == (
        "background",
        "background",
        "embedding",
    )
    assert context.reranking is None and context.embedding.dimensions == 3
    assert context.evidence[0].chunk_id == env.support.chunk_id
    assert env.mapping.embedding_identity == {"model": "m", "version": "v", "dimensions": 3}
    env.calls["_commit"].assert_awaited_once()
    assert context.authorize_receipt_aggregate is not None and context.receipt_aggregate is None


@pytest.mark.asyncio
async def test_inactive_source_generation_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted source blocks normal contexts but cleanup contexts may proceed."""
    env = Env(monkeypatch, source_ok=False)
    with pytest.raises(GraphOperationError, match="graph_source_generation_terminal"):
        await env.build()
    cleanup = Env(monkeypatch, cleanup=True, source_ok=False)
    context, _, _ = await cleanup.build()
    assert context.embedding.mapping is not None


@pytest.mark.asyncio
async def test_support_from_another_source_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence references that resolve to a different source abort context construction."""
    env = Env(monkeypatch)
    monkeypatch.setattr(
        worker.documents,
        "read_evidence_refs",
        AsyncMock(return_value=[SimpleNamespace(source_id=uuid4())]),
    )
    with pytest.raises(GraphOperationError, match="graph_support_source_changed"):
        await env.build()


@pytest.mark.asyncio
async def test_embedding_identity_unconfigured_and_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing embedding slot or a previously recorded different identity requires a rebuild."""
    env = Env(monkeypatch)
    env.config.defaults.resolve = lambda role: None
    with pytest.raises(GraphOperationError, match="graph_embedding_identity_unconfigured"):
        await env.build()
    changed = Env(monkeypatch)
    changed.other.external_state = "present"
    changed.other.embedding_identity = {"model": "old", "version": "v", "dimensions": 3}
    with pytest.raises(
        GraphOperationError, match="graph_embedding_identity_changed_requires_rebuild"
    ):
        await changed.build()


@pytest.mark.asyncio
async def test_cleanup_context_cannot_infer_and_uses_disabled_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identifier cleanup denies authorize(); unresolved roles become an empty disabled mapping."""
    env = Env(monkeypatch, cleanup=True)
    env.config.defaults.resolve = lambda role: None
    context, _, _ = await env.build()
    with pytest.raises(PrivacyPolicyDenied):
        await context.authorize("embedding")
    assert context.embedding.mapping.__class__.__name__ == "ModelMapping"
    assert context.reranking is None


@pytest.mark.asyncio
async def test_required_role_unconfigured_blocks_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-cleanup context without the background role fails with the stable error."""
    env = Env(monkeypatch)
    env.config.defaults.resolve = lambda role: (
        SimpleNamespace(model="m", version="v", dimensions=3) if role == "embedding" else None
    )
    with pytest.raises(GraphOperationError, match="graph_model_role_unconfigured"):
        await env.build()


@pytest.mark.asyncio
async def test_validate_partition_checks_lease_inventory_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_partition renews the lease, compares the inventory and enforces terminal rules by mode."""
    env = Env(monkeypatch)
    context, _, _ = await env.build()
    await context.validate_partition("delete", None)
    env.calls["_lease"].assert_awaited_with(env.factory, env.adm, env.operation.id, env.token)
    await context.validate_partition("upsert", str(env.mapping.episode_id))
    env.other.status = "failed"
    with pytest.raises(GraphOperationError, match="graph_partition_requires_recovery"):
        await context.validate_partition("upsert", None)
    await context.validate_partition("reconcile", None)
    env.source.generation = 2
    with pytest.raises(GraphOperationError, match="graph_source_generation_terminal"):
        await context.validate_partition("search", None)
    env.calls["_inventory"].return_value = ([env.mapping], [env.support])
    with pytest.raises(GraphOperationError, match="graph_canonical_dependency_changed"):
        await context.validate_partition("delete", None)


@pytest.mark.asyncio
async def test_authorize_detects_policy_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """authorize revalidates the partition and refuses egress when the gateway policy digest changed."""
    env = Env(monkeypatch)
    context, _, _ = await env.build()
    await context.authorize("reasoning")
    env.config.configuration_revision = 2
    with pytest.raises(PrivacyPolicyDenied, match="Graph policy changed before egress"):
        await context.authorize("reasoning")


@pytest.mark.asyncio
async def test_fence_and_record_renew_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """fence() renews the lease; record() journals through _record and rebuild-absence markers need proof."""
    env = Env(monkeypatch)
    record = AsyncMock()
    monkeypatch.setattr(worker, "_record", record)
    context, _, _ = await env.build()
    async with context.fence():
        pass
    env.calls["_lease"].assert_awaited()
    receipt = SimpleNamespace(rebuild_absence_observed=None)
    await context.record_write_intent(receipt)  # type: ignore[arg-type]
    record.assert_awaited_once_with(env.factory, env.adm, env.operation.id, env.token, receipt)
    marker = SimpleNamespace(rebuild_absence_observed="node", entity_ids=(), fact_ids=())
    with pytest.raises(GraphOperationError, match="graph_rebuild_absence_marker_invalid"):
        await context.record_write_intent(marker)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_dispatch_journal_rejects_foreign_and_uncertain_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatch ownership must match the operation/partition and no unfinished dispatch may exist."""
    env = Env(monkeypatch)
    current = SimpleNamespace(id=env.operation.id)
    env.calls["_get"].return_value = current
    context, _, _ = await env.build()
    foreign = DispatchOwnership(uuid4(), str(env.partition_id), "a" * 40, 5)
    with pytest.raises(GraphOperationError, match="graph_dispatch_not_owned"):
        await context.record_dispatch_ownership(foreign)
    ownership = DispatchOwnership(env.operation.id, str(env.partition_id), "a" * 40, 5)
    env.factory.session.scalar.return_value = uuid4()
    with pytest.raises(GraphOperationError, match="graph_previous_dispatch_not_ceased"):
        await context.record_dispatch_ownership(ownership)
    env.factory.session.scalar.return_value = None
    await context.record_dispatch_ownership(ownership)
    env.factory.session.add.assert_called_once()
    assert (current.dispatch_server_run_id, current.dispatch_client_id) == ("a" * 40, 5)
    assert current.cessation_verified_at is None and current.dispatch_deadline is not None
    row = SimpleNamespace(lease_owner=env.token, completed_at=None)
    env.factory.session.scalar.return_value = row
    await context.record_dispatch_completion(ownership)
    assert row.completed_at is not None
    row.lease_owner = uuid4()
    with pytest.raises(GraphOperationError, match="graph_dispatch_completion_not_owned"):
        await context.record_dispatch_completion(ownership)


@pytest.mark.asyncio
async def test_cessation_requires_owned_journal_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cessation is authorized only for a journaled dispatch of this operation and partition."""
    env = Env(monkeypatch)
    context, _, _ = await env.build()
    ownership = DispatchOwnership(env.operation.id, str(env.partition_id), "a" * 40, 5)
    env.factory.session.scalar.return_value = SimpleNamespace()
    await context.authorize_dispatch_cessation(ownership)
    env.factory.session.scalar.return_value = None
    with pytest.raises(GraphOperationError, match="graph_cessation_not_owned"):
        await context.authorize_dispatch_cessation(ownership)


def _binding(env: Env) -> CanonicalEntityBinding:
    """Return a canonical binding for a graph node of this env's partition."""
    import hashlib

    membership = uuid4()
    evidence = EvidenceIdentity(
        env.source_id,
        1,
        env.mapping.document_id,
        env.mapping.document_version_id,
        env.support.chunk_id,
    )
    return CanonicalEntityBinding(
        canonical_entity_id=uuid4(),
        canonical_revision=1,
        membership_ids=(membership,),
        evidence=(evidence,),
        graph_entity_uuid=str(uuid4()),
        group_id=str(env.partition_id),
        mapping_revision=3,
        node_name="n",
        field_support=(("name", hashlib.sha256(b"n").hexdigest(), (membership,)),),
    )


@pytest.mark.asyncio
async def test_canonicalize_publishes_only_bound_synchronized_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facts need both endpoints bound, all supporting rows synchronized and live evidence."""
    env = Env(monkeypatch)
    left, right = _binding(env), _binding(env)
    env.bindings = (left, right)
    context, _, _ = await env.build()
    edge = SimpleNamespace(
        source_node_uuid=left.graph_entity_uuid,
        target_node_uuid=right.graph_entity_uuid,
        fact="f",
        valid_at=None,
        invalid_at=None,
        episodes=[str(env.mapping.episode_id)],
    )
    unbound = SimpleNamespace(**{**vars(edge), "target_node_uuid": str(uuid4())})
    unsynced = SimpleNamespace(
        **{**vars(edge), "episodes": [str(env.mapping.episode_id), str(uuid4())]}
    )
    result = await context.canonicalize([edge, unbound, unsynced])
    assert len(result) == 1 and result[0].fact == "f"
    assert result[0].entity_ids == (left.canonical_entity_id, right.canonical_entity_id)
    assert result[0].episode_ids == (str(env.mapping.episode_id),)
    env.mapping.status = "reconcile_needed"
    assert await context.canonicalize([edge]) == []


def _action(node_id: str, kind: str, **extra: Any) -> CanonicalNodeRecoveryAction:
    """Build a node recovery action."""
    return CanonicalNodeRecoveryAction(
        graph_entity_uuid=node_id,
        expected_current_state_fingerprint=extra.pop("fingerprint", None),
        action=kind,
        **extra,
    )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_node_authorization_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain needs a current binding and intended fingerprint; others need independent support rules."""
    env = Env(monkeypatch)
    binding = _binding(env)
    env.bindings = (binding,)
    context, _, _ = await env.build()
    node = binding.graph_entity_uuid
    intended = "e" * 64
    receipt = GraphWriteReceipt(
        operation_id=env.operation.id,
        lease_token=env.operation.receipt_token,
        episode_id=env.mapping.episode_id,
        group_id=str(env.partition_id),
        mapping_revision=3,
        desired_support_digest="a" * 64,
        phase="canonical_node_write_intent",
        entity_ids=(node,),
        prior_entity_state_fingerprints=((node, False, None),),
        intended_entity_state_fingerprints=((node, intended),),
    )
    await context.authorize_node_recovery(
        (receipt,), (_action(node, "retain", fingerprint=intended),)
    )
    env.calls["_recovery_authorization"].assert_awaited()
    with pytest.raises(GraphOperationError, match="graph_node_retained_fields_unproved"):
        await context.authorize_node_recovery(
            (receipt,), (_action(node, "retain", fingerprint="f" * 64),)
        )
    other = str(uuid4())
    with pytest.raises(GraphOperationError, match="graph_node_retained_fields_unproved"):
        await context.authorize_node_recovery(
            (receipt,), (_action(other, "retain", fingerprint=intended),)
        )
    await context.authorize_node_recovery((receipt,), (_action(other, "delete_orphan"),))
    with pytest.raises(GraphOperationError, match="graph_node_independent_support"):
        await context.authorize_node_recovery((receipt,), (_action(node, "delete_orphan"),))
    env.mapping.tombstoned = True
    with pytest.raises(GraphOperationError, match="graph_node_retained_fields_unproved"):
        await context.authorize_node_recovery(
            (receipt,), (_action(node, "retain", fingerprint=intended),)
        )


@pytest.mark.asyncio
async def test_fact_authorization_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fact deletion with surviving support needs a terminal deleted mapping and a rebuild-absence proof."""
    env = Env(monkeypatch)
    context, _, _ = await env.build()
    fact_id, source, target = str(uuid4()), str(uuid4()), str(uuid4())
    from modules.knowledge.temporal.adapter import ExactFactState, ExactFactSupport

    remaining = str(env.other.episode_id)
    state = ExactFactState(
        fact_id, source, target, (str(env.mapping.episode_id), remaining), "a" * 64, True
    )
    support = ExactFactSupport(
        fact_id=fact_id,
        episode_ids=(str(env.mapping.episode_id), remaining),
        source_node_id=source,
        target_node_id=target,
        state_fingerprint="a" * 64,
    )
    receipt = GraphWriteReceipt(
        operation_id=env.operation.id,
        lease_token=env.operation.receipt_token,
        episode_id=env.mapping.episode_id,
        group_id=str(env.partition_id),
        mapping_revision=3,
        desired_support_digest="a" * 64,
        phase="bulk_write_intent",
        episode_state_fingerprint="b" * 64,
        prior_episode_state_fingerprint="c" * 64,
        fact_ids=(fact_id,),
        existing_fact_states=(state,),
        intended_fact_support=(support,),
    )
    action = ExactFactRecoveryAction(
        fact_id=fact_id,
        source_node_id=source,
        target_node_id=target,
        expected_current_state_fingerprint="a" * 64,
        action="delete_unsupported",
    )
    with pytest.raises(GraphOperationError, match="graph_fact_surviving_support"):
        await context.authorize_fact_recovery((receipt,), (action,))
    env.other.tombstoned, env.other.external_state = True, "absent"
    env.calls["_get"].return_value = SimpleNamespace(
        partition_id=uuid4(), cleanup_completed_at=None
    )
    with pytest.raises(GraphOperationError, match="graph_rebuild_absence_proof_missing"):
        await context.authorize_fact_recovery((receipt,), (action,))
