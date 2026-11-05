"""Characterization of ``publish_event`` (the module's stable outbox publication seam)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.errors import DomainError
from core.events import DomainEvent
from core.ids import uuid7
from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.ingestion import _event_publisher as event_publisher
from modules.ingestion import public as ingestion
from modules.ingestion.models import EventOutbox

from .patching import patch_ingestion

WORKSPACE = uuid4()
SOURCE_ID = uuid4()
RUN_ID = uuid4()
STAGE_ID = uuid4()
SCOPE = InternalJobScope(workspace_id=WORKSPACE, actor_user_id=7, membership_revision=3)
BOUND = InternalJobScope(
    workspace_id=WORKSPACE, actor_user_id=7, membership_revision=3, source_id=SOURCE_ID, source_generation=1,
)


class Session:
    """Scalar results are consumed in order; ``add`` records the outbox row."""

    def __init__(self, *scalars: Any) -> None:
        self.scalars = list(scalars)
        self.added: list[Any] = []

    async def scalar(self, *_a: Any, **_k: Any) -> Any:
        """Pop the next scripted lookup result."""
        return self.scalars.pop(0)

    def add(self, obj: Any) -> None:
        """Record the inserted outbox row."""
        self.added.append(obj)


def event(event_type: str = "ingestion.stage.requested", payload: dict[str, Any] | None = None, *,
          producer: str = "modules.ingestion") -> DomainEvent:
    """Build a domain event with a default run/stage payload."""
    body = payload if payload is not None else {
        "run_id": str(RUN_ID), "stage_id": str(STAGE_ID), "source_generation": 1,
    }
    return DomainEvent(
        id=uuid7(), type=event_type, version=1, occurred_at=datetime.now(UTC), producer=producer, payload=body,
    )


async def publish(session: Session, domain_event: DomainEvent, scope: Any = SCOPE) -> None:
    """Call ``publish_event`` with admission replaced by a no-op."""
    with patch_ingestion("_admit_ingestion_scope", return_value=None):
        await ingestion.publish_event(session, domain_event, scope=scope, multi_workspace_enabled=False)


@pytest.mark.asyncio
async def test_run_event_is_stamped_with_principal_and_source_and_added_pending():
    """A run/stage event resolves its lineage, gains the principal fields and the run's Source id."""
    session = Session(SimpleNamespace(id=RUN_ID, batch_id=uuid4(), source_id=SOURCE_ID),
                      SimpleNamespace(source_generation=1), STAGE_ID)
    await publish(session, event())
    (row,) = session.added
    assert isinstance(row, EventOutbox) and row.status == "pending"
    assert row.payload["source_id"] == str(SOURCE_ID)
    assert row.payload["workspace_id"] == str(WORKSPACE) and row.payload["actor_user_id"] == 7
    assert row.payload["membership_revision"] == 3
    assert (row.workspace_id, row.actor_user_id, row.membership_revision) == (WORKSPACE, 7, 3)


@pytest.mark.asyncio
async def test_event_without_a_run_is_stamped_without_lineage_lookups():
    """Events that name no run only receive the principal identity."""
    session = Session()
    await publish(session, event("ingestion.stage.requested", {"note": "x"}))
    assert session.added[0].payload == {
        "note": "x", "workspace_id": str(WORKSPACE), "actor_user_id": 7, "membership_revision": 3,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [{"workspace_id": str(uuid4())}, {"actor_user_id": "7"}, {"membership_revision": 4}])
async def test_a_claimed_principal_field_must_match_exactly(claimed):
    """Pre-filled identity fields are never overwritten: a different value or type is a 404."""
    with pytest.raises(DomainError) as exc:
        await publish(Session(), event("ingestion.stage.requested", dict(claimed)))
    assert (exc.value.status_code, exc.value.detail) == (404, "Event identity not found")


LINEAGE_FAILURES = [
    ([None], "Event run not found"),
    ([SimpleNamespace(id=RUN_ID, batch_id=uuid4(), source_id=SOURCE_ID), None], "Event batch lineage not found"),
    ([SimpleNamespace(id=RUN_ID, batch_id=uuid4(), source_id=SOURCE_ID), SimpleNamespace(source_generation=9)],
     "Event batch lineage not found"),
    ([SimpleNamespace(id=RUN_ID, batch_id=uuid4(), source_id=SOURCE_ID), SimpleNamespace(source_generation=1), None],
     "Event stage not found"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scalars,detail", LINEAGE_FAILURES)
async def test_run_lineage_failures_are_404(scalars, detail):
    """Missing run, batch, generation mismatch or stage are 404 and nothing is added."""
    session = Session(*scalars)
    with pytest.raises(DomainError) as exc:
        await publish(session, event())
    assert (exc.value.status_code, exc.value.detail) == (404, detail) and session.added == []


@pytest.mark.asyncio
async def test_source_mismatch_malformed_ids_and_bound_scope():
    """A payload Source differing from the run's is 404; malformed ids are ValueError; bound scopes are strict."""
    run = SimpleNamespace(id=RUN_ID, batch_id=uuid4(), source_id=SOURCE_ID)
    with pytest.raises(DomainError) as exc:
        await publish(Session(run, SimpleNamespace(source_generation=1)),
                      event(payload={"run_id": str(RUN_ID), "source_generation": 1, "source_id": str(uuid4())}))
    assert exc.value.detail == "Event source not found"
    with pytest.raises(ValueError, match="run identity"):
        await publish(Session(), event(payload={"run_id": "nope"}))
    with pytest.raises(ValueError, match="stage identity"):
        await publish(Session(run, SimpleNamespace(source_generation=1)),
                      event(payload={"run_id": str(RUN_ID), "source_generation": 1, "stage_id": "nope"}))
    session = Session(run, SimpleNamespace(source_generation=1), STAGE_ID)
    await publish(session, event(), scope=BOUND)  # bound scope sees its own Source and generation
    other = Session(SimpleNamespace(id=RUN_ID, batch_id=uuid4(), source_id=uuid4()), SimpleNamespace(source_generation=1),
                    STAGE_ID)
    with pytest.raises(DomainError) as exc2:
        await publish(other, event(), scope=BOUND)
    assert exc2.value.detail == "Event Source identity not found"


@pytest.mark.asyncio
async def test_member_workspace_scope_still_uses_the_actor_identity():
    """A workspace member context is stamped with its own user id."""
    member = WorkspaceContext(workspace_id=WORKSPACE, user_id=9, membership_revision=3, role="owner")
    session = Session()
    await publish(session, event("ingestion.stage.requested", {}), scope=member)
    assert session.added[0].actor_user_id == 9


@pytest.mark.asyncio
async def test_cleanup_event_requires_a_matching_documents_receipt(monkeypatch):
    """The cleanup event is proved by Documents' retained receipt (workspace/actor/epoch/Source)."""
    from modules.knowledge.documents import public as documents

    operation_id = uuid4()
    monkeypatch.setattr(event_publisher, "_document_cleanup_operation_id", lambda *a: operation_id)
    good = SimpleNamespace(workspace_id=WORKSPACE, actor_user_id=7, membership_revision=3,
                           source_id=SOURCE_ID, source_generation=1)

    async def read(session, op, **kw):
        return read.result

    monkeypatch.setattr(documents, "read_document_cleanup_job_identity", read)
    for result, ok in ((good, True), (None, False), (SimpleNamespace(**{**good.__dict__, "actor_user_id": 8}), False),
                       (SimpleNamespace(**{**good.__dict__, "source_id": uuid4()}), False)):
        read.result = result
        session = Session()
        if ok:
            await publish(session, event("document.cleanup.requested", {"operation": "x"}), scope=BOUND)
            assert session.added[0].payload == {"operation": "x"}  # operation-only payload is preserved
        else:
            with pytest.raises(DomainError) as exc:
                await publish(session, event("document.cleanup.requested", {"operation": "x"}), scope=BOUND)
            assert exc.value.detail == "Document cleanup receipt not found"


@pytest.mark.asyncio
async def test_purge_event_requires_producer_identity_and_matching_receipt(monkeypatch):
    """Source purge events need the exact retained producer subject and the same receipt scope."""
    from modules.sources import public as sources

    retained = SimpleNamespace(tag="scope")
    monkeypatch.setattr(event_publisher, "_source_purge_event_subject", lambda *a, **k: subject.value)

    class Subject:
        value: Any = None

    subject = Subject()

    async def read(session, op, **kw):
        return receipt.value

    receipt = Subject()
    monkeypatch.setattr(sources, "read_source_purge_job_identity", read)
    subject.value = None
    with pytest.raises(ValueError, match="retained producer identity"):
        await publish(Session(), event("source.purge.requested", {}))
    subject.value = (uuid4(), retained)
    receipt.value = SimpleNamespace(tag="other")
    with pytest.raises(DomainError) as exc:
        await publish(Session(), event("source.purge.requested", {}))
    assert exc.value.detail == "Source purge receipt not found"
    receipt.value = retained
    session = Session()
    await publish(session, event("source.purge.requested", {}))
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_ready_event_proves_lineage_and_ingestion_journal(monkeypatch):
    """Ready events validate the producer payload, Documents' locator and, for Ingestion, the journal row."""
    from modules.knowledge.documents import public as documents

    document_id, version_id = uuid4(), uuid4()
    payload = {
        "source_id": str(SOURCE_ID), "document_id": str(document_id), "document_version_id": str(version_id),
        "source_generation": 1, "version_number": 1, "workspace_id": str(WORKSPACE), "actor_user_id": 7,
        "membership_revision": 3,
    }

    async def locator(session, version, **kw):
        return locator.value

    locator.value = (document_id, SOURCE_ID)
    monkeypatch.setattr(documents, "review_version_locator", locator)
    session = Session(uuid4())
    await publish(session, event("document.version.ready", dict(payload)))
    assert session.added[0].type == "document.version.ready"

    with pytest.raises(DomainError) as exc:
        await publish(Session(None), event("document.version.ready", dict(payload)))
    assert exc.value.detail == "Ready normalization lineage not found"

    locator.value = (uuid4(), SOURCE_ID)
    with pytest.raises(DomainError) as exc2:
        await publish(Session(uuid4()), event("document.version.ready", dict(payload)))
    assert exc2.value.detail == "Ready document lineage not found"

    with pytest.raises(ValueError, match="exact scoped producer payload"):
        await publish(Session(), event("document.version.ready", {**payload, "extra": 1}))

    locator.value = (document_id, SOURCE_ID)
    documents_session = Session()  # the Documents producer needs no journal lookup
    await publish(documents_session, event("news.document.ready", dict(payload), producer="modules.knowledge.documents"))
    assert len(documents_session.added) == 1
