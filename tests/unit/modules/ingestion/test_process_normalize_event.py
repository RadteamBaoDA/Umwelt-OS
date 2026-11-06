"""Characterization of ``process_normalize_event``: provider metadata, byte budget, validation reject, settlement.

Every collaborator that touches the database is replaced by a spy so the tests pin the per-row dispositions,
the normalized input handed to the Documents owner, the slice settlement, and the failure/retry bookkeeping.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from arq import Retry
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from core.workspaces.schemas import InternalJobScope
from modules.ingestion import worker
from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.schemas import ProviderRecordMetadata

from .patching import patch_worker

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SOURCE_ID = uuid4()
RUN_ID = uuid4()
STAGE_ID = uuid4()
CLAIM = SimpleNamespace(run_id=RUN_ID, stage_id=STAGE_ID, tag="original-claim")
_OPTIONAL_MODULES = ("modules.ingestion._normalize_metadata",)


class Result:
    """Minimal ``NormalizedDocumentResult`` stand-in supporting ``model_copy``."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(
            {
                "disposition": "normalized",
                "document_id": uuid4(),
                "document_version_id": uuid4(),
                "chunk_count": 2,
                "created_version": True,
                "version_number": 1,
                "selected_current": True,
                **values,
            }
        )

    def model_copy(self, *, update: dict[str, Any]) -> Result:
        """Return a copy with ``update`` applied, like the pydantic API."""
        return Result(**{**self.__dict__, **update})


def make_row(
    provider_id: str = "item-1",
    *,
    metadata: dict[str, Any] | None = None,
    content: str = "body",
    envelope: Any = None,
    bad_hash: bool = False,
    generation: int = 1,
    pad: int = 0,
    payload_pad: int = 0,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    """Build a (progress, observation) pair whose accepted hash matches the persisted payload."""
    record = IngestionRecord(
        provider_id=provider_id,
        content="x" * pad if pad else content,
        observed_at=NOW,
        version="v1",
        metadata=metadata or {},
    )
    accepted = hashlib.sha256(
        json.dumps(
            {"version": record.version, "content": record.content, "metadata": record.metadata},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    payload = record.model_dump(mode="json", exclude={"collected_at"})
    if envelope is not None:
        payload["_native_telegram"] = envelope
    if payload_pad:
        payload["pad"] = "x" * payload_pad  # never parsed: the oversize check precedes validation
    observation = SimpleNamespace(
        id=uuid4(),
        source_id=SOURCE_ID,
        provider_id=provider_id,
        record_hash="0" * 64 if bad_hash else accepted,
        payload=payload,
        observed_at=NOW,
        received_at=NOW,
        collected_at=NOW,
    )
    progress = SimpleNamespace(
        source_generation=generation,
        disposition="pending",
        error_code=None,
        selected_current=False,
        document_id=None,
        document_version_id=None,
        chunk_count=0,
    )
    return progress, observation


def provider_record(provider: str, identity: str, **extra: Any) -> dict[str, Any]:
    """Build a valid typed ``provider_record`` block for a non-Telegram, non-measurement provider."""
    body = {
        "provider": provider,
        "identity": identity,
        "provider_version": "v1",
        "timestamp_basis": "collection",
        "coverage": "returned_snapshot",
        "content_truncated": False,
        **extra,
    }
    return ProviderRecordMetadata.model_validate(body).model_dump(mode="json")


class FakeSession:
    """Scripted session: three settlement counts in order, then the run's stages."""

    def __init__(
        self, counts: tuple[int, int, int] = (0, 0, 2), stages: list[Any] | None = None
    ) -> None:
        self.counts = list(counts)
        self.stages = stages
        self.flushes = 0
        self.rollbacks = 0

    async def scalar(self, *_a: Any, **_k: Any) -> int:
        """Return pending, failed, then chunk-total counts."""
        return self.counts.pop(0)

    async def scalars(self, *_a: Any, **_k: Any) -> Any:
        """Return the run's stage rows."""
        return SimpleNamespace(all=lambda: list(self.stages or []))

    async def flush(self) -> None:
        """Count flushes (ready-event publication proves its journal first)."""
        self.flushes += 1

    async def rollback(self) -> None:
        """Count rollbacks."""
        self.rollbacks += 1


class Env:
    """Wire the worker against fakes and expose recorded calls."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[tuple[Any, Any]],
        *,
        provider: str = "rss",
        native: bool = False,
        counts: tuple[int, int, int] = (0, 0, 2),
        stages: list[Any] | None = None,
        stage_status: str = "pending",
        preparation: Any = "prep",
    ) -> None:
        self.mp = monkeypatch
        self.events: list[Any] = []
        self.inputs: list[Any] = []
        self.counts_logged: list[tuple[str, int, str]] = []
        self.sessions: list[FakeSession] = []
        self.session_args = (counts, stages)
        self.native = native
        self.upsert_script: list[Any] = []
        self.published: list[Any] = []
        self.scope_obj = InternalJobScope(
            workspace_id=uuid4(),
            actor_user_id=7,
            membership_revision=3,
            source_id=SOURCE_ID,
            source_generation=1,
        )
        self.state = SimpleNamespace(lease_run_id=RUN_ID, lease_expires_at=NOW)
        self.run = SimpleNamespace(id=RUN_ID, status="queued", error_code=None)
        self.stage = SimpleNamespace(
            id=STAGE_ID,
            status=stage_status,
            attempts=0,
            lease_expires_at=None,
            error_code=None,
            next_attempt_at=None,
            result_count=0,
        )
        self.event = SimpleNamespace(status="pending", next_attempt_at=None)
        self.work = SimpleNamespace(
            scope=self.scope_obj,
            access_fence="access",
            source=SimpleNamespace(id=SOURCE_ID, generation=1, status="active"),
            projection=SimpleNamespace(provider=provider),
            state=self.state,
            run=self.run,
            batch=SimpleNamespace(id=uuid4()),
            stage=self.stage,
            event=self.event,
            normalization_rows=tuple(rows),
            document_preparation=preparation,
            observation_preparation="obs-prep",
        )
        self._install(native)

    def session(self) -> FakeSession:
        """Create the next scripted session."""
        session = FakeSession(*self.session_args)
        self.sessions.append(session)
        return session

    def _install(self, native: bool) -> None:
        """Replace every database-touching collaborator with a recording spy."""
        mp, env = self.mp, self

        async def lock_worker_event(session, identifier, **_k):
            env.events.append("lock_worker_event")
            return env.work

        async def enabled(*_a, **_k):
            env.events.append("ingestion_enabled")
            return True

        async def commit(session, run, stage, extras=(), **kw):
            env.events.append(("commit", run.status, stage.status, tuple(extras)))

        async def delivered(*_a, **_k):
            env.events.append("mark_delivered")

        async def upsert(session, normalized_input, **kw):
            env.inputs.append(normalized_input)
            env.events.append("upsert")
            outcome = env.upsert_script.pop(0) if env.upsert_script else Result()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome, kw["preparation"]

        async def publish(session, event, **kw):
            env.published.append(event.type)

        async def processing_result(*a, **kw):
            env.events.append(("processing_result", a[4]))

        async def provider_scope(*_a, **_k):
            return SimpleNamespace(discriminator="scope-1")

        async def retry_lock(session, identifier, **kw):
            env.events.append(("retry_lock", kw["expected"]))
            return env.retry_work

        patch_worker(mp, "_lock_worker_event", lock_worker_event)
        patch_worker(mp, "_ingestion_enabled", enabled)
        patch_worker(mp, "_capture_worker_claim", lambda work: CLAIM)
        patch_worker(mp, "_commit_ingestion_change", commit)
        patch_worker(mp, "_lock_normalization_retry_event", retry_lock)
        mp.setattr(worker.ingestion_api, "mark_event_delivered", delivered)
        mp.setattr(worker.ingestion_api, "publish_event", publish)
        mp.setattr(worker.documents, "upsert_normalized_document_in_uow", upsert)
        mp.setattr(worker.sources, "record_processing_result_in_uow", processing_result)
        mp.setattr(worker.connectors, "is_native_provider", lambda provider: native)
        mp.setattr(worker.connectors, "get_current_provider_scope", provider_scope)
        patch_worker(
            mp,
            "count",
            lambda name, value, **labels: env.counts_logged.append(
                (name, value, labels["outcome"])
            ),
        )
        self.retry_work = None
        self.module_enabled = True

        async def module_enabled(*_a, **_k):
            return env.module_enabled

        mp.setattr("modules.settings.public.module_is_enabled", module_enabled)

    def patch_telegram_validator(self, fn: Any) -> None:
        """Replace the Telegram delivery validator in every module that binds it."""
        for name in (
            "modules.ingestion.public",
            "modules.ingestion._native.records",
            "modules.ingestion._normalize_metadata",
        ):
            try:
                module = importlib.import_module(name)
            except ImportError:
                continue
            if hasattr(module, "validate_telegram_record_delivery"):
                self.mp.setattr(module, "validate_telegram_record_delivery", fn)

    async def run_event(self) -> None:
        """Run the worker entry point with this environment's session factory."""
        env = self

        @asynccontextmanager
        async def factory():
            yield env.session()

        ctx = {
            "session_factory": factory,
            "settings": SimpleNamespace(multi_workspace_enabled=False),
        }
        await worker.process_normalize_event(ctx, str(uuid4()))


@pytest.mark.asyncio
async def test_missing_work_or_disabled_module_returns_without_effects(monkeypatch):
    """No locked work (or a disabled module) is a silent return: nothing is claimed, committed or counted."""
    env = Env(monkeypatch, [])
    env.work = None

    async def none(*_a, **_k):
        return None

    patch_worker(monkeypatch, "_lock_worker_event", none)
    await env.run_event()
    assert env.events == [] and env.counts_logged == []


@pytest.mark.asyncio
async def test_already_succeeded_stage_marks_delivered_releases_native_lease_and_commits(
    monkeypatch,
):
    """A replayed event for a succeeded stage only delivers the event and releases the native run lease."""
    env = Env(monkeypatch, [], native=True, stage_status="succeeded")
    commits: list[bool] = []

    async def session_commit():
        commits.append(True)

    original = env.session

    def session_with_commit():
        session = original()
        session.commit = session_commit
        return session

    env.session = session_with_commit
    await env.run_event()
    assert env.events == [
        "lock_worker_event",
        "ingestion_enabled",
        "mark_delivered",
    ] and commits == [True]
    assert env.state.lease_run_id is None and env.state.lease_expires_at is None


@pytest.mark.asyncio
async def test_failed_stage_returns_untouched(monkeypatch):
    """A terminally failed stage is left alone."""
    env = Env(monkeypatch, [], stage_status="failed")
    await env.run_event()
    assert env.events == ["lock_worker_event", "ingestion_enabled"]
    assert env.stage.status == "failed"


@pytest.mark.asyncio
async def test_missing_document_preparation_goes_through_the_retry_settlement(monkeypatch):
    """No document preparation is a preparation conflict: attempts are spent under the original claim."""
    env = Env(monkeypatch, [], preparation=None)
    retry_stage = SimpleNamespace(
        attempts=0, status="running", error_code=None, next_attempt_at=None, lease_expires_at=NOW
    )
    retry_run = SimpleNamespace(id=RUN_ID, status="running", error_code=None)
    retry_event = SimpleNamespace(status="pending", next_attempt_at=None)
    env.retry_work = SimpleNamespace(
        run=retry_run,
        stage=retry_stage,
        event=retry_event,
        projection=SimpleNamespace(provider="rss"),
        state=env.state,
        scope=env.scope_obj,
        access_fence="access",
    )
    await env.run_event()
    assert env.events[2] == ("retry_lock", CLAIM)
    assert (
        retry_stage.attempts == 1
        and retry_stage.status == "retrying"
        and retry_stage.error_code == "transient_failure"
    )
    assert retry_run.status == "queued" and retry_event.status == "pending"
    assert retry_stage.lease_expires_at is None and retry_stage.next_attempt_at is not None
    assert env.events[-1] == ("commit", "queued", "retrying", ())


@pytest.mark.asyncio
async def test_retry_exhaustion_fails_stage_run_and_event(monkeypatch):
    """The fifth failed attempt marks stage, run and event failed with ``retry_exhausted``."""
    env = Env(monkeypatch, [], preparation=None, native=True)
    stage = SimpleNamespace(
        attempts=4, status="running", error_code=None, next_attempt_at=None, lease_expires_at=NOW
    )
    run = SimpleNamespace(id=RUN_ID, status="running", error_code=None)
    event = SimpleNamespace(status="pending", next_attempt_at=None)
    env.retry_work = SimpleNamespace(
        run=run,
        stage=stage,
        event=event,
        projection=SimpleNamespace(provider="x"),
        state=env.state,
        scope=env.scope_obj,
        access_fence="access",
    )
    await env.run_event()
    assert (stage.status, stage.error_code, run.status, run.error_code, event.status) == (
        "failed",
        "retry_exhausted",
        "failed",
        "retry_exhausted",
        "failed",
    )
    assert env.state.lease_run_id is None  # terminal native retry releases the run lease


@pytest.mark.asyncio
async def test_lost_retry_admission_and_database_errors_are_swallowed(monkeypatch):
    """No retry work, an HTTP admission loss, or a database error during settlement grants no new authority."""
    env = Env(monkeypatch, [], preparation=None)
    await env.run_event()  # retry_work None -> returns
    assert env.events[-1][0] == "retry_lock"

    async def lost(*_a, **_k):
        raise HTTPException(status_code=409, detail="x")

    patch_worker(monkeypatch, "_lock_normalization_retry_event", lost)
    await env.run_event()

    async def broken(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("db"))

    patch_worker(monkeypatch, "_lock_normalization_retry_event", broken)
    await env.run_event()


@pytest.mark.asyncio
async def test_error_before_any_claim_is_retried_by_the_queue(monkeypatch):
    """A transient failure before the claim was captured retries the whole job with jittered backoff (try 1: 7.5-15 s)."""
    env = Env(monkeypatch, [])

    async def boom(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("db"))

    patch_worker(monkeypatch, "_lock_worker_event", boom)
    with pytest.raises(Retry) as exc:
        await env.run_event()
    assert 7500 <= exc.value.defer_score <= 15000


@pytest.mark.asyncio
async def test_measurement_provider_waits_while_observations_are_disabled(monkeypatch):
    """The slice is deferred (not consumed) when the observation projection is lifecycle-disabled."""
    progress, observation = make_row()
    env = Env(monkeypatch, [(progress, observation)], provider="coingecko", native=True)
    env.module_enabled = False
    await env.run_event()
    assert (env.stage.status, env.run.status, env.event.status) == ("pending", "queued", "pending")
    assert env.stage.lease_expires_at is None and env.event.next_attempt_at is not None
    assert env.events[-1] == ("commit", "queued", "pending", ())
    assert "upsert" not in env.events and progress.disposition == "pending"
    assert (
        env.state.lease_run_id == RUN_ID and env.state.lease_expires_at != NOW
    )  # renewed, not released


COMMON_CASES = [
    # (metadata, expected title, expected canonical_url, expected published_at, content_type)
    ({}, "item-1", None, None, None),
    ({"title": "  Hello  "}, "Hello", None, None, None),
    ({"title": "   "}, "item-1", None, None, None),
    ({"canonical_url": "https://ex.org/a"}, "item-1", "https://ex.org/a", None, None),
    ({"url": "http://ex.org/b"}, "item-1", "http://ex.org/b", None, None),
    ({"canonical_url": "ftp://ex.org/a"}, "item-1", None, None, None),
    ({"canonical_url": "https://user:pw@ex.org/a"}, "item-1", None, None, None),
    (
        {"published_at": "2026-01-01T00:00:00Z"},
        "item-1",
        None,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
    ),
    (
        {"published_at": "2026-01-01T00:00:00"},
        "item-1",
        None,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
    ),
    ({"published_at": "not a date"}, "item-1", None, None, None),
    ({"content_type": "t" * 100}, "item-1", None, None, "t" * 64),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata,title,url,published,content_type", COMMON_CASES)
async def test_common_metadata_normalization(
    monkeypatch, metadata, title, url, published, content_type
):
    """Generic RSS-style metadata: title/url/date/content-type are normalized and bounded."""
    progress, observation = make_row(metadata=metadata)
    env = Env(monkeypatch, [(progress, observation)])
    await env.run_event()
    normalized = env.inputs[0]
    assert (
        normalized.title,
        normalized.canonical_url,
        normalized.published_at,
        normalized.content_type,
    ) == (title, url, published, content_type)
    assert normalized.provenance["provider_scope_discriminator"] == "scope-1"
    assert "provider_record" not in normalized.provenance and normalized.telegram_order is None
    assert progress.disposition == "normalized" and progress.chunk_count == 2


@pytest.mark.asyncio
async def test_safe_metadata_is_filtered_and_bounded(monkeypatch):
    """Only allowlisted string/list fields survive, truncated to their limits."""
    metadata = {
        "author": "a" * 600,
        "language": "en",
        "summary": "s" * 20_000,
        "description": 5,
        "tags": ["x" * 600, 7, "y"],
        "categories": "c" * 3000,
        "unlisted": "dropped",
    }
    progress, observation = make_row(metadata=metadata)
    env = Env(monkeypatch, [(progress, observation)])
    await env.run_event()
    safe = env.inputs[0].provenance["metadata"]
    assert (
        safe["author"] == "a" * 500 and safe["language"] == "en" and len(safe["summary"]) == 10_000
    )
    assert "description" not in safe and "unlisted" not in safe
    assert safe["tags"] == ["x" * 500, "y"] and safe["categories"] == "c" * 2000


@pytest.mark.asyncio
async def test_typed_provider_record_merges_only_allowlisted_raw_fields(monkeypatch):
    """A typed provider envelope absorbs its provider's allowlisted raw metadata keys into source_fields."""
    record_block = provider_record("reddit", "item-1", source_fields={"title": "typed"})
    metadata = {
        "provider_record": record_block,
        "subreddit": "r/x",
        "author": "u",
        "unrelated": "no",
    }
    progress, observation = make_row(metadata=metadata)
    env = Env(monkeypatch, [(progress, observation)], provider="reddit")
    await env.run_event()
    stored = env.inputs[0].provenance["provider_record"]
    assert stored["source_fields"] == {"title": "typed", "subreddit": "r/x", "author": "u"}
    assert progress.disposition == "normalized"


INVALID_ROWS = [
    ("hash", {"bad_hash": True}),
    ("identity", {"metadata": {"provider_record": provider_record("reddit", "other")}}),
    ("provider", {"metadata": {"provider_record": provider_record("mastodon", "item-1")}}),
    ("missing-envelope", {}),
    (
        "foreign-telegram-envelope",
        {
            "metadata": {"provider_record": provider_record("reddit", "item-1")},
            "envelope": {"proof": {}, "raw_update": {}},
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kwargs", INVALID_ROWS)
async def test_invalid_rows_are_failed_without_calling_the_owner(monkeypatch, name, kwargs):
    """Hash/identity/provider/missing-envelope violations fail only that row before owner entry."""
    progress, observation = make_row(**kwargs)
    provider = "reddit"
    env = Env(monkeypatch, [(progress, observation)], provider=provider, counts=(0, 1, 0))
    await env.run_event()
    assert (
        progress.disposition == "failed" and progress.error_code == "invalid_normalization_record"
    )
    assert "upsert" not in env.events
    assert env.counts_logged == [("ingestion_documents_total", 1, "failed")]


@pytest.mark.asyncio
async def test_generation_change_and_oversized_record_are_skipped_or_failed(monkeypatch):
    """A row from another generation is skipped; a single record above the slice budget fails by itself."""
    big = make_row("big", payload_pad=worker.NORMALIZATION_BATCH_BYTES + 1)
    old = make_row(generation=9)
    env = Env(monkeypatch, [big, old])
    await env.run_event()
    assert (old[0].disposition, old[0].error_code) == ("skipped", "source_generation_changed")
    assert (big[0].disposition, big[0].error_code) == (
        "failed",
        "record_exceeds_normalization_limit",
    )
    assert "upsert" not in env.events
    assert env.counts_logged == []  # neither counted as a processed or failed document


@pytest.mark.asyncio
async def test_byte_budget_stops_the_slice_and_keeps_the_rest_pending(monkeypatch):
    """The slice stops before the row that would exceed the byte budget (after at least one row)."""
    rows = [
        make_row(f"r{i}", pad=900_000) for i in range(5)
    ]  # ~0.9 MB each against a 4 MiB slice budget
    env = Env(monkeypatch, rows, counts=(1, 0, 8))
    await env.run_event()
    assert [r[0].disposition for r in rows] == ["normalized"] * 4 + ["pending"]
    assert (env.stage.status, env.run.status, env.event.status) == ("pending", "queued", "pending")
    assert env.event.next_attempt_at is not None
    assert (
        env.events[-1][0] == "commit" and env.events[-1][3] != ()
    )  # processed work publishes its changes


@pytest.mark.asyncio
async def test_validation_rejection_fails_that_row_and_keeps_earlier_rows(monkeypatch):
    """NormalizedDocumentValidationRejected marks the row failed and the slice continues with later rows."""
    rows = [make_row(f"r{i}") for i in range(3)]
    env = Env(monkeypatch, rows, counts=(0, 1, 4))
    env.upsert_script = [Result(), documents.NormalizedDocumentValidationRejected("bad"), Result()]
    await env.run_event()
    assert [r[0].disposition for r in rows] == ["normalized", "failed", "normalized"]
    assert rows[1][0].error_code == "invalid_normalization_record"
    assert env.counts_logged == [
        ("ingestion_documents_total", 2, "processed"),
        ("ingestion_documents_total", 1, "failed"),
    ]
    assert env.stage.status == "failed" and env.stage.error_code == "normalization_failed"
    assert env.event.status == "failed"
    assert ("processing_result", "normalization_failed") in env.events
    assert env.stage.result_count == 4


@pytest.mark.asyncio
async def test_value_error_after_owner_entry_is_a_preparation_conflict(monkeypatch):
    """An unexpected ValueError after D was entered rolls back the attempt and goes to retry settlement."""
    rows = [make_row("r0")]
    env = Env(monkeypatch, rows)
    env.upsert_script = [ValueError("owner blew up")]
    retry_stage = SimpleNamespace(
        attempts=0, status="running", error_code=None, next_attempt_at=None, lease_expires_at=NOW
    )
    env.retry_work = SimpleNamespace(
        run=SimpleNamespace(id=RUN_ID, status="running", error_code=None),
        stage=retry_stage,
        event=SimpleNamespace(status="pending", next_attempt_at=None),
        projection=SimpleNamespace(provider="rss"),
        state=env.state,
        scope=env.scope_obj,
        access_fence="access",
    )
    await env.run_event()
    assert rows[0][0].disposition == "pending"  # owner entry made the failure non-recordable
    assert retry_stage.attempts == 1 and retry_stage.status == "retrying"
    assert not any(
        isinstance(e, tuple) and e[0] == "commit" and e[2] == "normalized" for e in env.events
    )


@pytest.mark.asyncio
async def test_dispositions_ready_events_and_knowledge_changes(monkeypatch):
    """Result dispositions map to progress; created versions with chunks publish both ready events."""
    rows = [make_row(f"r{i}") for i in range(3)]
    env = Env(monkeypatch, rows, native=True, counts=(0, 0, 6))
    env.upsert_script = [
        Result(disposition="normalized", created_version=True, chunk_count=3),
        Result(
            disposition="duplicate", created_version=False, chunk_count=3, selected_current=False
        ),
        Result(
            disposition="tombstoned", created_version=False, chunk_count=0, selected_current=False
        ),
    ]
    await env.run_event()
    assert [r[0].disposition for r in rows] == ["normalized", "duplicate", "skipped"]
    assert rows[2][0].error_code == "document_deleted" and rows[1][0].chunk_count == 0
    assert env.published == ["document.version.ready", "news.document.ready"]
    kind, run_status, stage_status, extras = env.events[-1]
    assert (kind, run_status, stage_status, len(extras)) == ("commit", "succeeded", "succeeded", 1)
    assert env.events.count("upsert") == 3
    assert env.counts_logged == [("ingestion_documents_total", 3, "processed")]
    assert env.event.status == "delivered" and ("processing_result", None) in env.events
    assert env.state.lease_run_id is None  # native run lease released on terminal normalization


@pytest.mark.asyncio
async def test_failed_sibling_stage_fails_the_run_and_other_stage_keeps_run_queued(monkeypatch):
    """Run status follows its stages: any failed stage fails the run; an unfinished sibling keeps it queued."""
    rows = [make_row("r0")]
    other_failed = SimpleNamespace(status="failed", error_code="boom")
    env = Env(monkeypatch, rows, stages=[other_failed])
    await env.run_event()
    assert env.run.status == "failed" and env.run.error_code == "boom"

    env2 = Env(
        monkeypatch, [make_row("r0")], stages=[SimpleNamespace(status="running", error_code=None)]
    )
    await env2.run_event()
    assert env2.run.status == "queued" and env2.run.error_code is None


def telegram_envelope(update_id: int = 11) -> dict[str, Any]:
    """Build a persisted Telegram owner-proof envelope (proof + raw update)."""
    update = {"update_id": update_id, "message": {"text": "hi"}}
    encoded = json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return {
        "proof": {"bot_id": "123", "epoch": 1, "update_id": update_id, "raw_update_sha256": digest},
        "raw_update": {"update_id": update_id, "raw_update_sha256": digest, "update": update},
    }


def telegram_record(identity: str = "telegram:-1001:9") -> dict[str, Any]:
    """Build the typed Telegram ``provider_record`` block."""
    body = {
        "provider": "telegram",
        "identity": identity,
        "provider_version": "v1",
        "timestamp_basis": "collection",
        "coverage": "pending_updates_only",
        "content_truncated": False,
        "telegram": {
            "bot_id": "123",
            "channel_id": "-1001",
            "message_id": "9",
            "epoch": 1,
            "update_id": 11,
            "edited_received": False,
            "published_at": NOW.isoformat(),
        },
    }
    return ProviderRecordMetadata.model_validate(body).model_dump(mode="json")


@pytest.mark.asyncio
async def test_telegram_row_binds_proof_order_and_digest(monkeypatch):
    """Telegram rows are re-verified against their persisted proof; order and raw digest reach the owner."""
    seen: list[Any] = []
    progress, observation = make_row(
        "telegram:-1001:9",
        metadata={"provider_record": telegram_record()},
        envelope=telegram_envelope(),
    )
    env = Env(monkeypatch, [(progress, observation)], provider="telegram", native=True)
    env.patch_telegram_validator(
        lambda record, meta, proof, raw: seen.append((proof.update_id, raw.update_id))
    )
    await env.run_event()
    normalized = env.inputs[0]
    assert seen == [(11, 11)]
    assert (
        normalized.telegram_order.bot_id,
        normalized.telegram_order.epoch,
        normalized.telegram_order.update_id,
    ) == ("123", 1, 11)
    assert (
        normalized.provenance["provider_record"]["telegram"]["raw_update_sha256"]
        == telegram_envelope()["proof"]["raw_update_sha256"]
    )
    assert progress.disposition == "normalized"


TELEGRAM_FAILURES = [
    ("no-envelope", {}, None),
    ("validator-rejects", {}, "reject"),
    ("clock-mismatch", {}, "clock"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kwargs,mode", TELEGRAM_FAILURES)
async def test_telegram_row_failures(monkeypatch, name, kwargs, mode):
    """Missing proof, a rejecting validator or a skewed selection clock fail the row before owner entry."""
    envelope = None if name == "no-envelope" else telegram_envelope()
    progress, observation = make_row(
        "telegram:-1001:9", metadata={"provider_record": telegram_record()}, envelope=envelope
    )
    if mode == "clock":
        observation.observed_at = datetime(2030, 1, 1, tzinfo=UTC)
    env = Env(
        monkeypatch, [(progress, observation)], provider="telegram", native=True, counts=(0, 1, 0)
    )

    def validator(*_a):
        if mode == "reject":
            raise ValueError("mismatch")

    env.patch_telegram_validator(validator)
    await env.run_event()
    assert (
        progress.disposition == "failed" and progress.error_code == "invalid_normalization_record"
    )
    assert "upsert" not in env.events


@pytest.mark.asyncio
async def test_telegram_record_without_typed_envelope_is_rejected(monkeypatch):
    """A native-provider row with no ``provider_record`` fails: compatibility only covers measurements."""
    for provider in ("telegram", "youtube", "github", "github_releases"):
        progress, observation = make_row("x")
        env = Env(
            monkeypatch, [(progress, observation)], provider=provider, native=True, counts=(0, 1, 0)
        )
        await env.run_event()
        assert progress.error_code == "invalid_normalization_record", provider


@pytest.mark.asyncio
async def test_measurement_compatibility_row_and_observation_projection(monkeypatch):
    """A legacy structured row (no envelope) is accepted from ``world_data`` and projected into Observations."""
    provider = "coingecko"
    world = {
        "provider": provider,
        "metric": "price",
        "value": 1.5,
        "unit": "usd",
        "quality": "provider_reported",
    }
    progress, observation = make_row("m-1", metadata={"world_data": world})
    env = Env(monkeypatch, [(progress, observation)], provider=provider, native=True)
    writes: list[Any] = []

    async def write(session, **kw):
        writes.append(kw)
        env.events.append("observation_write")
        return SimpleNamespace(selected_current=True), "next-obs-prep"

    async def select_current(session, **kw):
        env.events.append("select_current")
        return True

    monkeypatch.setattr(worker.observations, "upsert_from_ingestion_in_uow", write)
    monkeypatch.setattr(
        worker.documents, "select_current_world_document_version_in_uow", select_current
    )
    await env.run_event()
    assert (
        env.events.index("upsert")
        < env.events.index("observation_write")
        < env.events.index("select_current")
    )
    assert progress.selected_current is True and progress.disposition == "normalized"
    assert (
        writes[0]["provider_scope_discriminator"] == "scope-1" and writes[0]["external_id"] == "m-1"
    )
    assert env.events[-1][3][0] is not None  # structured series change is published with the commit


def test_common_metadata_function_is_pure_and_total():
    """``_normalize_common_metadata`` never raises on junk and bounds every field."""
    from modules.ingestion._normalize_metadata import _normalize_common_metadata

    junk = {
        "title": 5,
        "canonical_url": 7,
        "published_at": [],
        "content_type": {},
        "tags": 3,
        "author": None,
    }
    common = _normalize_common_metadata(junk, "p" * 600)
    assert (
        common.title == "p" * 500 and common.canonical_url is None and common.published_at is None
    )
    assert common.content_type is None and common.safe_metadata == {}
    again = _normalize_common_metadata(
        {"url": "https://ex.org/x", "published_at": "2026-01-01T05:00:00+05:00"}, "id"
    )
    assert again.canonical_url == "https://ex.org/x" and again.published_at == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_extractor_registry_resolves_by_provider_and_accepts_registrations(monkeypatch):
    """Telegram, measurement and envelope-required providers resolve to their extractors; others pass through."""
    from modules.ingestion import _normalize_metadata as nm

    assert isinstance(nm.metadata_extractor_for("telegram"), nm.TelegramExtractor)
    assert isinstance(nm.metadata_extractor_for("coingecko"), nm.MeasurementExtractor)
    assert isinstance(nm.metadata_extractor_for("youtube"), nm.EnvelopeRequiredExtractor)
    assert type(nm.metadata_extractor_for("rss")) is nm.PassthroughExtractor
    monkeypatch.setattr(nm, "_REGISTRY", dict(nm._REGISTRY))

    class Acme(nm.TypedEnvelopeExtractor):
        """Mandatory-envelope provider registered without editing the worker."""

        providers = ("acme",)
        requires_envelope = True

    nm.register_metadata_extractor(Acme())
    record = IngestionRecord(provider_id="a", content="c", observed_at=NOW)
    with pytest.raises(ValueError, match="Native provider record metadata is missing"):
        nm.metadata_extractor_for("acme").extract(nm.ExtractionInput(record, NOW, "acme", None))
