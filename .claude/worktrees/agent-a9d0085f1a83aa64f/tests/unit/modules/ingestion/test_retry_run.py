"""Characterization of ``retry_run`` and its stage-selection / lease helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.errors import DomainError
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import _runs
from modules.ingestion import public as ingestion

from .patching import patch_ingestion

WORKSPACE = uuid4()
SOURCE_ID = uuid4()
RUN_ID = uuid4()
SCOPE = InternalJobScope(workspace_id=WORKSPACE, actor_user_id=7, membership_revision=3)
ACCESS = AccessFence(WORKSPACE, 7, 3, 5)
NOW = datetime.now(UTC)


def stage(key: str, status: str) -> Any:
    """Build a stage row stand-in."""
    return SimpleNamespace(
        id=uuid4(),
        stage_key=key,
        status=status,
        attempts=3,
        error_code="boom",
        next_attempt_at=None,
        lease_expires_at=NOW,
    )


@pytest.mark.parametrize(
    "statuses,requested,expected",
    [
        ([("receive", "running")], None, None),
        ([("receive", "succeeded"), ("normalize", "succeeded")], None, None),
        ([("receive", "cancelled")], None, None),
        ([("normalize", "failed"), ("receive", "failed")], None, "receive"),
        ([("normalize", "failed"), ("receive", "failed")], "normalize", "normalize"),
        ([("parse_file", "failed"), ("zzz", "failed")], None, "parse_file"),
    ],
)
def test_choose_retry_stage(statuses, requested, expected):
    """The earliest failed stage in pipeline order is chosen; non-retryable runs select nothing."""
    chosen = _runs._choose_retry_stage([stage(k, s) for k, s in statuses], requested)
    assert (chosen.stage_key if chosen else None) == expected


@pytest.mark.parametrize(
    "statuses,requested,detail",
    [
        ([("receive", "pending")], "receive", "Ingestion run still has an active stage"),
        ([("receive", "failed")], "normalize", "Requested stage is not retryable"),
    ],
)
def test_choose_retry_stage_conflicts(statuses, requested, detail):
    """A requested retry against an active run, or of a stage that did not fail, is a 409."""
    with pytest.raises(DomainError) as exc:
        _runs._choose_retry_stage([stage(k, s) for k, s in statuses], requested)
    assert (exc.value.status_code, exc.value.detail) == (409, detail)


def test_reacquire_collection_lease_rules():
    """The lease moves to the run unless another live run owns it or ownership state is inconsistent."""
    run = SimpleNamespace(id=RUN_ID)
    state = SimpleNamespace(
        lease_run_id=None, lease_expires_at=None, collection_lease_token=uuid4()
    )
    _runs._reacquire_collection_lease(state, run)
    assert (
        state.lease_run_id == RUN_ID
        and state.collection_lease_token is None
        and state.lease_expires_at > NOW
    )
    for bad in (
        SimpleNamespace(lease_run_id=uuid4(), lease_expires_at=None, collection_lease_token=None),
        SimpleNamespace(
            lease_run_id=uuid4(),
            lease_expires_at=NOW + timedelta(minutes=5),
            collection_lease_token=None,
        ),
        SimpleNamespace(
            lease_run_id=None,
            lease_expires_at=NOW + timedelta(minutes=5),
            collection_lease_token=None,
        ),
    ):
        with pytest.raises(DomainError):
            _runs._reacquire_collection_lease(bad, run)
    own = SimpleNamespace(
        lease_run_id=RUN_ID,
        lease_expires_at=NOW + timedelta(minutes=5),
        collection_lease_token=None,
    )
    _runs._reacquire_collection_lease(own, run)  # a run may renew its own live lease


class Session:
    """Scripted session for the retry flow."""

    def __init__(
        self, *, state: Any, run: Any, stages: list[Any], prior: Any, failed_progress: Any = None
    ) -> None:
        self.state, self.run, self.stages, self.prior = state, run, stages, prior
        self.failed_progress = failed_progress
        self.log: list[str] = []
        self.hint = SimpleNamespace(source_id=SOURCE_ID)

    async def scalar(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
        """Answer by entity: hint run, locked run, prior event or failed-progress probe."""
        entity = stmt.column_descriptions[0]["entity"].__name__
        self.log.append(f"scalar:{entity}")
        if entity == "IngestionRun":
            return self.hint if self.log.count("scalar:IngestionRun") == 1 else self.run
        if entity == "EventOutbox":
            return self.prior
        return self.failed_progress

    async def get(self, model: Any, ident: Any, **kw: Any) -> Any:
        """Lock the state row."""
        self.log.append("get:state")
        return self.state

    async def scalars(self, *_a: Any, **_k: Any) -> Any:
        """Return the run's stages."""
        self.log.append("scalars:stages")
        return SimpleNamespace(all=lambda: self.stages)

    async def refresh(self, obj: Any) -> None:
        """Record the final refresh."""
        self.log.append("refresh")


async def run_retry(
    session: Session, requested: str | None = None, *, source_status: str = "active"
) -> Any:
    """Invoke ``retry_run`` with admission, Source lock and publication replaced by recorders."""
    fence = SimpleNamespace(id=SOURCE_ID, status=source_status, generation=1)

    async def lock_set(*_a, **_k):
        session.log.append("lock_source_set")
        return SimpleNamespace(fences=(fence,), access_fence=ACCESS)

    async def publish(sess, event, **kw):
        session.log.append(f"publish:{event.type}")
        session.published = event

    async def commit(sess, changes, **kw):
        session.log.append("commit_with_replay")
        session.changes = changes

    with (
        patch_ingestion("_admit_ingestion_scope", return_value=None),
        patch_ingestion("publish_event", publish),
        patch_ingestion("commit_with_replay", commit),
        patch_ingestion("make_ingestion_change", lambda *a, **k: ("change", a[3], a[4])),
    ):
        from modules.sources import public as sources

        original = sources.lock_source_set
        sources.lock_source_set = lock_set
        try:
            return await ingestion.retry_run(
                session, RUN_ID, requested, scope=SCOPE, multi_workspace_enabled=False
            )
        finally:
            sources.lock_source_set = original


def make_session(
    stage_key: str = "receive", *, generation: Any = 1, state: Any = "default", **kw: Any
) -> Session:
    """Build a session with one failed stage and a prior event carrying ``generation``."""
    failed = stage(stage_key, "failed")
    run = SimpleNamespace(id=RUN_ID, status="failed", error_code="x")
    prior = SimpleNamespace(
        type="ingestion.stage.requested",
        payload={
            "run_id": str(RUN_ID),
            "stage_id": str(failed.id),
            "source_generation": generation,
        },
    )
    if state == "default":
        state = SimpleNamespace(
            lease_run_id=None, lease_expires_at=None, collection_lease_token=None
        )
    return Session(state=state, run=run, stages=[failed], prior=prior, **kw)


@pytest.mark.asyncio
async def test_retry_resets_the_stage_republishes_and_reacquires_the_lease():
    """Accepted retry: lock order, stage reset, copied event payload, lease for collection stages, one commit."""
    session = make_session()
    run = await run_retry(session)
    assert run is session.run and run.status == "queued" and run.error_code is None
    failed = session.stages[0]
    assert (failed.status, failed.attempts, failed.error_code, failed.lease_expires_at) == (
        "pending",
        0,
        None,
        None,
    )
    assert session.log == [
        "scalar:IngestionRun",
        "lock_source_set",
        "get:state",
        "scalar:IngestionRun",
        "scalars:stages",
        "scalar:EventOutbox",
        "publish:ingestion.stage.requested",
        "commit_with_replay",
        "refresh",
    ]
    assert (
        session.published.payload == session.prior.payload
        and session.published.producer == "modules.ingestion"
    )
    assert session.state.lease_run_id == RUN_ID and session.state.lease_expires_at > NOW
    assert session.changes == [("change", "receive", "pending")]


@pytest.mark.asyncio
async def test_retry_of_a_non_collection_stage_leaves_the_state_lease_alone():
    """Normalization retry only checks the failed-progress probe; the Source lease is untouched."""
    session = make_session("normalize")
    await run_retry(session)
    assert session.log.count("scalar:ObservationNormalization") == 1
    assert session.state.lease_run_id is None


@pytest.mark.asyncio
async def test_retry_conflicts():
    """Every guard is a 409 and nothing is published or committed."""
    cases = [
        (make_session(generation=2), "Failed stage belongs to an obsolete source generation"),
        (make_session(generation="1"), "Failed stage belongs to an obsolete source generation"),
        (
            make_session("normalize", failed_progress=uuid4()),
            "Invalid observations require correction before normalization retry",
        ),
        (
            make_session(
                state=SimpleNamespace(
                    lease_run_id=None,
                    lease_expires_at=NOW + timedelta(minutes=5),
                    collection_lease_token=uuid4(),
                )
            ),
            "Source collection is already in progress",
        ),
        (
            make_session(
                state=SimpleNamespace(
                    lease_run_id=uuid4(),
                    lease_expires_at=NOW + timedelta(minutes=5),
                    collection_lease_token=None,
                )
            ),
            "Source already has an active collection run",
        ),
    ]
    for session, detail in cases:
        with pytest.raises(DomainError) as exc:
            await run_retry(session)
        assert (exc.value.status_code, exc.value.detail) == (409, detail), detail
        assert "commit_with_replay" not in session.log


@pytest.mark.asyncio
async def test_retry_edge_cases():
    """Missing prior event, inactive Source, stage-less run, absent run and expired stale reservation."""
    session = make_session()
    session.prior = None
    with pytest.raises(DomainError) as exc:
        await run_retry(session)
    assert exc.value.detail == "Failed stage has no durable retry event"

    with pytest.raises(DomainError) as exc2:
        await run_retry(make_session(), source_status="paused")
    assert exc2.value.detail == "Source is not active"

    empty = make_session()
    empty.stages = []
    with pytest.raises(RuntimeError, match="no stage"):
        await run_retry(empty)

    gone = make_session()
    gone.hint = None
    assert await run_retry(gone) is None

    stale = make_session(
        state=SimpleNamespace(
            lease_run_id=None,
            lease_expires_at=NOW - timedelta(minutes=1),
            collection_lease_token=uuid4(),
        )
    )
    await run_retry(stale)
    assert (
        stale.state.collection_lease_token is None
    )  # an expired reservation is cleared before retrying
