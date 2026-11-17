"""Characterization of ``process_uploaded_file``: claim -> parse outside SQL -> publish, and failure settlement."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.workspaces.schemas import InternalJobScope
from modules.ingestion import worker
from modules.ingestion.parsers import ParsedDocument

from .patching import patch_worker

RUN_ID, STAGE_ID, DOC_ID, SOURCE_ID = uuid4(), uuid4(), uuid4(), uuid4()
# Leases are computed from the real clock at use time: the worker compares them against datetime.now(UTC).
RAW = f"workspaces/{uuid4()}/documents/{DOC_ID}/a.pdf"


class Env:
    """Fakes for the loader, Documents owner, storage and parser, with an ordered call log."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        stage_status: str = "pending",
        extraction: bool = True,
        saved: Any = DOC_ID,
    ) -> None:
        self.log: list[Any] = []
        self.stage = SimpleNamespace(
            id=STAGE_ID,
            status=stage_status,
            attempts=0,
            lease_expires_at=None,
            error_code=None,
            result_count=0,
        )
        self.run = SimpleNamespace(id=RUN_ID, status="queued", error_code=None)
        self.event = SimpleNamespace(
            status="pending",
            payload={"document_id": str(DOC_ID), "raw_uri": RAW, "mime_type": "application/pdf"},
        )
        scope = InternalJobScope(
            workspace_id=uuid4(),
            actor_user_id=7,
            membership_revision=3,
            source_id=SOURCE_ID,
            source_generation=1,
        )
        self.work = SimpleNamespace(
            scope=scope,
            access_fence="fence",
            source=SimpleNamespace(id=SOURCE_ID, generation=1, status="active"),
            run=self.run,
            stage=self.stage,
            event=self.event,
            batch=SimpleNamespace(id=uuid4()),
        )
        self.parse: Any = lambda: ParsedDocument(
            text="hello world", metadata={"pages": 1}, warnings=[]
        )
        env = self

        async def lock(session, identifier, **kw):
            env.log.append(("lock", kw.get("expected") is not None))
            if kw.get("expected") is not None:
                env.stage.status = "running"
                env.stage.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
            return env.work

        async def enabled(*_a, **_k):
            return True

        async def set_status(session, document_id, source_id, status, **kw):
            env.log.append(("extraction_status", status))
            return extraction

        async def save(
            session, document_id, source_id, text, chunks, status, metadata, warnings, version, **kw
        ):
            env.log.append(("save_extraction", status, len(chunks)))
            return saved

        async def commit(session, run, stage, extras=(), **kw):
            env.log.append(("commit", run.status, stage.status, len(extras)))

        async def delivered(*_a, **_k):
            env.log.append("delivered")

        async def processing(*a, **k):
            env.log.append(("processing_result", a[4]))
            return True

        async def parse(path, mime, *a):
            env.log.append(("parse", mime))
            return env.parse()

        async def joined(fn, *a):
            return fn(*a)

        class Storage:
            @asynccontextmanager
            async def download_to_temp(self, uri):
                env.log.append(("download", uri))
                yield "/tmp/x"  # noqa: S108  # fake path string used as fixture data; nothing is written

        patch_worker(monkeypatch, "_lock_worker_event", lock)
        patch_worker(monkeypatch, "_ingestion_enabled", enabled)
        patch_worker(monkeypatch, "_capture_worker_claim", lambda work: "claim")
        patch_worker(monkeypatch, "_commit_ingestion_change", commit)
        patch_worker(monkeypatch, "parse_file_bounded", parse)
        patch_worker(monkeypatch, "to_thread_joined", joined)
        patch_worker(monkeypatch, "get_storage_backend", lambda settings: Storage())
        patch_worker(
            monkeypatch,
            "count",
            lambda name, **labels: env.log.append(("count", labels["outcome"])),
        )
        monkeypatch.setattr(worker.documents, "set_extraction_status", set_status)
        monkeypatch.setattr(worker.documents, "save_extraction", save)
        monkeypatch.setattr(worker.ingestion_api, "mark_event_delivered", delivered)
        monkeypatch.setattr(worker.sources, "record_processing_result_in_uow", processing)

    async def run_event(self) -> None:
        """Run the entry point with test settings."""
        env = self

        class Session:
            async def commit(self) -> None:
                env.log.append("session-commit")

        @asynccontextmanager
        async def factory():
            yield Session()

        settings = SimpleNamespace(
            multi_workspace_enabled=False,
            parser_timeout_seconds=30,
            docx_expanded_max_bytes=1,
            pdf_page_max=1,
            parsed_text_max_chars=100,
        )
        # Bypass the heavy-work slot decorator (it needs a real database); the body is what is characterized.
        await inspect.unwrap(worker.process_uploaded_file)(
            {"session_factory": factory, "settings": settings}, str(uuid4())
        )


@pytest.mark.asyncio
async def test_success_claims_parses_outside_sql_then_publishes(monkeypatch):
    """Processing is committed before the download; publication succeeds the stage with the chunk count."""
    env = Env(monkeypatch)
    await env.run_event()
    assert env.log == [
        ("lock", False),
        ("extraction_status", "processing"),
        ("commit", "running", "running", 1),
        ("download", RAW),
        ("parse", "application/pdf"),
        ("lock", True),
        ("save_extraction", "succeeded", 1),
        "delivered",
        ("processing_result", None),
        ("commit", "succeeded", "succeeded", 2),
        ("count", "processed"),
    ]
    assert (
        env.stage.attempts == 1
        and env.stage.result_count == 1
        and env.stage.lease_expires_at is None
    )


@pytest.mark.asyncio
async def test_claim_phase_early_exits(monkeypatch):
    """A succeeded stage delivers its event, failed/leased stages and a refused ``processing`` change stop."""
    env = Env(monkeypatch, stage_status="succeeded")
    await env.run_event()
    assert env.log == [("lock", False), "delivered", "session-commit"]
    env = Env(monkeypatch, stage_status="failed")
    await env.run_event()
    assert env.log == [("lock", False)]
    env = Env(monkeypatch, stage_status="running")
    env.stage.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    await env.run_event()
    assert env.log == [("lock", False)]
    env = Env(monkeypatch, extraction=False)
    await env.run_event()
    assert env.log == [("lock", False), ("extraction_status", "processing")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code", [(TimeoutError(), "parser_timeout"), (ValueError("bad pdf"), "parse_failed")]
)
async def test_parse_failure_is_recorded_as_a_failed_stage(monkeypatch, error, code):
    """Any parse error fails stage, run and event, marks the document failed and counts a failed document."""
    env = Env(monkeypatch)

    def boom():
        raise error

    env.parse = boom
    await env.run_event()
    assert env.log[-5:] == [
        ("lock", True),
        ("extraction_status", "failed"),
        ("processing_result", code),
        ("commit", "failed", "failed", 2),
        ("count", "failed"),
    ]
    assert (env.stage.error_code, env.run.error_code, env.event.status) == (code, code, "failed")


@pytest.mark.asyncio
async def test_publication_for_another_document_is_dropped_and_extraction_text_is_bounded(
    monkeypatch,
):
    """If Documents saved a different document nothing is settled; oversized text is capped with a warning."""
    env = Env(monkeypatch, saved=uuid4())
    await env.run_event()
    assert "delivered" not in env.log and env.log[-1] == ("save_extraction", "succeeded", 1)

    env = Env(monkeypatch)
    env.parse = lambda: ParsedDocument(text="x" * 500, metadata={}, warnings=[])
    await env.run_event()
    assert ("save_extraction", "succeeded", 1) in env.log

    env = Env(monkeypatch)
    env.parse = lambda: ParsedDocument(text="", metadata={}, warnings=["no text layer"])
    await env.run_event()
    assert ("save_extraction", "needs_ocr", 0) in env.log
