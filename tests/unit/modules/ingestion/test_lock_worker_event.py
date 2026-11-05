"""Characterization of ``_lock_worker_event``: admission, lock ORDER and the early-exit rules per event type."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.errors import ConflictError
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import worker

NOW = datetime(2026, 1, 2, tzinfo=UTC)
WORKSPACE = uuid4()
SOURCE_ID = uuid4()
RUN_ID = uuid4()
STAGE_ID = uuid4()
BATCH_ID = uuid4()
EVENT_ID = uuid4()
DOC_ID = uuid4()
SCOPE = InternalJobScope(
    workspace_id=WORKSPACE,
    actor_user_id=7,
    membership_revision=3,
    source_id=SOURCE_ID,
    source_generation=1,
)
ACCESS = AccessFence(WORKSPACE, 7, 3, 5)
SOURCE_FENCE = SimpleNamespace(id=SOURCE_ID, status="active", generation=1)
STAGE_KEYS = {
    "ingestion.stage.requested": "receive",
    "connector.crawl.requested": "collect_web",
    "ingestion.normalize.requested": "normalize",
    "document.file.uploaded": "parse_file",
}


class FakeSession:
    """Session whose ``get``/``scalar``/``execute`` record the entity (and lock intent) they were asked for."""

    def __init__(self, calls: list[str], rows: dict[str, Any]) -> None:
        self.calls = calls
        self.rows = rows

    async def get(self, model: Any, _ident: Any, **kwargs: Any) -> Any:
        """Lock the state row."""
        self.calls.append(
            f"get:{model.__name__}:{'lock' if kwargs.get('with_for_update') else 'read'}"
        )
        return self.rows["state"]

    async def scalar(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
        """Answer by first entity; the predecessor-stage probe selects a bare column and returns ``rows['pending']``."""
        entity = stmt.column_descriptions[0]["entity"].__name__
        name = stmt.column_descriptions[0]["name"]
        label = f"scalar:{entity}" + (
            ":predecessor"
            if name == "id" and entity == "IngestionStage" and "stage_key IN" in str(stmt)
            else ""
        )
        self.calls.append(label)
        if label.endswith("predecessor"):
            return self.rows.get("predecessor")
        return self.rows.get(entity)

    async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
        """Candidate discovery / journal locks: answer from scripted row lists."""
        entity = stmt.column_descriptions[0]["entity"].__name__
        locked = "FOR UPDATE" in str(stmt)
        self.calls.append(
            f"execute:{entity}:{'lock' if locked else 'read'}:{len(stmt.column_descriptions)}"
        )
        return SimpleNamespace(all=lambda: self.rows.get("candidate_rows", []))


class Harness:
    """Build the fakes for one event type and record the cross-module call sequence."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        provider: str = "rss",
    ) -> None:
        self.calls: list[str] = []
        self.mp = monkeypatch
        self.event_type = event_type
        self.payload = (
            payload if payload is not None else {"run_id": str(RUN_ID), "stage_id": str(STAGE_ID)}
        )
        self.delivery = SimpleNamespace(
            id=EVENT_ID,
            type=event_type,
            status="queued",
            dispatched_at=NOW,
            producer="modules.ingestion",
            version=1,
            payload=self.payload,
        )
        run = SimpleNamespace(id=RUN_ID, batch_id=BATCH_ID)
        batch = SimpleNamespace(id=BATCH_ID)
        stage = SimpleNamespace(id=STAGE_ID, status="pending", attempts=0, lease_expires_at=None)
        event = SimpleNamespace(
            type=event_type,
            version=1,
            producer="modules.ingestion",
            payload=self.payload,
            id=EVENT_ID,
            dispatched_at=NOW,
        )
        self.rows = {
            "state": SimpleNamespace(lease_run_id=None, collection_lease_token=None),
            "IngestionRun": run,
            "IngestionBatch": batch,
            "IngestionStage": stage,
            "EventOutbox": event,
            "candidate_rows": [],
        }
        self.session = FakeSession(self.calls, self.rows)
        self.projection = SimpleNamespace(status="active", generation=1, provider=provider)
        self.valid_envelope = True
        self.delivery_override: Any = self.delivery
        self.run_scope: Any = SCOPE
        self._install()

    def _install(self) -> None:
        """Replace every owner call with a recorder returning the happy-path value."""
        mp, h = self.mp, self

        async def resolve_scope(*_a, **_k):
            h.calls.append("resolve_event_scope")
            return SCOPE

        async def access(*_a, **_k):
            h.calls.append("read_access_fence")
            return ACCESS

        async def delivery(*_a, **_k):
            h.calls.append("get_event_delivery")
            return h.delivery_override

        async def run_scope(*_a, **_k):
            h.calls.append("resolve_run_scope")
            return h.run_scope

        async def lock_sources(*_a, **_k):
            h.calls.append("lock_source_set")
            return SimpleNamespace(fences=(SOURCE_FENCE,), access_fence=ACCESS)

        async def projection(*_a, **_k):
            h.calls.append("get_connector_source")
            return h.projection

        async def collection_fence(*_a, **_k):
            h.calls.append("collection_fence")
            return True

        async def capture_claim(*_a, **_k):
            h.calls.append("capture_normalization_claim")
            return (
                SimpleNamespace(
                    attempts=0,
                    batch_id=BATCH_ID,
                    tag="claim",
                )
                if h.rows.get("claim", True)
                else None
            )

        async def lock_document(*_a, **_k):
            h.calls.append("lock_document_for_extraction")
            return h.rows.get("document_ok", True)

        async def prepare_documents(*_a, **_k):
            h.calls.append("prepare_normalized_document_keys")
            return "doc-prep"

        async def prepare_observations(*_a, **_k):
            h.calls.append("prepare_ingestion_observation_keys")
            return "obs-prep"

        mp.setattr(worker.ingestion_api, "resolve_ingestion_event_scope", resolve_scope)
        mp.setattr(worker, "read_access_fence", access)
        mp.setattr(worker.ingestion_api, "get_event_delivery", delivery)
        mp.setattr(worker.ingestion_api, "resolve_ingestion_run_scope", run_scope)
        mp.setattr(worker.sources, "lock_source_set", lock_sources)
        mp.setattr(worker.sources, "get_connector_source", projection)
        mp.setattr(worker.connectors, "require_collection_fence", collection_fence)
        mp.setattr(worker, "_capture_normalization_retry_claim_in_uow", capture_claim)
        mp.setattr(worker.documents, "lock_document_for_extraction", lock_document)
        mp.setattr(worker.documents, "prepare_normalized_document_keys", prepare_documents)
        mp.setattr(worker.observations, "prepare_ingestion_observation_keys", prepare_observations)
        mp.setattr(worker, "valid_event_envelope", lambda event, scope: h.valid_envelope)
        mp.setattr(worker, "_same_worker_claim", lambda work, claim: True)

    async def call(self, **kwargs: Any) -> Any:
        """Run ``_lock_worker_event`` for this harness's event type."""
        return await worker._lock_worker_event(
            self.session,
            EVENT_ID,
            multi_workspace_enabled=False,
            event_types=(self.event_type,),
            **kwargs,
        )


HEAD = [
    "resolve_event_scope",
    "read_access_fence",
    "get_event_delivery",
    "resolve_run_scope",
    "lock_source_set",
    "get_connector_source",
]
TAIL = [
    "get:SourceIngestionState:lock",
    "scalar:IngestionRun",
    "scalar:IngestionBatch",
    "scalar:IngestionStage",
]


@pytest.mark.asyncio
async def test_receive_event_lock_order_and_state(monkeypatch):
    """A plain stage event: admission reads, Source set, projection, then state -> run -> batch -> stage -> outbox."""
    h = Harness(monkeypatch, "ingestion.stage.requested")
    work = await h.call()
    assert h.calls == [*HEAD, *TAIL, "scalar:EventOutbox"]
    assert work.run.id == RUN_ID and work.batch.id == BATCH_ID and work.normalization_rows == ()
    assert work.document_preparation is None and work.observation_preparation is None
    assert work.access_fence == ACCESS and work.source is SOURCE_FENCE


@pytest.mark.asyncio
async def test_crawl_event_takes_the_collection_fence_after_the_projection(monkeypatch):
    """Crawl events lock the Connector collection fence right after the projection (not for native providers)."""
    h = Harness(
        monkeypatch,
        "connector.crawl.requested",
        payload={"run_id": str(RUN_ID), "stage_id": str(STAGE_ID), "connector_revision": 2},
    )
    await h.call()
    assert h.calls[:7] == [*HEAD, "collection_fence"]
    for variant, expected in (
        ({"connector_revision": 0}, None),
        ({"connector_revision": "2"}, None),
    ):
        h2 = Harness(
            monkeypatch,
            "connector.crawl.requested",
            payload={"run_id": str(RUN_ID), "stage_id": str(STAGE_ID), **variant},
        )
        assert await h2.call() is expected
        assert h2.calls[-1] == "get_connector_source"
    h3 = Harness(
        monkeypatch,
        "connector.crawl.requested",
        payload={"run_id": str(RUN_ID), "stage_id": str(STAGE_ID), "connector_revision": 2},
    )
    h3.mp.setattr(worker.connectors, "is_native_provider", lambda provider: True)
    assert await h3.call() is None and "collection_fence" not in h3.calls


@pytest.mark.asyncio
async def test_upload_event_prepares_the_document_before_state(monkeypatch):
    """Upload: the Document/URI preparation precedes the state row and everything else."""
    payload = {
        "run_id": str(RUN_ID),
        "stage_id": str(STAGE_ID),
        "document_id": str(DOC_ID),
        "raw_uri": f"workspaces/{WORKSPACE}/documents/{DOC_ID}/a.pdf",
        "mime_type": "application/pdf",
    }
    h = Harness(monkeypatch, "document.file.uploaded", payload=payload)
    await h.call()
    assert h.calls == [*HEAD, "lock_document_for_extraction", *TAIL, "scalar:EventOutbox"]
    legacy = dict(payload, raw_uri=f"documents/{DOC_ID}/a.pdf")
    h2 = Harness(monkeypatch, "document.file.uploaded", payload=legacy)
    assert await h2.call() is not None
    h3 = Harness(monkeypatch, "document.file.uploaded", payload=payload)
    h3.rows["document_ok"] = False
    assert await h3.call() is None and "get:SourceIngestionState:lock" not in h3.calls
    for bad in (dict(payload, raw_uri="documents/other/a.pdf"), dict(payload, mime_type=None)):
        h4 = Harness(monkeypatch, "document.file.uploaded", payload=bad)
        with pytest.raises(ValueError):
            await h4.call()


def _candidate_row(provider_id: str = "p1") -> tuple[Any, ...]:
    """One discovery row in the exact column order of the candidate query."""
    return (
        uuid4(),
        uuid4(),
        provider_id,
        "h" * 64,
        WORKSPACE,
        SOURCE_ID,
        1,
        RUN_ID,
        BATCH_ID,
        STAGE_ID,
        1,
        SOURCE_ID,
    )


@pytest.mark.asyncio
async def test_normalize_event_discovers_prepares_then_locks_journal_rows_before_outbox(
    monkeypatch,
):
    """Normalize: claim -> discover -> prepare D -> state/run/batch/stage -> journals -> accepted -> compare -> outbox."""
    h = Harness(monkeypatch, "ingestion.normalize.requested", provider="reddit")
    row = _candidate_row()
    h.rows["candidate_rows"] = [row]
    h.rows["IngestionStage"].status = "pending"
    # Journal/observation pairs reloaded under lock must reproduce the discovered identities exactly.
    progress = SimpleNamespace(
        id=row[0],
        workspace_id=WORKSPACE,
        source_id=SOURCE_ID,
        source_generation=1,
        run_id=RUN_ID,
        stage_id=STAGE_ID,
        normalization_version=1,
    )
    observation = SimpleNamespace(
        id=row[1], provider_id="p1", record_hash="h" * 64, batch_id=BATCH_ID, source_id=SOURCE_ID
    )

    original = h.session.execute

    async def execute(stmt, *a, **k):
        result = await original(stmt, *a, **k)
        if len(stmt.column_descriptions) == 2:
            return SimpleNamespace(all=lambda: [(progress, observation)])
        return result

    h.session.execute = execute
    work = await h.call()
    assert h.calls == [
        *HEAD,
        "capture_normalization_claim",
        "execute:ObservationNormalization:read:12",
        "prepare_normalized_document_keys",
        *TAIL,
        "execute:ObservationNormalization:lock:1",
        "execute:SourceObservation:lock:1",
        "execute:ObservationNormalization:read:12",
        "execute:ObservationNormalization:read:2",
        "scalar:EventOutbox",
        "scalar:IngestionStage:predecessor",
    ]
    assert work.document_preparation == "doc-prep" and len(work.normalization_rows) == 1
    assert work.observation_preparation is None
    journal_lock = [c for c in h.calls if "lock" in c and c.startswith("execute")]
    assert len(journal_lock) == 2  # journals first, then accepted observations


@pytest.mark.asyncio
async def test_measurement_provider_prepares_observation_keys_before_the_outbox(monkeypatch):
    """Structured providers also prepare Observations keys, after the journal compare and before the outbox."""
    h = Harness(monkeypatch, "ingestion.normalize.requested", provider="coingecko")
    row = _candidate_row()
    h.rows["candidate_rows"] = [row]
    progress = SimpleNamespace(
        id=row[0],
        workspace_id=WORKSPACE,
        source_id=SOURCE_ID,
        source_generation=1,
        run_id=RUN_ID,
        stage_id=STAGE_ID,
        normalization_version=1,
    )
    observation = SimpleNamespace(
        id=row[1], provider_id="p1", record_hash="h" * 64, batch_id=BATCH_ID, source_id=SOURCE_ID
    )
    original = h.session.execute

    async def execute(stmt, *a, **k):
        result = await original(stmt, *a, **k)
        return (
            SimpleNamespace(all=lambda: [(progress, observation)])
            if len(stmt.column_descriptions) == 2
            else result
        )

    h.session.execute = execute
    work = await h.call()
    assert work.observation_preparation == "obs-prep"
    assert h.calls.index("prepare_ingestion_observation_keys") < h.calls.index("scalar:EventOutbox")
    assert h.calls.index("prepare_ingestion_observation_keys") > max(
        i for i, c in enumerate(h.calls) if c.startswith("execute")
    )


@pytest.mark.asyncio
async def test_normalize_conflicts_carry_the_original_claim(monkeypatch):
    """Candidate-set drift, a swapped batch or a spent retry budget are preparation conflicts with the claim."""
    h = Harness(monkeypatch, "ingestion.normalize.requested")
    row = _candidate_row()
    h.rows["candidate_rows"] = [row]
    calls = {"n": 0}
    original = h.session.execute

    async def drifting(stmt, *a, **k):
        result = await original(stmt, *a, **k)
        calls["n"] += 1
        if "FOR UPDATE" not in str(stmt) and calls["n"] > 1 and len(stmt.column_descriptions) != 2:
            return SimpleNamespace(all=list)
        return result

    h.session.execute = drifting
    with pytest.raises(worker.NormalizationPreparationConflict) as exc:
        await h.call()
    assert exc.value.code == "normalization_candidate_set_changed"

    h2 = Harness(monkeypatch, "ingestion.normalize.requested")
    h2.rows["IngestionRun"].batch_id = uuid4()
    with pytest.raises(worker.NormalizationPreparationConflict) as exc2:
        await h2.call()
    assert exc2.value.code == "normalization_batch_changed"

    h3 = Harness(monkeypatch, "ingestion.normalize.requested")

    async def spent(*_a, **_k):
        return SimpleNamespace(attempts=worker.MAX_STAGE_ATTEMPTS, batch_id=BATCH_ID)

    h3.mp.setattr(worker, "_capture_normalization_retry_claim_in_uow", spent)
    with pytest.raises(worker.NormalizationPreparationConflict) as exc3:
        await h3.call()
    assert exc3.value.code == "normalization_retry_exhausted"
    assert "lock_source_set" in h3.calls and "get:SourceIngestionState:lock" not in h3.calls


@pytest.mark.asyncio
async def test_normalize_without_a_claim_only_serves_terminal_stages(monkeypatch):
    """No retry claim: only an already-terminal stage may be returned (idempotent replay), nothing else."""
    h = Harness(monkeypatch, "ingestion.normalize.requested")
    h.rows["claim"] = False
    assert await h.call() is None
    h2 = Harness(monkeypatch, "ingestion.normalize.requested")
    h2.rows["claim"] = False
    h2.rows["IngestionStage"].status = "succeeded"
    work = await h2.call()
    assert work is not None and work.document_preparation is None


@pytest.mark.asyncio
async def test_unfinished_predecessor_yields_no_work(monkeypatch):
    """Normalization waits for its receive/browser predecessor stage."""
    h = Harness(monkeypatch, "ingestion.normalize.requested")
    h.rows["claim"] = False
    h.rows["IngestionStage"].status = "succeeded"
    h.rows["predecessor"] = uuid4()
    assert await h.call() is None
    assert h.calls[-1] == "scalar:IngestionStage:predecessor"


@pytest.mark.asyncio
async def test_early_exits_never_reach_domain_locks(monkeypatch):
    """Missing scope/delivery, wrong type/status, bad envelope or run lineage stop before the Source lock."""

    def configure(h: Harness, mode: str) -> None:
        if mode == "wrong-type":
            h.delivery.type = "other.event"
        elif mode == "not-queued":
            h.delivery.status = "pending"
        elif mode == "no-dispatch":
            h.delivery.dispatched_at = None
        elif mode == "bad-envelope":
            h.valid_envelope = False
        elif mode == "no-delivery":
            h.delivery_override = None
        elif mode == "bad-run-id":
            h.delivery.payload["run_id"] = "not-a-uuid"
        elif mode == "foreign-run":
            h.run_scope = SimpleNamespace(other=True)

    for mode in (
        "wrong-type",
        "not-queued",
        "no-dispatch",
        "bad-envelope",
        "no-delivery",
        "bad-run-id",
        "foreign-run",
    ):
        h = Harness(monkeypatch, "ingestion.stage.requested")
        configure(h, mode)
        assert await h.call() is None, mode
        assert "lock_source_set" not in h.calls, mode


@pytest.mark.asyncio
async def test_source_comparisons_and_expected_claim(monkeypatch):
    """Inactive/changed Source, projection drift and expected-claim mismatches yield no work or a 409."""
    h = Harness(monkeypatch, "ingestion.stage.requested")
    h.projection.generation = 2
    assert await h.call() is None and h.calls[-1] == "get_connector_source"

    h2 = Harness(monkeypatch, "ingestion.stage.requested")
    payload_hash = hashlib.sha256(
        json.dumps(
            h2.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    expected = SimpleNamespace(
        scope=SCOPE,
        access_fence=ACCESS,
        event_id=EVENT_ID,
        event_type="ingestion.stage.requested",
        producer="modules.ingestion",
        dispatched_at=NOW,
        payload_hash=payload_hash,
        source_fence=SOURCE_FENCE,
        attempts=0,
        batch_id=BATCH_ID,
    )
    assert await h2.call(expected=expected) is not None

    h3 = Harness(monkeypatch, "ingestion.stage.requested")
    changed = SimpleNamespace(
        **{**expected.__dict__, "access_fence": AccessFence(WORKSPACE, 7, 3, 99)}
    )
    with pytest.raises(ConflictError) as exc:
        await h3.call(expected=changed)
    assert (exc.value.status_code, exc.value.detail) == (409, "Original worker admission changed")
    assert "lock_source_set" not in h3.calls

    h4 = Harness(monkeypatch, "ingestion.stage.requested")
    other_hash = SimpleNamespace(**{**expected.__dict__, "payload_hash": "0" * 64})
    assert await h4.call(expected=other_hash) is None and "lock_source_set" not in h4.calls

    h5 = Harness(monkeypatch, "ingestion.stage.requested")
    other_source = SimpleNamespace(
        **{**expected.__dict__, "source_fence": SimpleNamespace(id=SOURCE_ID)}
    )
    assert await h5.call(expected=other_source) is None and "get_connector_source" not in h5.calls


@pytest.mark.asyncio
async def test_missing_lineage_rows_yield_no_work(monkeypatch):
    """A missing run, batch or stage stops before the outbox lock."""
    for entity in ("IngestionRun", "IngestionBatch", "IngestionStage"):
        h = Harness(monkeypatch, "ingestion.stage.requested")
        h.rows[entity] = None
        assert await h.call() is None
        assert "scalar:EventOutbox" not in h.calls, entity
