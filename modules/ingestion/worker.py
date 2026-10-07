from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from arq import Retry
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.chunking import chunk_text
from core.config import Settings
from core.events import DomainEvent
from core.heavy_work import bounded_heavy_work, to_thread_joined
from core.realtime import (
    ReplayDraft,
    commit_with_replay,
    make_ingestion_change,
    make_knowledge_change,
    make_source_change,
)
from core.storage import cleanup_orphaned_files, storage_path
from core.telemetry import count, set_trace, timed
from modules.connectors.public import CollectionFence, ConnectorRecord
from modules.ingestion.dispatcher import mark_event_delivered
from modules.ingestion.models import (
    COLLECTION_LEASE,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.ingestion.parsers import parse_file_bounded
from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.schemas import NormalizedDocumentInput
from modules.sources import public as sources

logger = logging.getLogger("bbd.worker")
STAGE_TIMEOUT_SECONDS = 120
MAX_STAGE_ATTEMPTS = 5
NORMALIZATION_VERSION = 1
NORMALIZATION_BATCH_RECORDS = 32
NORMALIZATION_BATCH_BYTES = 4 * 1024 * 1024


async def _commit_ingestion_change(
    session: AsyncSession,
    run: IngestionRun,
    stage: IngestionStage,
    extras: tuple[ReplayDraft, ...] = (),
) -> None:
    """Commit stage state and related realtime changes through replay protection."""
    await commit_with_replay(
        session,
        [make_ingestion_change(run.source_id, run.id, run.status, stage.stage_key, stage.status), *extras],
    )
    if stage.status in {"succeeded", "failed"}:
        count("ingestion_stages_total", stage=stage.stage_key, outcome=stage.status)
    if run.status in {"succeeded", "failed"}:
        count("ingestion_runs_total", outcome=run.status)


async def _refresh_run_status(session: AsyncSession, run: IngestionRun) -> None:
    """Derive run status from its stages, preserving the first failure code."""
    stages = list((await session.scalars(
        select(IngestionStage).where(IngestionStage.run_id == run.id)
    )).all())
    if any(stage.status == "failed" for stage in stages):
        run.status = "failed"
        run.error_code = next((stage.error_code for stage in stages if stage.status == "failed"), "stage_failed")
    elif stages and all(stage.status == "succeeded" for stage in stages):
        run.status = "succeeded"
        run.error_code = None
    else:
        run.status = "queued"
        run.error_code = None


class ConnectorRetryError(OSError):
    """Represent a retryable connector failure with an optional server delay."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Store the Retry-After hint alongside the transport error."""
        super().__init__(message)
        self.retry_after = retry_after


def _update_native_run_lease(state: SourceIngestionState | None, run_id: UUID, *, terminal: bool) -> None:
    """Renew or release only this native run's source lease at slice boundaries.

    A live fetch token or newer run is never changed; terminalization clears only
    a matching run owner, while bounded retry/slice work refreshes its expiry.
    """
    if state is None or state.lease_run_id != run_id:
        return
    if terminal:
        state.lease_run_id = None
        state.lease_expires_at = None
    else:
        state.lease_expires_at = datetime.now(UTC) + COLLECTION_LEASE


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse Retry-After seconds or date values, capped at 60 seconds."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, min(60.0, float(value)))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            return max(0.0, min(60.0, (target - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


async def _collect_web_job(
    ctx: dict[str, object],
    factory: async_sessionmaker[AsyncSession],
    event: EventOutbox,
    run_id: UUID,
    stage_id: UUID,
) -> None:
    """Collect one active web stage after checking its persisted source and lease fence.

    The worker owns the existing heavy-work slot; this helper never reacquires it.
    Source, connector, run, stage, and lease authority is checked in a short local
    transaction before egress and rechecked before receipt publication. Network
    I/O runs only after those row locks and the database session are released.
    Missing, coercible (including bool), or stale revisions fail closed.

    Generic-only preflight: the source provider is immutable after creation, so a
    stale or directly queued crawl event cannot route a native source through the
    browser collector; native providers are rejected before any external request.
    """
    source_id = UUID(str(event.payload["source_id"]))
    async with factory() as session:
        source_view = await sources.get_connector_source(session, source_id)
        from modules.connectors import public as connectors

        if source_view is None:
            raise ValueError("Crawl source is unavailable")
        if connectors.is_native_provider(source_view.provider):
            raise ValueError("Native provider collection is required")
    settings = cast(Settings, ctx["settings"])
    config = cast(dict[str, Any], event.payload["configuration"])
    token = settings.browser_shared_token.get_secret_value()
    if not token:
        raise ValueError("Browser collector is not configured")
    source_id = UUID(str(event.payload["source_id"]))
    source_generation = event.payload.get("source_generation")
    connector_revision = event.payload.get("connector_revision")
    if (
        type(source_generation) is not int
        or source_generation < 1
        or type(connector_revision) is not int
        or connector_revision < 1
    ):
        raise ValueError("Crawl job is missing a valid persisted collection fence")
    fence = CollectionFence(
        source_generation=source_generation,
        connector_revision=connector_revision,
    )
    # Revalidate the durable claim immediately before egress. Do not keep the
    # source/run/stage locks across the remote request; publication rechecks them.
    async with factory() as session:
        source = await sources.lock_source(session, source_id)
        source_view = await sources.get_connector_source(session, source_id)
        from modules.connectors import public as connectors

        run = await session.scalar(
            select(IngestionRun).where(
                IngestionRun.id == run_id, IngestionRun.source_id == source_id
            ).with_for_update()
        )
        stage = await session.scalar(
            select(IngestionStage).where(
                IngestionStage.id == stage_id, IngestionStage.run_id == run_id
            ).with_for_update()
        )
        state = await session.get(SourceIngestionState, source_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            source is None
            or source.status != "active"
            or source.generation != source_generation
            or source_view is None
            or not await connectors.require_collection_fence(session, source_view, fence)
            or run is None
            or run.status != "running"
            or stage is None
            or stage.status != "running"
            or stage.lease_expires_at is None
            or stage.lease_expires_at <= now
            or state is None
            or state.lease_run_id != run_id
            or state.lease_expires_at is None
            or state.lease_expires_at <= now
        ):
            raise ValueError("Crawl job is no longer active")
    payload = {
        "source_id": str(source_id),
        "source_generation": source_generation,
        "connector_revision": connector_revision,
        "url": config["url"],
        "mode": config["mode"],
        "max_pages": config["max_pages"],
        "max_depth": config["max_depth"],
        "timeout_seconds": config["timeout_seconds"],
    }
    try:
        async with httpx.AsyncClient(timeout=int(config["timeout_seconds"]) + 5) as client:
            response = await client.post(
                f"{str(settings.browser_service_url).rstrip('/')}/crawl",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code in {408, 425, 429} or response.status_code >= 500:
                raise ConnectorRetryError(
                    f"Browser collector returned HTTP {response.status_code}",
                    _retry_after_seconds(response),
                )
            if response.status_code >= 400:
                raise ValueError(f"Browser collector rejected the job with HTTP {response.status_code}")
    except httpx.TimeoutException as exc:
        raise TimeoutError("Browser collection timed out") from exc
    except httpx.NetworkError as exc:
        raise OSError("Browser collection transport failed") from exc
    try:
        raw_records = response.json()
        records = [ConnectorRecord.model_validate(item) for item in raw_records]
    except (TypeError, ValueError) as exc:
        raise ValueError("Browser collector returned invalid records") from exc
    if not records or len(records) > 500:
        raise ValueError("Browser job returned no pages or too many records")

    canonical_records = []
    for record in records:
        data = record.model_dump(mode="json")
        canonical_records.append(data)
    collected_at = datetime.now(UTC)
    payload_hash = hashlib.sha256(
        json.dumps(canonical_records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    async with factory() as session:
        source = await sources.lock_source(session, source_id)
        source_view = await sources.get_connector_source(session, source_id)
        from modules.connectors import public as connectors
        from modules.ingestion import public as ingestion

        fence_current = bool(
            source_view is not None
            and await connectors.require_batch_fence(
                session,
                source_view,
                source_generation,
                connector_revision,
            )
        )
        run = await session.scalar(
            select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.source_id == source_id).with_for_update()
        )
        stage = await session.scalar(select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update())
        state = await session.get(SourceIngestionState, source_id, with_for_update=True)
        batch = await session.scalar(select(IngestionBatch).where(IngestionBatch.id == run.batch_id)) if run else None
        if (
            source is None or source.status != "active"
            or source.generation != source_generation
            or not fence_current
            or run is None or stage is None or batch is None
            or state is None or state.lease_run_id != run.id
        ):
            raise ValueError("Crawl job is no longer active")

        received_at = datetime.now(UTC)
        observations = []
        for data in canonical_records:
            record_observed_at = datetime.fromisoformat(str(data["observed_at"]).replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
            if record_observed_at.tzinfo is None:
                raise ValueError("Browser collector returned a naive observation time")
            record_observed_at = record_observed_at.astimezone(UTC)
            record_hash = hashlib.sha256(
                json.dumps(
                    {"version": data.get("version"), "content": data["content"], "metadata": data["metadata"]},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            observations.append(
                {
                    "source_id": source_id,
                    "batch_id": batch.id,
                    "provider_id": data["provider_id"],
                    "record_hash": record_hash,
                    "payload": data,
                    "observed_at": record_observed_at,
                    "received_at": received_at,
                    "collected_at": collected_at,
                }
            )
        await session.execute(
            pg_insert(SourceObservation)
            .values(observations)
            .on_conflict_do_nothing(constraint="uq_source_observations_batch_record_observed")
        )
        batch.payload_hash = payload_hash
        batch.source_generation = source.generation
        cursor_after = max(
            (datetime.fromisoformat(str(data["observed_at"]).replace("Z", "+00:00")).astimezone(UTC).isoformat()  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
             for data in canonical_records),
            default=state.cursor,
        )
        if state.cursor:
            try:
                prior_cursor = datetime.fromisoformat(state.cursor.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
                assert cursor_after is not None
                latest = datetime.fromisoformat(cursor_after.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
                if prior_cursor.tzinfo is not None and prior_cursor > latest:
                    cursor_after = state.cursor
            except ValueError:
                pass
        state.cursor = cursor_after
        await session.flush()
        normalize_stage = await ingestion.schedule_normalization(
            session, run, batch, source.generation, received_at
        )
        drafts = []
        if normalize_stage is not None:
            drafts.append(make_ingestion_change(
                source.id, run.id, run.status, normalize_stage.stage_key, normalize_stage.status
            ))
        await commit_with_replay(session, drafts)


async def _fail_ingestion_stage(
    factory: async_sessionmaker[AsyncSession],
    event_id: UUID,
    run_id: UUID,
    stage_id: UUID,
    error_code: str,
) -> None:
    """Persist a terminal stage failure and refresh the owning run status."""
    async with factory() as session:
        run_hint = await session.get(IngestionRun, run_id)
        if run_hint is None:
            event = await session.get(EventOutbox, event_id, with_for_update=True)
            if event is not None:
                event.status = "failed"
                await session.commit()
            return
        source = await sources.lock_source(session, run_hint.source_id)
        if source is not None:
            await sources.get_connector_source(session, run_hint.source_id)

        run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id).with_for_update())
        stage = await session.scalar(
            select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update()
        )
        event = await session.get(EventOutbox, event_id, with_for_update=True)
        if stage is None or run is None or event is None:
            return
        stage.status = "failed"
        stage.error_code = error_code
        stage.lease_expires_at = None
        run.status = "failed"
        run.error_code = error_code
        event.status = "failed"
        source_changed = False
        if source is not None:
            source_changed = await sources.record_collection_result(
                session,
                source.id,
                int(event.payload.get("source_generation", source.generation)),
                datetime.now(UTC),
                error_code,
            )
        state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
        if state is not None and state.lease_run_id == run.id:
            state.lease_run_id = None
            state.lease_expires_at = None
        logger.warning("Ingestion stage failed run_id=%s stage_id=%s error_code=%s", run.id, stage.id, error_code)
        extras = (make_source_change(source.id, source.generation, source.status),) if source is not None and source_changed else ()
        await _commit_ingestion_change(session, run, stage, extras)


@timed("ingestion_stage_ms", stage="collect")
async def process_ingestion_event(ctx: dict[str, object], event_id: str) -> None:
    """Process one durable ingestion stage with a committed lease and source fence.

    Commits the running stage lease before bounded work outside that transaction,
    then rechecks source generation before completion. Timeout, OSError,
    OperationalError and connector retry hints schedule durable delays up to five
    attempts and raise ARQ Retry; other failures terminalize the stage. A stale
    or inactive source is terminal rather than retried. Provider routing uses the
    detached connector projection read while the source lifecycle lock is held.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    identifier = UUID(event_id)
    async with factory() as session:
        event = await session.get(EventOutbox, identifier)
        if event is None or event.status == "delivered":
            return
        run_id = UUID(str(event.payload["run_id"]))
        stage_id = UUID(str(event.payload["stage_id"]))
        set_trace(ingestion_run_id=str(run_id))
        run_hint = await session.get(IngestionRun, run_id)
        if run_hint is None:
            event.status = "failed"
            await session.commit()
            return
        source = await sources.lock_source(session, run_hint.source_id)
        source_projection = (
            await sources.get_connector_source(session, run_hint.source_id) if source is not None else None
        )
        run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id).with_for_update())
        stage = await session.scalar(
            select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update()
        )
        if stage is None or run is None:
            event.status = "failed"
            await session.commit()
            return
        if (
            source is None or source_projection is None or source.status != "active"
            or source_projection.status != source.status
            or source_projection.generation != source.generation
            or source.generation != int(event.payload.get("source_generation", source.generation))
        ):
            stage.status = "failed"
            stage.error_code = "source_unavailable"
            run.status = "failed"
            run.error_code = "source_unavailable"
            event.status = "failed"
            state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
            if state is not None and state.lease_run_id == run.id:
                state.lease_run_id = None
                state.lease_expires_at = None
            logger.warning("Ingestion stage rejected run_id=%s stage_id=%s source unavailable", run.id, stage.id)
            await _commit_ingestion_change(session, run, stage)
            return
        now = datetime.now(UTC)
        if stage.status == "succeeded":
            await mark_event_delivered(session, identifier)
            return
        if stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now:
            return
        state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
        if (
            state is None
            or state.lease_run_id != run.id
            or state.lease_expires_at is None
            or state.lease_expires_at <= now
        ):
            stage.status = "failed"
            stage.error_code = "lease_expired"
            run.status = "failed"
            run.error_code = "lease_expired"
            event.status = "failed"
            if state is not None and state.lease_run_id == run.id:
                state.lease_run_id = None
                state.lease_expires_at = None
            logger.warning("Ingestion stage rejected run_id=%s stage_id=%s lease expired", run.id, stage.id)
            await _commit_ingestion_change(session, run, stage)
            return
        stage.status = "running"
        stage.attempts += 1
        stage.lease_expires_at = now + timedelta(seconds=STAGE_TIMEOUT_SECONDS)
        state.lease_expires_at = now + COLLECTION_LEASE
        stage.error_code = None
        run.status = "running"
        logger.info("Ingestion stage started run_id=%s stage_id=%s attempt=%s", run.id, stage.id, stage.attempts)
        await _commit_ingestion_change(session, run, stage)

    # External collection runs after the lease commit; it cannot share the database transaction.
    try:
        # Stage work is deliberately bounded; later ingestion tasks add extraction consumers.
        async with asyncio.timeout(STAGE_TIMEOUT_SECONDS):
            if event.type == "connector.crawl.requested":
                await _collect_web_job(ctx, factory, event, run_id, stage_id)
            async with factory() as session:
                observed = await session.scalar(
                    select(func.count()).select_from(SourceObservation).where(
                        SourceObservation.batch_id == (await session.scalar(
                            select(IngestionRun.batch_id).where(IngestionRun.id == run_id)
                        ))
                    )
                )
                if not observed:
                    raise RuntimeError("Accepted ingestion batch has no observations")
    except (TimeoutError, OSError, OperationalError) as exc:
        async with factory() as session:
            run_hint = await session.get(IngestionRun, run_id)
            if run_hint is None:
                return
            source = await sources.lock_source(session, run_hint.source_id)
            run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id).with_for_update())
            stage = await session.scalar(
                select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update()
            )
            event = await session.get(EventOutbox, identifier, with_for_update=True)
            if (
                source is None or source.status != "active" or event is None
                or source.generation != int(event.payload.get("source_generation", -1))
            ):
                if stage is not None:
                    stage.status = "failed"
                    stage.error_code = "source_unavailable"
                if run is not None:
                    run.status = "failed"
                    run.error_code = "source_unavailable"
                if event is not None:
                    event.status = "failed"
                state = await session.get(SourceIngestionState, run_hint.source_id, with_for_update=True)
                if state is not None and state.lease_run_id == run_id:
                    state.lease_run_id = None
                    state.lease_expires_at = None
                if run is not None and stage is not None:
                    await _commit_ingestion_change(session, run, stage)
                else:
                    await session.commit()
                return
            if stage is None or run is None or event is None:
                return
            attempt = stage.attempts
            if attempt >= MAX_STAGE_ATTEMPTS:
                await session.rollback()
                await _fail_ingestion_stage(factory, identifier, run_id, stage_id, "retry_exhausted")
                return
            delay = (
                max(0.5, exc.retry_after)
                if isinstance(exc, ConnectorRetryError) and exc.retry_after is not None
                else random.uniform(0.5, min(60.0, 2.0 ** attempt))
            )
            stage.status = "retrying"
            stage.error_code = "transient_failure"
            stage.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            stage.lease_expires_at = None
            run.status = "queued"
            event.status = "pending"
            event.next_attempt_at = stage.next_attempt_at
            state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
            if state is not None and state.lease_run_id == run.id:
                state.lease_expires_at = datetime.now(UTC) + COLLECTION_LEASE
            logger.warning(
                "Ingestion stage retry scheduled run_id=%s stage_id=%s attempt=%s",
                run.id,
                stage.id,
                attempt,
            )
            await _commit_ingestion_change(session, run, stage)
        raise Retry(defer=delay) from exc
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        await _fail_ingestion_stage(factory, identifier, run_id, stage_id, "stage_failed")
        return

    async with factory() as session:
        run_hint = await session.get(IngestionRun, run_id)
        if run_hint is None:
            return
        source = await sources.lock_source(session, run_hint.source_id)
        source_projection = (
            await sources.get_connector_source(session, run_hint.source_id) if source is not None else None
        )
        from modules.connectors import public as connectors

        native_source = source_projection is not None and connectors.is_native_provider(source_projection.provider)
        run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id).with_for_update())
        stage = await session.scalar(
            select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update()
        )
        if stage is None or run is None:
            return
        if source is None or source_projection is None or source.status != "active" or source.generation != int(event.payload.get("source_generation", -1)):
            stage.status = "failed"
            stage.error_code = "source_unavailable"
            run.status = "failed"
            run.error_code = "source_unavailable"
            state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
            if state is not None and state.lease_run_id == run.id:
                state.lease_run_id = None
                state.lease_expires_at = None
            await _commit_ingestion_change(session, run, stage)
            return
        stage.status = "succeeded"
        stage.error_code = None
        stage.lease_expires_at = None
        await _refresh_run_status(session, run)
        source_changed = await sources.record_collection_result(
            session,
            source.id,
            int(event.payload.get("source_generation", source.generation)),
            datetime.now(UTC),
            None,
        )
        state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
        if native_source:
            _update_native_run_lease(state, run.id, terminal=run.status in {"succeeded", "failed"})
        elif state is not None and state.lease_run_id == run.id:
            state.lease_run_id = None
            state.lease_expires_at = None
        event_row = await session.get(EventOutbox, identifier, with_for_update=True)
        if event_row is not None:
            event_row.status = "delivered"
        logger.info("Ingestion stage completed run_id=%s stage_id=%s", run.id, stage.id)
        extras = (make_source_change(source.id, source.generation, source.status),) if source_changed else ()
        await _commit_ingestion_change(session, run, stage, extras)


@timed("ingestion_stage_ms", stage="normalize")
async def process_normalize_event(ctx: dict[str, object], event_id: str) -> None:
    """Materialize one bounded slice and retain native collection ownership until terminal.

    PostgreSQL progress makes each slice resumable. For native providers, the
    matching source run lease is renewed while work remains and released only
    when normalization or its owning run becomes terminal; generic ingestion
    keeps its existing lease lifecycle. Provider validation uses the detached
    connector projection obtained under the source lock. Telegram selection
    time must agree across its persisted record, raw Bot API clock, and selected
    observation column before current-version ordering is authorized. Structured
    observations select by accepted time and ingestion identity; their winning
    immutable document version is projected through Documents before commit.
    If Observations is lifecycle-disabled, world normalization stays durably
    pending instead of consuming its accepted canonical records.
    Source ownership is locked before run, stage, and progress rows, and a newer
    token or run is never modified.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    identifier = UUID(event_id)
    run_id: UUID | None = None
    stage_id: UUID | None = None
    delay = 60.0
    try:
        async with factory() as session:
            event = await session.get(EventOutbox, identifier)
            if event is None or event.status == "delivered":
                return
            if event.type != "ingestion.normalize.requested":
                raise ValueError("Unexpected event type for normalization worker")
            run_id = UUID(str(event.payload["run_id"]))
            stage_id = UUID(str(event.payload["stage_id"]))
            run_hint = await session.get(IngestionRun, run_id)
            if run_hint is None:
                event.status = "failed"
                await session.commit()
                return
            source = await sources.lock_source(session, run_hint.source_id)
            source_projection = (
                await sources.get_connector_source(session, run_hint.source_id) if source is not None else None
            )
            from modules.connectors import public as connectors

            native_source = source_projection is not None and connectors.is_native_provider(source_projection.provider)
            run = await session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id).with_for_update()
            )
            stage = await session.scalar(
                select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update()
            )
            event = await session.scalar(
                select(EventOutbox).where(EventOutbox.id == identifier)
                .with_for_update().execution_options(populate_existing=True)
            )
            generation = int(event.payload.get("source_generation", -1)) if event is not None else -1
            if (
                source is None or source.status != "active" or source.generation != generation
                or source_projection is None
                or source_projection.status != source.status
                or source_projection.generation != source.generation
                or run is None or stage is None or event is None
                or stage.stage_key != "normalize" or run.id != stage.run_id
            ):
                if stage is not None:
                    stage.status = "failed"
                    stage.error_code = "source_generation_changed"
                if run is not None:
                    run.status = "failed"
                    run.error_code = "source_generation_changed"
                if event is not None:
                    event.status = "failed"
                if run is not None and stage is not None:
                    if native_source:
                        state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
                        _update_native_run_lease(state, run.id, terminal=True)
                    await _commit_ingestion_change(session, run, stage)
                else:
                    await session.commit()
                return
            if stage.status == "succeeded":
                event.status = "delivered"
                if native_source:
                    state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
                    _update_native_run_lease(state, run.id, terminal=True)
                await session.commit()
                return

            if source_projection.provider in {"alpha_vantage", "open_meteo"}:
                from modules.settings.public import module_is_enabled

                if not await module_is_enabled(session, "knowledge.observations"):
                    # The connector already accepted canonical provider records; retain their
                    # normalization progress until the owning observation projection is enabled.
                    deferred_until = datetime.now(UTC) + timedelta(seconds=60)
                    stage.status = "pending"
                    stage.next_attempt_at = deferred_until
                    stage.lease_expires_at = None
                    run.status = "queued"
                    run.error_code = None
                    event.status = "pending"
                    event.next_attempt_at = deferred_until
                    if native_source:
                        state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
                        _update_native_run_lease(state, run.id, terminal=False)
                    await _commit_ingestion_change(session, run, stage)
                    return

            stage.status = "running"
            stage.lease_expires_at = datetime.now(UTC) + timedelta(seconds=STAGE_TIMEOUT_SECONDS)
            stage.error_code = None
            run.status = "running"
            if native_source:
                # Keep the reservation alive across each bounded normalization slice.
                state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
                _update_native_run_lease(state, run.id, terminal=False)
            rows = list((await session.execute(
                select(ObservationNormalization, SourceObservation)
                .join(SourceObservation, SourceObservation.id == ObservationNormalization.observation_id)
                .where(
                    ObservationNormalization.stage_id == stage.id,
                    ObservationNormalization.disposition == "pending",
                )
                .order_by(SourceObservation.provider_id, SourceObservation.id)
                .limit(NORMALIZATION_BATCH_RECORDS)
                .with_for_update(of=ObservationNormalization)
            )).all())
            used_bytes = 0
            processed = 0
            processed_documents = 0
            failed_documents = 0
            knowledge_changes = []
            from modules.ingestion import public as ingestion_api
            for progress, observation in rows:
                data_bytes = len(json.dumps(observation.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if processed and used_bytes + data_bytes > NORMALIZATION_BATCH_BYTES:
                    break
                if data_bytes > NORMALIZATION_BATCH_BYTES:
                    progress.disposition = "failed"
                    progress.error_code = "record_exceeds_normalization_limit"
                    processed += 1
                    continue
                used_bytes += data_bytes
                try:
                    if progress.source_generation != generation:
                        progress.disposition = "skipped"
                        progress.error_code = "source_generation_changed"
                        processed += 1
                        continue
                    persisted_payload = dict(observation.payload)
                    native_telegram_envelope = persisted_payload.pop("_native_telegram", None)
                    record = IngestionRecord.model_validate(persisted_payload)
                    if record.provider_id != observation.provider_id:
                        raise ValueError("Provider identity does not match accepted observation")
                    accepted_hash = hashlib.sha256(json.dumps(
                        {"version": record.version, "content": record.content, "metadata": record.metadata},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8")).hexdigest()
                    if accepted_hash != observation.record_hash:
                        raise ValueError("Accepted observation hash does not match its payload")
                    raw_metadata = record.metadata
                    provider_record = None
                    telegram_order = None
                    provider_raw = raw_metadata.get("provider_record")
                    if provider_raw is not None:
                        from modules.knowledge.documents.schemas import (
                            ProviderRecordMetadata,
                            TelegramDocumentOrder,
                        )

                        provider_record = ProviderRecordMetadata.model_validate(provider_raw)
                        if (
                            provider_record.identity != record.provider_id
                            or provider_record.provider_version != record.version
                            or provider_record.provider != source_projection.provider
                        ):
                            raise ValueError("Typed provider provenance does not match the accepted source record")
                        provider_field_keys = {
                            "youtube": ("author", "summary", "title", "tags", "published_at", "provider_updated_at"),
                            "arxiv": ("author", "authors", "categories", "tags", "summary", "title", "published_at", "provider_updated_at"),
                            "huggingface": ("author", "tags", "pipeline_tag", "created_at", "last_modified"),
                            "github_releases": ("node_id", "name", "body", "html_url", "tag_name", "draft", "prerelease", "author", "created_at", "published_at"),
                            "github": ("record_type", "node_id", "html_url"),
                            "telegram": (),
                        }[provider_record.provider]
                        source_fields = dict(provider_record.source_fields)
                        for key in provider_field_keys:
                            if key in raw_metadata and raw_metadata[key] is not None:
                                source_fields[key] = raw_metadata[key]
                        provider_record_data = provider_record.model_dump(mode="json")
                        provider_record_data["source_fields"] = source_fields
                        provider_record = ProviderRecordMetadata.model_validate(provider_record_data)
                        if provider_record.provider == "telegram":
                            detail = provider_record.telegram
                            from modules.ingestion.schemas import (
                                TelegramDeliveryProof,
                                TelegramRawDelivery,
                            )

                            if not isinstance(native_telegram_envelope, dict) or detail is None:
                                raise ValueError("Persisted Telegram raw delivery proof is missing")
                            proof = TelegramDeliveryProof.model_validate(native_telegram_envelope.get("proof"))
                            raw = TelegramRawDelivery.model_validate(native_telegram_envelope.get("raw_update"))
                            ingestion_api.validate_telegram_record_delivery(
                                record, provider_record, proof, raw,
                            )
                            if observation.observed_at != record.observed_at:
                                raise ValueError(
                                    "Persisted observation selection clock differs from verified Telegram clock"
                                )
                            provider_record_data = provider_record.model_dump(mode="json")
                            provider_record_data["telegram"]["raw_update_sha256"] = proof.raw_update_sha256
                            provider_record = ProviderRecordMetadata.model_validate(provider_record_data)
                            telegram_order = TelegramDocumentOrder(
                                bot_id=detail.bot_id, epoch=detail.epoch,
                                update_id=detail.update_id,
                            )
                        elif native_telegram_envelope is not None:
                            raise ValueError("Telegram delivery proof is not valid for this provider")
                    elif source_projection.provider in {"alpha_vantage", "open_meteo"}:
                        from modules.knowledge.documents.schemas import (
                            ProviderRecordMetadata,
                            WorldDataMeasurement,
                        )

                        measurement = WorldDataMeasurement.model_validate(raw_metadata.get("world_data"))
                        if measurement.provider != source_projection.provider:
                            raise ValueError("Structured measurement does not match configured source provider")
                        provider_record = ProviderRecordMetadata(
                            provider=measurement.provider, identity=record.provider_id,
                            provider_version=record.version, timestamp_basis="collection",
                            coverage="returned_snapshot", content_truncated=False,
                            world_data=measurement,
                        )
                    elif source_projection.provider in {"youtube", "arxiv", "huggingface", "github_releases", "github", "telegram"}:
                        raise ValueError("Native provider record metadata is missing")
                    title_value = raw_metadata.get("title")
                    title = title_value.strip()[:500] if isinstance(title_value, str) and title_value.strip() else record.provider_id[:500]
                    raw_url = raw_metadata.get("canonical_url", raw_metadata.get("url"))
                    canonical_url = None
                    if isinstance(raw_url, str):
                        parsed = urlsplit(raw_url)
                        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
                            canonical_url = raw_url[:2048]
                    published_at = None
                    published_value = raw_metadata.get("published_at")
                    if isinstance(published_value, str) and published_value:
                        try:
                            published_at = datetime.fromisoformat(published_value.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
                            if published_at.tzinfo is None:
                                published_at = published_at.replace(tzinfo=UTC)
                            published_at = published_at.astimezone(UTC)
                        except ValueError:
                            published_at = None
                    content_type = raw_metadata.get("content_type")
                    content_type = content_type[:64] if isinstance(content_type, str) else None
                    safe_metadata: dict[str, object] = {}
                    for key, limit in (("author", 500), ("language", 32), ("summary", 10_000), ("description", 10_000)):
                        value = raw_metadata.get(key)
                        if isinstance(value, str):
                            safe_metadata[key] = value[:limit]
                    for key in ("tags", "categories"):
                        value = raw_metadata.get(key)
                        if isinstance(value, str):
                            safe_metadata[key] = value[:2_000]
                        elif isinstance(value, list):
                            safe_metadata[key] = [item[:500] for item in value[:100] if isinstance(item, str)]
                    from modules.connectors import public as connectors
                    scope = await connectors.get_current_provider_scope(
                        session, observation.source_id, generation,
                    )
                    accepted_at = observation.received_at
                    if source_projection.provider in {"alpha_vantage", "open_meteo"} and (scope is None or accepted_at is None):
                        raise ValueError("Accepted world observation is missing its scope or acceptance clock")
                    provenance = {
                        "title": title, "canonical_url": canonical_url,
                        "published_at": published_at.isoformat() if published_at else None,
                        "content_type": content_type, "metadata": safe_metadata,
                    }
                    if provider_record is not None:
                        provenance["provider_record"] = provider_record.model_dump(mode="json")
                    if scope is not None:
                        provenance["provider_scope_discriminator"] = scope.discriminator
                    result = await documents.upsert_normalized_document(
                        session,
                        NormalizedDocumentInput(
                            source_id=observation.source_id,
                            expected_source_generation=generation,
                            observation_id=observation.id,
                            provider_id=record.provider_id,
                            provider_version=record.version,
                            accepted_record_hash=observation.record_hash,
                            normalization_version=NORMALIZATION_VERSION,
                            observed_at=observation.observed_at,
                            received_at=observation.received_at,
                            collected_at=observation.collected_at,
                            title=title,
                            canonical_url=canonical_url,
                            published_at=published_at,
                            content_type=content_type,
                            content=record.content,
                            provenance=provenance,
                            telegram_order=telegram_order,
                        ),
                    )
                    observation_write = None
                    if (
                        source_projection.provider in {"alpha_vantage", "open_meteo"}
                        and result.disposition != "tombstoned"
                        and result.document_id is not None and result.document_version_id is not None
                        and provider_record is not None and provider_record.world_data is not None
                    ):
                        from modules.knowledge.observations import public as observations
                        from modules.knowledge.observations.schemas import WorldMeasurement

                        if scope is None or accepted_at is None:
                            raise ValueError("Accepted world observation is missing its scope or acceptance clock")
                        observation_write = await observations.upsert_from_ingestion(
                            session, source_id=observation.source_id,
                            source_generation=generation,
                            ingestion_observation_id=observation.id,
                            document_id=result.document_id,
                            document_version_id=result.document_version_id,
                            external_id=record.provider_id,
                            provider_version=record.version,
                            observed_at=record.observed_at,
                            collected_at=record.collected_at or observation.collected_at or datetime.now(UTC),
                            accepted_at=accepted_at,
                            provider_scope_discriminator=scope.discriminator,
                            measurement=WorldMeasurement.model_validate(
                                provider_record.world_data.model_dump(mode="python")
                            ),
                        )
                        if observation_write is not None and observation_write.selected_current:
                            selected_document = await documents.select_current_world_document_version(
                                session, document_id=result.document_id,
                                document_version_id=result.document_version_id,
                                expected_source_generation=generation,
                                provider_scope_discriminator=scope.discriminator,
                            )
                            if not selected_document:
                                raise RuntimeError("Accepted current observation has no selectable document version")
                        if observation_write is not None:
                            result = result.model_copy(update={
                                "selected_current": observation_write.selected_current,
                            })
                        progress.selected_current = (
                            observation_write.selected_current if observation_write is not None else False
                        )
                    progress.disposition = {
                        "normalized": "normalized", "duplicate": "duplicate", "tombstoned": "skipped",
                    }[result.disposition]
                    if progress.disposition == "failed":
                        failed_documents += 1
                    else:
                        processed_documents += 1
                    progress.error_code = "document_deleted" if result.disposition == "tombstoned" else None
                    progress.document_id = result.document_id
                    progress.document_version_id = result.document_version_id
                    progress.chunk_count = result.chunk_count if result.created_version else 0
                    if result.created_version and result.chunk_count:
                        ready = DomainEvent(
                            id=uuid4(), type="document.version.ready", version=1,
                            occurred_at=datetime.now(UTC), producer="modules.ingestion",
                            payload={
                                "source_id": str(observation.source_id),
                                "document_id": str(result.document_id),
                                "document_version_id": str(result.document_version_id),
                                "source_generation": generation,
                                "version_number": result.version_number,
                            },
                        )
                        await ingestion_api.publish_event(session, ready)
                    if source_projection.provider in {"alpha_vantage", "open_meteo"}:
                        # Structured series can change even when the normalized document version is reused.
                        knowledge_changes.append(make_knowledge_change(observation.source_id))
                    elif result.selected_current and result.created_version:
                        knowledge_changes.append(make_knowledge_change(
                            observation.source_id, result.document_id, result.version_number
                        ))
                except (ValueError, TypeError):
                    progress.disposition = "failed"
                    progress.error_code = "invalid_normalization_record"
                    failed_documents += 1
                processed += 1

            pending_count = int(await session.scalar(
                select(func.count()).select_from(ObservationNormalization).where(
                    ObservationNormalization.stage_id == stage.id,
                    ObservationNormalization.disposition == "pending",
                )
            ) or 0)
            failed_count = int(await session.scalar(
                select(func.count()).select_from(ObservationNormalization).where(
                    ObservationNormalization.stage_id == stage.id,
                    ObservationNormalization.disposition == "failed",
                )
            ) or 0)
            chunk_total = int(await session.scalar(
                select(func.coalesce(func.sum(ObservationNormalization.chunk_count), 0))
                .where(ObservationNormalization.stage_id == stage.id)
            ) or 0)
            stage.result_count = chunk_total
            stage.lease_expires_at = None
            if pending_count:
                stage.status = "pending"
                run.status = "queued"
                event.status = "pending"
                event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=1)
            else:
                stage.status = "failed" if failed_count else "succeeded"
                stage.error_code = "normalization_failed" if failed_count else None
                stages = list((await session.scalars(
                    select(IngestionStage).where(IngestionStage.run_id == run.id)
                )).all())
                if any(item.status == "failed" for item in stages):
                    run.status = "failed"
                    run.error_code = next((item.error_code for item in stages if item.status == "failed"), "stage_failed")
                elif all(item.status == "succeeded" for item in stages):
                    run.status = "succeeded"
                    run.error_code = None
                else:
                    run.status = "queued"
                    run.error_code = None
                event.status = "failed" if failed_count else "delivered"
                if failed_count:
                    await sources.record_processing_result(
                        session, source.id, source.generation, datetime.now(UTC), "normalization_failed"
                    )
                else:
                    await sources.record_processing_result(
                        session, source.id, source.generation, datetime.now(UTC), None
                    )
            extras = [*knowledge_changes]
            if native_source:
                state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
                _update_native_run_lease(
                    state, run.id,
                    terminal=(not pending_count or run.status in {"succeeded", "failed"}),
                )
            if processed or not pending_count:
                await _commit_ingestion_change(session, run, stage, tuple(extras))
            else:
                await _commit_ingestion_change(session, run, stage)
            if processed_documents:
                count("ingestion_documents_total", processed_documents, outcome="processed")
            if failed_documents:
                count("ingestion_documents_total", failed_documents, outcome="failed")
    except OperationalError as exc:
        if run_id is None or stage_id is None:
            raise Retry(defer=delay) from exc
        async with factory() as session:
            hint = await session.get(IngestionRun, run_id)
            if hint is not None:
                source = await sources.lock_source(session, hint.source_id)
                run = await session.scalar(select(IngestionRun).where(IngestionRun.id == run_id).with_for_update())
                stage = await session.scalar(select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update())
                event = await session.scalar(
                    select(EventOutbox).where(EventOutbox.id == identifier)
                    .with_for_update().execution_options(populate_existing=True)
                )
                if source is not None and run is not None and stage is not None and event is not None:
                    if stage.attempts + 1 >= MAX_STAGE_ATTEMPTS:
                        stage.attempts += 1
                        stage.status = "failed"
                        stage.error_code = "retry_exhausted"
                        run.status = "failed"
                        run.error_code = "retry_exhausted"
                        event.status = "failed"
                    else:
                        stage.attempts += 1
                        delay = random.uniform(0.5, min(60.0, 2.0 ** stage.attempts))
                        stage.status = "retrying"
                        stage.error_code = "transient_failure"
                        stage.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                        event.status = "pending"
                        event.next_attempt_at = stage.next_attempt_at
                        run.status = "queued"
                    stage.lease_expires_at = None
                    source_projection = await sources.get_connector_source(session, hint.source_id)
                    from modules.connectors import public as connectors

                    if source_projection is not None and connectors.is_native_provider(source_projection.provider):
                        state = await session.get(SourceIngestionState, run.source_id, with_for_update=True)
                        _update_native_run_lease(
                            state, run.id,
                            terminal=(
                                stage.status == "failed"
                                or run.status in {"succeeded", "failed"}
                            ),
                        )
                    await _commit_ingestion_change(session, run, stage)
        raise Retry(defer=delay) from exc


@bounded_heavy_work
@timed("ingestion_stage_ms", stage="extract")
async def process_uploaded_file(ctx: dict[str, object], event_id: str) -> None:
    """Parse one staged upload after committing processing state and its lease.

    Runs the bounded parser outside the database transaction, then rechecks the
    active source generation before saving text and chunks. Parser exceptions are
    recorded as terminal failures rather than automatically retried; an empty
    PDF result with warnings is saved as ``needs_ocr``. The route owns deletion
    of raw bytes when intake fails or deduplicates.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    identifier = UUID(event_id)
    async with factory() as session:
        event = await session.get(EventOutbox, identifier)
        if event is None or event.status == "delivered":
            return
        run_id = UUID(str(event.payload["run_id"]))
        stage_id = UUID(str(event.payload["stage_id"]))
        document_id = UUID(str(event.payload["document_id"]))
        run_hint = await session.get(IngestionRun, run_id)
        if run_hint is None:
            event.status = "failed"
            await session.commit()
            return
        source_id = run_hint.source_id
        # Match source archive/retry order: source, run, stage, then document.
        source = await sources.lock_source(session, source_id)
        run = await session.scalar(
            select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.source_id == source_id).with_for_update()
        )
        stage = await session.scalar(select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update())
        document_exists = await documents.lock_document_for_extraction(session, document_id, source_id)
        if (
            stage is None or run is None or not document_exists or source is None
            or source.status != "active"
            or source.generation != int(event.payload.get("source_generation", source.generation))
        ):
            if stage is not None:
                stage.status = "failed"
                stage.error_code = "source_or_document_unavailable"
            if run is not None:
                run.status = "failed"
                run.error_code = "source_or_document_unavailable"
            event.status = "failed"
            if run is not None and stage is not None:
                await _commit_ingestion_change(session, run, stage)
            else:
                await session.commit()
            return
        now = datetime.now(UTC)
        if stage.status == "succeeded":
            event.status = "delivered"
            await session.commit()
            return
        if stage.status == "running" and stage.lease_expires_at and stage.lease_expires_at > now:
            return
        stage.status = "running"
        stage.attempts += 1
        stage.lease_expires_at = now + timedelta(seconds=settings.parser_timeout_seconds + 30)
        stage.error_code = None
        run.status = "running"
        await documents.set_extraction_status(session, document_id, source_id, "processing")
        await _commit_ingestion_change(
            session, run, stage,
            (make_knowledge_change(source_id, document_id),),
        )

    # Parser work is external to the committed processing-state transaction; failures are recorded below.
    try:
        raw_path = storage_path(settings.data_dir, str(event.payload["raw_uri"]))
        parsed = await parse_file_bounded(
            raw_path,
            str(event.payload["mime_type"]),
            settings.parser_timeout_seconds,
            settings.docx_expanded_max_bytes,
            settings.pdf_page_max,
        )
        parsed_text = _cap_parsed_text(parsed.text, settings.parsed_text_max_chars)
        drafts = await to_thread_joined(chunk_text, parsed_text)
        extraction_status = "needs_ocr" if parsed.warnings and not parsed.text else "succeeded"
        async with factory() as session:
            source = await sources.lock_source(session, source_id)
            run = await session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.source_id == source_id).with_for_update()
            )
            stage = await session.scalar(select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update())
            if (
                source is None or source.status != "active"
                or source.generation != int(event.payload.get("source_generation", -1))
                or run is None or stage is None
            ):
                if stage is not None:
                    stage.status = "failed"
                    stage.error_code = "source_unavailable"
                if run is not None:
                    run.status = "failed"
                    run.error_code = "source_unavailable"
                event = await session.get(EventOutbox, identifier, with_for_update=True)
                if event is not None:
                    event.status = "failed"
                if run is not None and stage is not None:
                    await _commit_ingestion_change(session, run, stage)
                else:
                    await session.commit()
                return
            saved_document_id = await documents.save_extraction(
                session,
                document_id,
                source_id,
                parsed_text,
                [
                    {"content": draft.content, "token_count": draft.token_count, "metadata": draft.metadata}
                    for draft in drafts
                ],
                extraction_status,
                parsed.metadata,
                parsed.warnings,
                "p02-t2-v1",
            )
            event = await session.get(EventOutbox, identifier, with_for_update=True)
            if saved_document_id is None or stage is None or run is None or event is None:
                return
            stage.status = "succeeded"
            stage.result_count = len(drafts)
            stage.lease_expires_at = None
            stage.error_code = None
            run.status = extraction_status if extraction_status == "needs_ocr" else "succeeded"
            run.error_code = None
            event.status = "delivered"
            source_changed = await sources.record_processing_result(session, source_id, source.generation, datetime.now(UTC), None)
            extras: list[ReplayDraft] = [make_knowledge_change(source_id, saved_document_id)]
            if source_changed:
                extras.append(make_source_change(source.id, source.generation, source.status))
            await _commit_ingestion_change(
                session, run, stage,
                tuple(extras),
            )
            count("ingestion_documents_total", outcome="processed")
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        async with factory() as session:
            source = await sources.lock_source(session, source_id)
            run = await session.scalar(
                select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.source_id == source_id).with_for_update()
            )
            stage = await session.scalar(select(IngestionStage).where(IngestionStage.id == stage_id).with_for_update())
            event = await session.get(EventOutbox, identifier, with_for_update=True)
            if (
                event is None or source is None or source.status != "active"
                or source.generation != int(event.payload.get("source_generation", -1))
            ):
                if event is not None:
                    event.status = "failed"
                await session.commit()
                return
            if stage is not None:
                stage.status = "failed"
                stage.error_code = _failure_code(exc)
                stage.lease_expires_at = None
            if run is not None:
                run.status = "failed"
                run.error_code = _failure_code(exc)
            await documents.set_extraction_status(session, document_id, source_id, "failed")
            if source is not None:
                code = _failure_code(exc)
                source_changed = await sources.record_processing_result(session, source_id, source.generation, datetime.now(UTC), code)
            else:
                source_changed = False
            if event is not None:
                event.status = "failed"
            if run is not None and stage is not None:
                extras = [make_knowledge_change(source_id, document_id)]
                if source is not None and source_changed:
                    extras.append(make_source_change(source.id, source.generation, source.status))
                await _commit_ingestion_change(
                    session, run, stage,
                    tuple(extras),
                )
                count("ingestion_documents_total", outcome="failed")
            else:
                await session.commit()


def _cap_parsed_text(text: str, limit: int) -> str:
    """Bound chunking input: keep the first `limit` chars and log the truncation instead of failing the upload."""
    if len(text) <= limit:
        return text
    logger.warning("Parsed text truncated to %s of %s chars", limit, len(text))
    return text[:limit]


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "parser_timeout"
    return "parse_failed"


async def cleanup_storage_orphans(ctx: dict[str, object]) -> int:
    """Remove unreferenced stored files and return the cleanup count."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    async with factory() as session:
        referenced = await documents.raw_uris(session)
    return await asyncio.to_thread(cleanup_orphaned_files, settings.data_dir, referenced, settings.storage_orphan_grace_seconds)
