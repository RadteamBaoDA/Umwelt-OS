"""publish_event accepts only the exact operation-only Documents cleanup envelope (D2a-3 publisher half)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, uuid5

import pytest
from fastapi import HTTPException

from core.events import DomainEvent
from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.ingestion import public as ingestion
from modules.knowledge.documents.schemas import DocumentCleanupJobIdentity

WS, SRC, OP = uuid4(), uuid4(), uuid4()
SCOPE = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=3)


def _event(**overrides) -> DomainEvent:
    values = {
        "id": uuid5(OP, "document-cleanup-requested"), "type": "document.cleanup.requested", "version": 1,
        "occurred_at": datetime.now(UTC), "producer": "modules.knowledge.documents",
        "payload": {"operation_id": str(OP)},
    }
    return DomainEvent(**{**values, **overrides})


def _identity(**overrides) -> DocumentCleanupJobIdentity:
    values = {"operation_id": OP, "workspace_id": WS, "actor_user_id": 7, "membership_revision": 3,
              "configuration_revision": 2, "source_id": SRC, "source_generation": 5, "document_id": uuid4()}
    return DocumentCleanupJobIdentity(**{**values, **overrides})


async def _publish(event, identity, scope=SCOPE):
    session = MagicMock()
    reader = AsyncMock(return_value=identity)
    with patch.object(ingestion, "_admit_ingestion_scope", AsyncMock(return_value=AccessFence(WS, 7, 3, 2))), \
            patch.object(ingestion.documents, "read_document_cleanup_job_identity", reader):
        await ingestion.publish_event(session, event, scope=scope, multi_workspace_enabled=True)
    return session, reader


async def test_exact_payload_is_preserved_with_principal_from_scope() -> None:
    session, reader = await _publish(_event(), _identity())
    outbox = session.add.call_args.args[0]
    assert outbox.payload == {"operation_id": str(OP)}  # no identity merge
    assert (outbox.workspace_id, outbox.actor_user_id, outbox.membership_revision) == (WS, 7, 3)
    assert reader.await_args.args[1] == OP and reader.await_args.kwargs["scope"] == SCOPE


@pytest.mark.parametrize("override", [
    {"payload": {"operation_id": str(OP), "workspace_id": str(WS)}},
    {"payload": {}},
    {"payload": {"operation_id": str(OP).upper()}},
    {"payload": {"operation_id": "not-a-uuid"}},
    {"id": uuid4()},
    {"producer": "modules.sources"},
    {"version": 2},
])
async def test_non_exact_envelope_is_value_error(override) -> None:
    with pytest.raises(ValueError, match="exact operation-only envelope"):
        await _publish(_event(**override), _identity())


async def test_extra_payload_identity_with_wrong_principal_is_not_found() -> None:
    event = _event(payload={"operation_id": str(OP), "actor_user_id": 99})
    with pytest.raises(HTTPException) as exc:
        await _publish(event, _identity())
    assert exc.value.status_code == 404


@pytest.mark.parametrize("identity", [None, "workspace", "actor", "membership"])
async def test_identity_missing_or_mismatching_is_404(identity) -> None:
    value = {"workspace": _identity(workspace_id=uuid4()), "actor": _identity(actor_user_id=8),
             "membership": _identity(membership_revision=4), None: None}[identity]
    with pytest.raises(HTTPException) as exc:
        await _publish(_event(), value)
    assert (exc.value.status_code, exc.value.detail) == (404, "Document cleanup receipt not found")


async def test_source_bound_scope_requires_matching_source_and_generation() -> None:
    bound = InternalJobScope(WS, 7, 3, SRC, 5)
    session, _ = await _publish(_event(), _identity(), scope=bound)
    assert session.add.call_args.args[0].payload == {"operation_id": str(OP)}
    for other in (_identity(source_id=uuid4()), _identity(source_generation=6)):
        with pytest.raises(HTTPException) as exc:
            await _publish(_event(), other, scope=bound)
        assert exc.value.status_code == 404
