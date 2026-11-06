"""Branch tests for the pure row-mutation helpers extracted from the long provisioning functions."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import provisioning
from modules.connectors.provisioning import _activation, _workflow_steps

SOURCE_ID = uuid4()
WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(
    workspace_id=WORKSPACE_ID,
    actor_user_id=7,
    membership_revision=3,
    source_id=SOURCE_ID,
    source_generation=2,
)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 5)


def _row(**over: object) -> SimpleNamespace:
    base = {
        "activation_intent": None,
        "workflow_operation": None,
        "workflow_id": None,
        "workflow_name": "",
        "state": "saved_not_active",
        "error_code": None,
        "desired_enabled": False,
        "applied_revision": 0,
        "execution_backend": "n8n",
        "backend_revision": 1,
        "applied_backend_revision": 0,
        "transition_phase": "idle",
        "target_backend": None,
        "transition_operation_id": None,
        "old_workflow_id": None,
        "template_revision": 0,
        "applied_template_revision": 0,
    }
    return SimpleNamespace(**(base | over))


def test_activation_retirement_releases_prepared_slots_and_drops_the_intent() -> None:
    credential = SimpleNamespace(
        operation_envelope={"activation_id": "a1", "state": "prepared"},
        operation_id=uuid4(),
        state="dispatching",
        credential_id="c1",
    )
    row = _row(activation_intent={"id": "a1", "required_credentials": {"collector": {}}})
    provisioning._retire_activation_for_saved_configuration(row, {"collector": credential})  # type: ignore[arg-type]
    assert row.activation_intent is None and row.state == "saved_not_active"
    assert (credential.operation_id, credential.operation_envelope, credential.state) == (
        None,
        None,
        "ready",
    )


def test_activation_retirement_keeps_unresolved_dispatched_operations_for_reconciliation() -> None:
    credential = SimpleNamespace(
        operation_envelope={"activation_id": "a1", "state": "unknown"}, state="dispatching"
    )
    row = _row(activation_intent={"id": "a1", "required_credentials": {"collector": {}}})
    provisioning._retire_activation_for_saved_configuration(row, {"collector": credential})  # type: ignore[arg-type]
    assert row.activation_intent["state"] == "stale_unresolved"  # type: ignore[index]
    assert (row.state, row.error_code) == ("reconciliation_required", "activation_outcome_pending")
    assert credential.state == "dispatching"


def test_workflow_operation_retirement_branches() -> None:
    prepared_deactivate = _row(
        workflow_operation={"kind": "deactivate", "step": {"state": "prepared"}}
    )
    provisioning._retire_workflow_operation_for_saved_configuration(
        prepared_deactivate,
        prior_enabled=True,
        source_generation=2,
        scope=SCOPE,
        access_fence=FENCE,
    )
    assert (
        prepared_deactivate.error_code == "deactivation_pending"
        and prepared_deactivate.workflow_operation is not None
    )

    prepared_create = _row(
        workflow_operation={"kind": "create", "step": {"state": "blocked"}}, error_code="x"
    )
    provisioning._retire_workflow_operation_for_saved_configuration(
        prepared_create, prior_enabled=False, source_generation=2, scope=SCOPE, access_fence=FENCE
    )
    assert prepared_create.workflow_operation is None and prepared_create.error_code is None

    dispatched = _row(workflow_operation={"kind": "update", "step": {"state": "dispatched"}})
    provisioning._retire_workflow_operation_for_saved_configuration(
        dispatched, prior_enabled=True, source_generation=2, scope=SCOPE, access_fence=FENCE
    )
    assert dispatched.workflow_operation["cleanup_required"] is True  # type: ignore[index]
    assert dispatched.error_code == "workflow_operation_pending"

    idle_enabled = _row(workflow_id="w1")
    with patch.object(_activation, "_new_deactivation", return_value={"kind": "deactivate"}):
        provisioning._retire_workflow_operation_for_saved_configuration(
            idle_enabled, prior_enabled=True, source_generation=2, scope=SCOPE, access_fence=FENCE
        )
    assert (
        idle_enabled.workflow_operation == {"kind": "deactivate"}
        and idle_enabled.error_code == "deactivation_pending"
    )

    never_enabled = _row()
    provisioning._retire_workflow_operation_for_saved_configuration(
        never_enabled, prior_enabled=False, source_generation=2, scope=SCOPE, access_fence=FENCE
    )
    assert never_enabled.workflow_operation is None


def _intent(**over: object) -> dict[str, object]:
    return {
        "operation_id": str(uuid4()),
        "kind": "create",
        "credential_type": "httpHeaderAuth",
        "target_id": None,
        "binding": {"b": 1},
        "input_ciphertext": "ct",
    } | over


def test_stage_credential_intent_adds_new_slot_or_updates_existing() -> None:
    session = MagicMock()
    activation_id = uuid4()
    provisioning._stage_credential_intent(
        session,
        SOURCE_ID,
        "collector",
        _intent(),
        None,
        activation_id=activation_id,
        source_generation=2,
        revision=4,
        scope=SCOPE,
        access_fence=FENCE,
    )
    added = session.add.call_args.args[0]
    assert added.slot == "collector" and added.state == "queued"
    assert added.operation_envelope["activation_id"] == str(activation_id)
    assert (
        added.operation_envelope["state"] == "prepared"
        and added.operation_envelope["revision"] == 4
    )

    existing = SimpleNamespace(state="ready", error_code="e", credential_id="c1")
    provisioning._stage_credential_intent(
        session,
        SOURCE_ID,
        "collector",
        _intent(target_id="c1"),
        existing,
        activation_id=activation_id,  # type: ignore[arg-type]
        source_generation=2,
        revision=4,
        scope=SCOPE,
        access_fence=FENCE,
    )
    assert (existing.state, existing.error_code, existing.operation_revision) == ("queued", None, 4)
    assert session.add.call_count == 1


def test_required_credentials_bind_to_intents_or_ready_slots() -> None:
    intents = {"collector": {"operation_id": "op-1", "target_id": "c9"}}
    required: dict[str, dict[str, object]] = {"collector": {}, "provider": {"binding": {"p": 1}}}
    ready = SimpleNamespace(state="ready", credential_id="c2", resolved_binding={"p": 1})
    assert provisioning._bind_required_credentials(required, intents, {"provider": ready}) is True  # type: ignore[arg-type]
    assert required["collector"] == {"operation_id": "op-1", "credential_id": "c9"}
    assert required["provider"] == {
        "binding": {"p": 1},
        "credential_id": "c2",
        "operation_id": None,
    }
    for slot in (
        None,
        SimpleNamespace(state="queued", credential_id="c2", resolved_binding={"p": 1}),
        SimpleNamespace(state="ready", credential_id="c2", resolved_binding={"other": 1}),
    ):
        assert (
            provisioning._bind_required_credentials(
                {"provider": {"binding": {"p": 1}}}, {}, {"provider": slot} if slot else {}
            )
            is False
        )  # type: ignore[arg-type]


def _step(kind: str, **over: object) -> dict[str, object]:
    return {"id": "s1", "kind": kind, "state": "dispatched", "target": "w1", "history": []} | over


def test_acknowledgement_matching_rules() -> None:
    row = _row(workflow_id="w1")
    operation = {"id": str(uuid4())}
    original = dict(operation)

    def matches(kind: str, workflow_id: str | None, **step_over: object) -> bool:
        with patch.object(_workflow_steps, "_retained_operation_matches", return_value=True):
            return provisioning._acknowledgement_matches(
                row,
                {**operation, "workflow_id": "w1"},
                _step(kind, **step_over),
                original,  # type: ignore[arg-type]
                source_id=SOURCE_ID,
                operation_id=__import__("uuid").UUID(operation["id"]),
                step_id="s1",
                workflow_id=workflow_id,
                scope=SCOPE,
                access_fence=FENCE,
            )

    assert matches("activate", None) is True
    assert matches("update", "w1") is True and matches("update", "other") is False
    assert (
        matches("create", "w2", target=None) is True
        and matches("create", None, target=None) is False
    )
    assert matches("activate", None, state="succeeded") is False
    assert matches("activate", None, target="elsewhere") is False


def test_apply_acknowledged_step_per_kind() -> None:
    source = SimpleNamespace(status="active")
    # current create chains to activate
    row, op = _row(), {"revision": 3}
    provisioning._apply_acknowledged_step(row, source, op, kind="create", current=True, target="w1")  # type: ignore[arg-type]
    assert (
        op["phase"] == "activate"
        and row.workflow_operation is op
        and op["step"]["kind"] == "activate"
    )  # type: ignore[index]
    # stale update chains to deactivate cleanup
    row, op = _row(), {"revision": 3}
    provisioning._apply_acknowledged_step(
        row, source, op, kind="update", current=False, target="w1"
    )  # type: ignore[arg-type]
    assert (op["phase"], op["cleanup_required"], row.error_code, row.state) == (
        "deactivate",
        True,
        "deactivation_pending",
        "saved_not_active",
    )
    # current activate finalizes
    row, op = (
        _row(activation_intent={"id": "a"}, workflow_operation=object()),
        {"revision": 3, "activation_id": "a"},
    )
    provisioning._apply_acknowledged_step(
        row, source, op, kind="activate", current=True, target="w1"
    )  # type: ignore[arg-type]
    assert (row.state, row.applied_revision, row.workflow_operation, row.activation_intent) == (
        "active",
        3,
        None,
        None,
    )
    assert row.transition_phase == "idle" and row.applied_backend_revision == row.backend_revision
    # stale activate disables desired state
    row, op = _row(desired_enabled=True), {"revision": 3}
    provisioning._apply_acknowledged_step(
        row, source, op, kind="activate", current=False, target="w1"
    )  # type: ignore[arg-type]
    assert row.desired_enabled is False and op["phase"] == "deactivate"
    # deactivate clears the operation and a cleanup-owned intent
    row, op = (
        _row(activation_intent={"id": "a"}, workflow_operation=object()),
        {"cleanup_required": True, "activation_id": "a"},
    )
    provisioning._apply_acknowledged_step(
        row, None, op, kind="deactivate", current=False, target="w1"
    )  # type: ignore[arg-type]
    assert (row.workflow_operation, row.activation_intent, row.state) == (None, None, "disabled")
    # unknown kinds are recorded as unsupported
    row, op = _row(desired_enabled=True), {}
    provisioning._apply_acknowledged_step(
        row, source, op, kind="mystery", current=False, target=None
    )  # type: ignore[arg-type]
    assert (row.error_code, op["error"], row.state) == (
        "workflow_operation_unsupported",
        "unsupported_workflow_step",
        "saved_not_active",
    )
