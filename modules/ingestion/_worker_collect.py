"""Commit helpers, run status refresh and the web/connector collection job body."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.cron import notify_on_commit
from core.errors import ConflictError
from core.heavy_work import to_thread_joined
from core.realtime import (
    ReplayDraft,
    commit_with_replay,
    make_ingestion_change,
)
from core.telemetry import count
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors.public import ConnectorRecord
from modules.ingestion import public as ingestion_api
from modules.ingestion._worker_lock import _lock_worker_event
from modules.ingestion._worker_state import WorkerClaim, logger
from modules.ingestion.models import (
    COLLECTION_LEASE,
    EventOutbox,
    IngestionRun,
    IngestionStage,
    SourceIngestionState,
    SourceObservation,
)


def _wake_dispatcher_after_commit(session: AsyncSession, delay_seconds: float = 1.0) -> None:
    """Wake ``dispatch_pending_work`` once ``session`` commits an outbox row that was returned to ``pending``.

    The dispatcher poll is only a 30 s safety net, so every continuation path (normalization slices with rows
    left, transient retries, observation deferral) must wake it itself; otherwise each slice would wait for the
    poll. The wake is deferred by the row's backoff but capped at the 5 s wake-debounce window (a longer
    deferral would block fresher wakes); rows still not due then are picked up by the safety net.
    No-op without a wake hook; rolled-back transactions discard the wake.
    """
    notify_on_commit(
        session, "dispatch_pending_work", defer_seconds=min(max(delay_seconds, 1.0), 5.0)
    )


async def _commit_ingestion_change(
    session: AsyncSession,
    run: IngestionRun,
    stage: IngestionStage,
    extras: tuple[ReplayDraft, ...] = (),
    *,
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Commit scoped stage/outbox/replay once under the captured caller-held access fence.

    No earlier admission/Source lock is acquired late; replay must compare the retained
    principal and configuration before its head lock. The owner caller settles its claim.
    """
    await commit_with_replay(
        session,
        [
            make_ingestion_change(
                run.source_id, run.id, run.status, stage.stage_key, stage.status, scope=scope
            ),
            *extras,
        ],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    if stage.status in {"succeeded", "failed"}:
        count("ingestion_stages_total", stage=stage.stage_key, outcome=stage.status)
    if run.status in {"succeeded", "failed"}:
        count("ingestion_runs_total", outcome=run.status)


async def _refresh_run_status(
    session: AsyncSession,
    run: IngestionRun,
    *,
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Derive one caller-locked scoped run's status, preserving its first stage failure.

    Nonlocking current admission must equal the captured fence and retained root
    principal before stage reads; no earlier locks, hidden commit or cross-run count.
    """
    current = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        current != access_fence
        or run.workspace_id != scope.workspace_id
        or run.actor_user_id != scope.actor_user_id
        or run.membership_revision != scope.membership_revision
        or run.source_id != scope.source_id
    ):
        raise ConflictError("Original run admission changed")
    stages = list(
        (await session.scalars(select(IngestionStage).where(IngestionStage.run_id == run.id))).all()
    )
    if any(stage.status == "failed" for stage in stages):
        run.status = "failed"
        run.error_code = next(
            (stage.error_code for stage in stages if stage.status == "failed"), "stage_failed"
        )
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


def _update_native_run_lease(
    state: SourceIngestionState | None, run_id: UUID, *, terminal: bool
) -> None:
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


def _prepare_crawl_records(
    response: httpx.Response,
) -> tuple[list[ConnectorRecord], list[dict[str, Any]], str, list[str]]:
    """Decode, validate and hash one browser-collector batch (pure CPU, no I/O, no DB).

    Returns ``(records, canonical_records, payload_hash, per_record_hashes)``. Raises
    ``ValueError("Browser collector returned invalid records")`` exactly where the former inline
    block did (bad JSON, empty/oversized list, schema failure or native-provenance metadata).
    Runs through ``to_thread_joined`` because a 10 MB body with 500 records costs 0.2-0.4 s of CPU;
    the naive-timestamp check and all SQL stay on the loop in their original order.
    """
    try:
        raw_records = response.json()
        if not isinstance(raw_records, list) or not raw_records or len(raw_records) > 500:
            raise ValueError("Browser job returned no pages or too many records")
        records = [ConnectorRecord.model_validate(item) for item in raw_records]
        if any(
            {"provider_record", "world_data", "_owner_telegram_proof", "_native_telegram"}
            & record.metadata.keys()
            for record in records
        ):
            raise ValueError("Browser metadata cannot provide native provenance")
    except (TypeError, ValueError) as exc:
        raise ValueError("Browser collector returned invalid records") from exc
    canonical_records = [record.model_dump(mode="json") for record in records]
    payload_hash = hashlib.sha256(
        json.dumps(
            canonical_records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    record_hashes = [
        hashlib.sha256(
            json.dumps(
                {
                    "version": data.get("version"),
                    "content": data["content"],
                    "metadata": data["metadata"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        for data in canonical_records
    ]
    return records, canonical_records, payload_hash, record_hashes


async def _collect_web_job(
    ctx: dict[str, object],
    factory: async_sessionmaker[AsyncSession],
    event: EventOutbox,
    run_id: UUID,
    stage_id: UUID,
    *,
    claim: WorkerClaim,
    multi_workspace_enabled: bool,
) -> None:
    """Send a generic browser request under the exact original durable stage claim.

    Re-resolve scoped access/Source/Connector/state/run/stage/outbox before egress and
    publication. Each short session closes before HTTP; no native routing, current-epoch
    lease reconstruction or second gateway retry layer is permitted. C2 owns later exact
    browser request/capability/slot binding; service secret alone is not that proof.
    """

    settings = cast(Settings, ctx["settings"])
    token = settings.browser_shared_token.get_secret_value()
    if not token:
        raise ValueError("Browser collector is not configured")
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            event.id,
            multi_workspace_enabled=multi_workspace_enabled,
            event_types=("connector.crawl.requested",),
            expected=claim,
        )
        if (
            work is None
            or work.run.id != run_id
            or work.stage.id != stage_id
            or work.stage.status != "running"
            or work.stage.lease_expires_at is None
            or work.stage.lease_expires_at <= datetime.now(UTC)
            or work.state is None
            or work.state.lease_run_id != run_id
            or work.state.lease_expires_at is None
            or work.state.lease_expires_at <= datetime.now(UTC)
        ):
            raise ValueError("Crawl original claim is no longer active")
        config = dict(work.event.payload["configuration"])
        payload = {
            "source_id": str(work.source.id),
            "source_generation": work.source.generation,
            "connector_revision": work.event.payload["connector_revision"],
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
                raise ValueError(
                    f"Browser collector rejected the job with HTTP {response.status_code}"
                )
    except httpx.TimeoutException as exc:
        raise TimeoutError("Browser collection timed out") from exc
    except httpx.NetworkError as exc:
        raise OSError("Browser collection transport failed") from exc
    if len(response.content) > 10 * 1024 * 1024:
        raise ValueError("Browser batch exceeds the intake bound")
    records, canonical_records, payload_hash, record_hashes = await to_thread_joined(
        _prepare_crawl_records, response
    )
    collected_at = datetime.now(UTC)
    async with factory() as session:
        work = await _lock_worker_event(
            session,
            event.id,
            multi_workspace_enabled=multi_workspace_enabled,
            event_types=("connector.crawl.requested",),
            expected=claim,
        )
        if (
            work is None
            or work.stage.status != "running"
            or work.state is None
            or work.state.lease_run_id != run_id
            or work.stage.lease_expires_at is None
            or work.stage.lease_expires_at <= datetime.now(UTC)
            or work.state.lease_expires_at is None
            or work.state.lease_expires_at <= datetime.now(UTC)
        ):
            raise ValueError("Crawl original claim changed during HTTP")
        received_at = datetime.now(UTC)
        observations = []
        for record, data, record_hash in zip(
            records, canonical_records, record_hashes, strict=True
        ):
            observed_at = record.observed_at
            if observed_at.tzinfo is None:
                raise ValueError("Browser collector returned a naive observation time")
            observations.append(
                {
                    "source_id": work.source.id,
                    "batch_id": work.batch.id,
                    "provider_id": data["provider_id"],
                    "record_hash": record_hash,
                    "payload": data,
                    "observed_at": observed_at.astimezone(UTC),
                    "received_at": received_at,
                    "collected_at": collected_at,
                }
            )
        await session.execute(
            pg_insert(SourceObservation)
            .values(observations)
            .on_conflict_do_nothing(constraint="uq_source_observations_batch_record_observed")
        )
        work.batch.payload_hash = payload_hash
        cursor_after = max(record.observed_at.astimezone(UTC) for record in records).isoformat()
        if work.state.cursor:
            try:
                previous = datetime.fromisoformat(work.state.cursor)
                if previous.tzinfo is not None and previous > datetime.fromisoformat(cursor_after):
                    cursor_after = work.state.cursor
            except ValueError:
                # A non-ISO legacy cursor is not comparable: the freshly computed cursor replaces it.
                logger.debug(
                    "Stored crawl cursor is not an ISO timestamp; replacing it", exc_info=True
                )
        work.state.cursor = cursor_after
        await session.flush()
        normalize_stage = await ingestion_api.schedule_normalization(
            session,
            work.run,
            work.batch,
            work.source.generation,
            received_at,
            scope=work.scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        drafts = (
            ()
            if normalize_stage is None
            else (
                make_ingestion_change(
                    work.source.id,
                    work.run.id,
                    work.run.status,
                    normalize_stage.stage_key,
                    normalize_stage.status,
                    scope=work.scope,
                ),
            )
        )
        await _commit_ingestion_change(
            session,
            work.run,
            work.stage,
            drafts,
            scope=work.scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=work.access_fence,
        )
