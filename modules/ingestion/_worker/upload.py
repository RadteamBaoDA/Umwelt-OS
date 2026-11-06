"""Uploaded-file extraction, storage orphan cleanup and collection receipt purge jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.chunking import chunk_text
from core.config import Settings
from core.heavy_work import bounded_heavy_work, to_thread_joined
from core.realtime import (
    ReplayDraft,
    make_knowledge_change,
    make_source_change,
)
from core.storage import get_storage_backend
from core.telemetry import count, timed
from core.worker_context import worker_multi_workspace_flag as _worker_flag
from core.worker_context import worker_session_factory
from modules.connectors import public as connectors
from modules.ingestion import public as ingestion_api
from modules.ingestion._worker.collect import _commit_ingestion_change
from modules.ingestion._worker.lock import _lock_worker_event
from modules.ingestion._worker.stage import _ingestion_enabled
from modules.ingestion._worker.state import WorkerClaim, _capture_worker_claim
from modules.ingestion.parsers import ParsedDocument, parse_file_bounded
from modules.knowledge.documents import public as documents
from modules.sources import public as sources

logger = logging.getLogger("bbd.worker")


@dataclass(frozen=True)
class _UploadClaim:
    """Detached identity of one claimed upload extraction (valid after the claiming session closes)."""

    claim: WorkerClaim
    document_id: UUID
    source_id: UUID
    raw_uri: str
    mime_type: str


async def _claim_upload_extraction(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    identifier: UUID,
    enabled: bool,
) -> _UploadClaim | None:
    """Mark the upload's document ``processing`` and commit the running stage lease; ``None`` if nothing to run.

    One transaction: gate, short-circuit succeeded (deliver the event) / failed / still-leased stages, move the
    stage to ``running`` with a parser-timeout lease, ask Documents to set ``processing`` under the held
    admission (aborting if it refuses) and commit with replay. The stored URI and MIME were proven by the
    loader under the early Document/URI locks.
    """
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            identifier,
            multi_workspace_enabled=enabled,
            event_types=("document.file.uploaded",),
        )
        if work is None or not await _ingestion_enabled(session, work, enabled):
            return None
        run, stage, event = work.run, work.stage, work.event
        source_id = work.source.id
        document_id = UUID(event.payload["document_id"])
        raw_uri, mime_type = event.payload.get("raw_uri"), event.payload.get("mime_type")
        # The loader proved these detached values under the early Document/URI locks.
        raw_uri, mime_type = cast(str, raw_uri), cast(str, mime_type)
        now = datetime.now(UTC)
        if stage.status == "succeeded":
            await ingestion_api.mark_event_delivered(
                session, identifier, scope=work.scope, multi_workspace_enabled=enabled
            )
            await session.commit()
            return None
        if stage.status == "failed" or (
            stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now
        ):
            return None
        stage.status = "running"
        stage.attempts += 1
        stage.lease_expires_at = now + timedelta(seconds=settings.parser_timeout_seconds + 30)
        stage.error_code = None
        run.status = "running"
        if not await documents.set_extraction_status(
            session,
            document_id,
            source_id,
            "processing",
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
            expected_raw_uri=raw_uri,
            expected_mime_type=mime_type,
        ):
            return None
        claim = _capture_worker_claim(work)
        await _commit_ingestion_change(
            session,
            run,
            stage,
            (make_knowledge_change(source_id, document_id, scope=work.scope),),
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
        )
    return _UploadClaim(claim, document_id, source_id, raw_uri, mime_type)


async def _publish_extraction(
    factory: async_sessionmaker[AsyncSession],
    identifier: UUID,
    enabled: bool,
    upload: _UploadClaim,
    *,
    parsed: ParsedDocument,
    drafts: list[Any],
    extraction_status: str,
) -> None:
    """Persist the parsed text and chunks and settle the stage under the original claim.

    Reacquires the original fences, compares the claim and the running unexpired lease, lets Documents save the
    extraction (the returned document must be the claimed one) and then succeeds the stage, delivers the event,
    records the processing result and commits with replay. Silent no-op if the claim or lease is gone.
    """
    document_id, source_id = upload.document_id, upload.source_id
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            identifier,
            multi_workspace_enabled=enabled,
            event_types=("document.file.uploaded",),
            expected=upload.claim,
        )
        if (
            work is None
            or work.stage.status != "running"
            or work.stage.lease_expires_at is None
            or work.stage.lease_expires_at <= datetime.now(UTC)
        ):
            return
        saved_document_id = await documents.save_extraction(
            session,
            document_id,
            source_id,
            parsed.text,
            [
                {
                    "content": draft.content,
                    "token_count": draft.token_count,
                    "metadata": draft.metadata,
                }
                for draft in drafts
            ],
            extraction_status,
            parsed.metadata,
            parsed.warnings,
            "p02-t2-v1",
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
            expected_raw_uri=upload.raw_uri,
            expected_mime_type=upload.mime_type,
        )
        if saved_document_id != document_id:
            return
        work.stage.status = "succeeded"
        work.stage.result_count = len(drafts)
        work.stage.lease_expires_at = None
        work.stage.error_code = None
        work.run.status = extraction_status
        work.run.error_code = None
        await ingestion_api.mark_event_delivered(
            session, identifier, scope=work.scope, multi_workspace_enabled=enabled
        )
        changed = await sources.record_processing_result_in_uow(
            session,
            source_id,
            work.source.generation,
            datetime.now(UTC),
            None,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
        )
        extras: list[ReplayDraft] = [
            make_knowledge_change(source_id, document_id, scope=work.scope)
        ]
        if changed:
            extras.append(
                make_source_change(
                    source_id, work.source.generation, work.source.status, scope=work.scope
                )
            )
        await _commit_ingestion_change(
            session,
            work.run,
            work.stage,
            tuple(extras),
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
        )
        count("ingestion_documents_total", outcome="processed")


async def _record_extraction_failure(
    factory: async_sessionmaker[AsyncSession],
    identifier: UUID,
    enabled: bool,
    upload: _UploadClaim,
    exc: Exception,
) -> None:
    """Record any extraction failure as a failed stage/run/event and a ``failed`` document, under the original claim.

    The failure code is ``parser_timeout`` for timeouts and ``parse_failed`` otherwise. Silent no-op when the
    claim is gone or Documents refuses the status change; commits with replay otherwise.
    """
    document_id, source_id = upload.document_id, upload.source_id
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            identifier,
            multi_workspace_enabled=enabled,
            event_types=("document.file.uploaded",),
            expected=upload.claim,
        )
        if work is None:
            return
        code = _failure_code(exc)
        work.stage.status = "failed"
        work.stage.error_code = code
        work.stage.lease_expires_at = None
        work.run.status = "failed"
        work.run.error_code = code
        work.event.status = "failed"
        if not await documents.set_extraction_status(
            session,
            document_id,
            source_id,
            "failed",
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
            expected_raw_uri=upload.raw_uri,
            expected_mime_type=upload.mime_type,
        ):
            return
        changed = await sources.record_processing_result_in_uow(
            session,
            source_id,
            work.source.generation,
            datetime.now(UTC),
            code,
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
            source_fence=work.source,
        )
        extras: list[ReplayDraft] = [
            make_knowledge_change(source_id, document_id, scope=work.scope)
        ]
        if changed:
            extras.append(
                make_source_change(
                    source_id, work.source.generation, work.source.status, scope=work.scope
                )
            )
        await _commit_ingestion_change(
            session,
            work.run,
            work.stage,
            tuple(extras),
            scope=work.scope,
            multi_workspace_enabled=enabled,
            access_fence=work.access_fence,
        )
        count("ingestion_documents_total", outcome="failed")


@bounded_heavy_work
@timed("ingestion_stage_ms", stage="extract")
async def process_uploaded_file(ctx: dict[str, object], event_id: str) -> None:
    """Parse a proven stored upload outside SQL, then settle its original dispatch/stage CAS.

    The loader prepares exact Document/URI/MIME before Ingestion roots/outbox on initial,
    publication and failure phases; late status/save consume those caller-held locks.
    Documents verifies stored URI/MIME/Source/document under the held admission
    before raw bytes are read. Legacy blobs remain usable only through that owner proof;
    new blobs require this workspace prefix. Publication reacquires the original fences,
    compares the original attempt and uses only transaction-local owner mutations.

    Phases: ``_claim_upload_extraction`` (SQL released afterwards) -> download and parse outside SQL ->
    ``_publish_extraction``; any exception during parse or publication is recorded by
    ``_record_extraction_failure``.
    """
    factory = worker_session_factory(ctx)
    settings = cast(Settings, ctx["settings"])
    enabled = _worker_flag(ctx)
    identifier = UUID(event_id)
    upload = await _claim_upload_extraction(factory, settings, identifier, enabled)
    if upload is None:
        return
    try:
        # The row/URI proof and original dispatch were captured before closing the session.
        async with get_storage_backend(settings).download_to_temp(upload.raw_uri) as source_path:
            parsed = await parse_file_bounded(
                source_path,
                upload.mime_type,
                settings.parser_timeout_seconds,
                settings.docx_expanded_max_bytes,
                settings.pdf_page_max,
            )
        parsed = _cap_parsed_text(parsed, settings.parsed_text_max_chars)
        drafts = await to_thread_joined(chunk_text, parsed.text)
        extraction_status = "needs_ocr" if parsed.warnings and not parsed.text else "succeeded"
        await _publish_extraction(
            factory,
            identifier,
            enabled,
            upload,
            parsed=parsed,
            drafts=drafts,
            extraction_status=extraction_status,
        )
    except Exception as exc:  # noqa: BLE001  # extraction failure of any kind must be recorded as a failed stage
        await _record_extraction_failure(factory, identifier, enabled, upload, exc)


def _cap_parsed_text(parsed: ParsedDocument, limit: int) -> ParsedDocument:
    """Bound chunking input: keep the first `limit` chars and record the truncation as a document warning."""
    text = parsed.text
    if len(text) <= limit:
        return parsed
    logger.warning("Parsed text truncated to %s of %s chars", limit, len(text))
    return replace(
        parsed,
        text=text[:limit],
        warnings=[*parsed.warnings, "parsed_text_truncated"],
        metadata={**parsed.metadata, "truncated_from_chars": len(text), "kept_chars": limit},
    )


def _failure_code(exc: Exception) -> str:
    """Map an extraction failure to its stored error code: ``parser_timeout`` for timeouts, else ``parse_failed``."""
    if isinstance(exc, TimeoutError):
        return "parser_timeout"
    return "parse_failed"


async def cleanup_storage_orphans(ctx: dict[str, object]) -> int:
    """Sweep global legacy/scoped storage under deliberate bootstrap maintenance admission.

    An active bootstrap account admits this internal operation;
    no selected workspace/member endpoint or per-job fallback is introduced. Documents
    supplies complete global reference proof before the dual-prefix grace-period sweep.
    SQL is closed before filesystem work.
    """
    from core.auth.public import get_active_account

    factory = worker_session_factory(ctx)
    settings = cast(Settings, ctx["settings"])
    enabled = _worker_flag(ctx)
    if enabled:
        # ponytail: multiworkspace orphan sweep disabled until O instance-operator admission exists (ruling §6)
        return 0
    async with factory() as session:
        if await get_active_account(session, 1, multi_workspace_enabled=enabled) is None:
            return 0
    async with factory() as session:
        referenced = await documents.raw_uris(
            session,
            instance_operator=True,
            multi_workspace_enabled=enabled,
        )
    return await get_storage_backend(settings).cleanup_orphans(
        referenced, settings.storage_orphan_grace_seconds
    )


async def purge_collection_receipts(ctx: dict[str, object]) -> int:
    """Delete one bounded slice (500) of expired, no-longer-recoverable acceptance receipts.

    Runs in its own session and commits itself; the remaining expired rows are taken by later runs.

    Also purges old connector request/send/quota history and delivered ingestion stage outbox rows.
    """
    factory = worker_session_factory(ctx)
    async with factory() as session:
        deleted = await ingestion_api.purge_expired_collection_receipts(session, limit=500)
        await session.commit()
    # Same hourly cron also trims other append-only bookkeeping, each in its own short transaction so
    # one failing table cannot block the receipts above. The receipt count stays the return value.
    settings = ctx.get("settings")
    outbox_days = getattr(settings, "outbox_retention_days", None)
    for purge, extra in (
        (connectors.purge_collection_history, {}),
        (ingestion_api.purge_delivered_outbox, {"retention_days": outbox_days}),
        (ingestion_api.purge_expired_datasource_data, {}),
    ):
        try:
            async with factory() as session:
                await purge(session, limit=1000, **extra)
                await session.commit()
        except Exception:
            logger.warning("collection retention step %s failed", purge.__name__, exc_info=True)
    return deleted
