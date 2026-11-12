"""Collection runtime core: run state, send gate, attempt loading, generic/walk/native acceptance and selection.

Private part of :mod:`modules.connectors.collection` (a pure move; that module re-exports every name).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError, error_status
from core.heavy_work import to_thread_joined
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import provider_options, provider_terms, provisioning, quota, scheduler
from modules.connectors import public as connectors
from modules.connectors.collection_schemas import CollectionAdmissionRead, CollectionRequestRef
from modules.connectors.models import ConnectorCollectionRequest, ConnectorProvisioning
from modules.connectors.provider_specs import (
    INCREMENTAL_PROVIDERS,
    LOCAL_SERVICE_PROVIDERS,
    get_provider_spec,
    get_quota_policies,
    get_realtime_tier,
    min_interval_minutes,
)
from modules.connectors.providers import rest
from modules.connectors.providers.json_feed import JSON_FEEDS
from modules.connectors.providers.keyed import KEYED
from modules.connectors.selection import select_incremental, selection_filters
from modules.ingestion import public as ingestion
from modules.ingestion.public import (
    CollectionState,
    CollectionStateUpdate,
    IngestionRecord,
    NativeCollectionBatch,
    ReceiveBatch,
)
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence

# A header-time recheck is skipped when the same send passed authorisation this recently.
_RECHECK_SKIP_SECONDS = 2.0
RENEW_EVERY_SECONDS = 20
PAYLOAD_RATE_LIMIT_BACKOFF = timedelta(
    minutes=15
)  # body-level throttling carries no Retry-After; coarse until observed
PURE_MAX_BYTES = {
    "ecb": 256 * 1024,
    "cisa_kev": 4 * 1024 * 1024,
}  # every other P3 body is capped at macro.MAX_BODY_BYTES
PURE_PROVIDERS = (  # coingecko needs a key slot
    "world_bank",
    "frankfurter",
    "ecb",
    "binance",
    "alternative_me",
    "usgs",
    "coinpaprika",
    "nasa_eonet",
    "cisa_kev",
    *JSON_FEEDS,  # table-driven JSON list/event feeds (providers/json_feed.py)
    "noaa_kp",
    "open_er_api",
    "oecd_cli",
    "iss_position",
    "disease_sh",
    "ioda",
    "nvd",  # bespoke keyless mappers
    "vn_stock",
)
KEYED_PROVIDERS = {
    provider: row.placement for provider, row in KEYED.items()
}  # provider -> stored credential header_name (header | query:<p> | path:<t>)
SIMPLE_NATIVE = frozenset(
    {
        "youtube",
        "arxiv",
        "huggingface",
        "bluesky",
        "reddit",
        "mastodon",
        "x",
        "github_releases",
        "alpha_vantage",
        "open_meteo",
        "google_mail",
        "google_calendar",
        "google_drive",
        "health",
        "home_assistant",
        "notion",
        "slack",
    }
)
# Both HTTP-shaped error families a collaborator (Sources, terms, access admission) may raise.
_HTTP_ERRORS = (HTTPException, DomainError)
HEADER_AUTH_PROVIDERS = LOCAL_SERVICE_PROVIDERS
REVISIONED_REST_PROVIDERS = frozenset({"notion", "slack", "mastodon", "x"})
REFUSED_HERE: frozenset[str] = (
    frozenset()
)  # providers that must never run here (GitHub/Telegram are native now)
Snapshot = tuple[int, int, int]


def snapshot_matches(captured: Snapshot, current: Snapshot) -> bool:
    """Compare source generation, connector revision and execution-backend revision.

    Comparison alone never grants authorization: it is used under locked fresh source and
    provisioning rows, in addition to the request/slot token and grant checks.
    """
    return captured == current


class CollectionFenceLost(Exception):  # control-flow signal
    """This attempt may no longer send or publish. ``cancel_as`` cancels the request; None leaves recovery to C2."""

    def __init__(self, reason: str, cancel_as: str | None = None) -> None:
        """Store the machine reason and the optional cancellation outcome name."""
        super().__init__(reason)
        self.reason, self.cancel_as = reason, cancel_as


class TermsIneligible(Exception):  # control-flow signal
    """Provider terms are no longer satisfied; the source is gated until the owner acts."""


class CredentialMissing(Exception):  # control-flow signal
    """A key-bearing provider has no usable owner-entered key; nothing was sent. Carries no secret."""


@dataclass(frozen=True)
class Attempt:
    """Everything an attempt needs, reloaded from the durable request rather than job arguments."""

    ref: CollectionRequestRef
    attempt: int
    scope: InternalJobScope
    multi: bool
    source: ConnectorSource
    access_fence: AccessFence
    source_fence: SourceFence
    terms_revision: int | None
    connector_revision: int
    authenticated: bool
    credential_revision: int | None = None  # captured at admission; fences key-bearing providers


@dataclass
class Run:
    """Per-attempt execution state shared by adapters."""

    ctx: dict[str, object]
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    attempt: Attempt
    gate: "SendGate"
    next_eligible_at: datetime | None = None
    extras: dict[str, Any] = field(default_factory=dict)


Adapter = Callable[[Run], Awaitable[None]]
logger = logging.getLogger(__name__)

ADAPTERS: dict[
    str, Adapter
] = {}  # provider id -> explicit adapter (HN and later free providers register here)


class SendGate:
    """Authorize and debit every physical send; commit before the wire, never refund.

    Providers call it before a send and again from an httpx trace at header time. The first call
    of a pair revalidates and debits (new ``send_sequence`` => new ledger row); the paired second
    call only revalidates. Providers abort the whole collection on any send failure, and a gate
    lives for one attempt, so an unpaired armed state cannot leak into a later send.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        attempt: Attempt,
        *,
        settings: Settings,
        deadline: float,
        quota_provider: str | None,
        fingerprint: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        """Bind the per-attempt gate to its session factory, attempt, deadline, quota provider and fingerprint callback."""
        self._factory, self._a, self._settings = factory, attempt, settings
        self._deadline, self._quota_provider, self._fingerprint = (
            deadline,
            quota_provider,
            fingerprint,
        )
        self.sends = 0
        self.captured_operation: UUID | None = None
        self._armed = False
        self._last_authorized: float | None = None

    async def __call__(self, credential_operation_id: UUID | None = None) -> None:
        """Run before one physical send (or at its header-time recheck)."""
        if credential_operation_id is not None:
            if self.captured_operation not in (None, credential_operation_id):
                raise CollectionFenceLost("credential_changed", "revision_changed")
            self.captured_operation = credential_operation_id
        if self._armed:
            self._armed = False
            # The header-time recheck follows the pre-send check of the same send; skip it when that
            # check passed moments ago. Long sends (slow headers) still revalidate, and the heartbeat
            # remains the sole owner of slot renewal across them.
            last = self._last_authorized
            if last is None or time.monotonic() - last >= _RECHECK_SKIP_SECONDS:
                await self._authorize()
            return
        await self._authorize()
        await self._debit()
        self._armed = True

    def bind_credential(self, fingerprint: str) -> None:
        """Key the shared credential budget on a deployment-keyed fingerprint (never the key)."""

        async def bound() -> str:
            """Return the already-computed fingerprint."""
            return fingerprint

        self._fingerprint = bound

    async def fetch_bytes(self, url: str, **kwargs: Any) -> rest.Fetched:
        """Gated, pinned GET for generic adapters (one debited/fenced send per call)."""
        return await rest.fetch_bounded(url, before_send=self, **kwargs)

    async def _authorize(self) -> None:
        """Revalidate the current request, terms and any captured credential revision before a send.

        Keyed presets, local-service tokens, and Notion/Slack bearer credentials must still match
        the admission revision; expired source, permission, terms or credential authority fails closed.
        """
        a = self._a
        if time.monotonic() > self._deadline:
            raise TimeoutError
        async with self._factory() as session:
            if not await scheduler.renew_admission(
                session,
                a.ref.request_id,
                a.ref.admission_token,
                multi_workspace_enabled=a.multi,
            ):
                raise CollectionFenceLost("admission_lost")
            try:
                source = await sources.get_connector_source(
                    session, a.source.id, scope=a.scope, multi_workspace_enabled=a.multi
                )
                if source is None:
                    raise CollectionFenceLost("source_inactive", "source_inactive")
                revision = await provider_terms.require_terms_eligible(session, source)
            except _HTTP_ERRORS as exc:
                raise TermsIneligible from exc
            finally:
                await session.rollback()
            if revision != a.terms_revision:
                raise CollectionFenceLost("terms_changed", "revision_changed")
            if source.provider == "alpha_vantage" and self.captured_operation is not None:
                await self._check_world_credential(session, source)
            if (
                source.provider in KEYED_PROVIDERS
                or source.provider in HEADER_AUTH_PROVIDERS
                or source.provider in REVISIONED_REST_PROVIDERS
            ):
                await self._check_credential_revision(session, source)
        # Stamped only after every check passed; a failed check raises before reaching here.
        self._last_authorized = time.monotonic()

    async def _check_credential_revision(
        self, session: AsyncSession, source: ConnectorSource
    ) -> None:
        """Owner key re-entry bumps ``credential_revision``; any send admitted before it is lost."""
        a = self._a
        try:
            source_fence, row, _ = await provisioning.lock_connector(
                session,
                source.id,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                expected_access_fence=a.access_fence,
            )
            if (
                source_fence is None
                or row is None
                or row.credential_revision != a.credential_revision
            ):
                raise CollectionFenceLost("credential_changed", "revision_changed")
        finally:
            await session.rollback()

    async def _check_world_credential(self, session: AsyncSession, source: ConnectorSource) -> None:
        """Re-lock the connector and revalidate the captured world-credential operation; raises fence loss when it changed."""
        a = self._a
        try:
            source_fence, row, _ = await provisioning.lock_connector(
                session,
                source.id,
                provisioning._ALL_CREDENTIAL_SLOTS,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                expected_access_fence=a.access_fence,
            )
            if (
                source_fence is None
                or row is None
                or not await connectors.validate_world_credential_operation_in_uow(
                    session,
                    source.id,
                    source_generation=source.generation,
                    connector_revision=a.connector_revision,
                    expected_operation_id=self.captured_operation,  # type: ignore[arg-type]
                    scope=a.scope,
                    multi_workspace_enabled=a.multi,
                    access_fence=a.access_fence,
                    source_fence=source_fence,
                )
            ):
                raise CollectionFenceLost("credential_changed", "revision_changed")
        finally:
            await session.rollback()

    async def _debit(self) -> None:
        """Debit one physical send from the provider quota (no-op without a quota provider) and count it."""
        if self._quota_provider is None:
            return
        a = self._a
        fingerprint = await self._fingerprint() if self._fingerprint is not None else None
        self.sends += 1
        async with self._factory() as session:
            try:
                await quota.debit_send(
                    session,
                    provider_id=self._quota_provider,
                    workspace_id=a.scope.workspace_id,
                    request_id=a.ref.request_id,
                    admission_token=a.ref.admission_token,
                    attempt=a.attempt,
                    send_sequence=self.sends,
                    request_target=self._quota_provider,
                    credential=fingerprint,
                )
                await session.commit()  # the debit is durable BEFORE bytes leave the process
            except BaseException:
                await session.rollback()
                raise


def _alpha_fingerprint(run_state: dict[str, Any]) -> Callable[[], Awaitable[str | None]]:
    """Build the cached, deployment-keyed Alpha Vantage key fingerprint callback shared by the gate and quota debit."""

    async def fingerprint() -> str | None:
        """Deployment-keyed fingerprint of the Alpha key so one key shares one budget across workspaces."""
        from modules.connectors.providers.world_data import get_alpha_vantage_key

        if "alpha" in run_state:
            return str(run_state["alpha"])
        a: Attempt = run_state["attempt"]
        async with run_state["factory"]() as session:
            credential = await get_alpha_vantage_key(
                session,
                a.source,
                run_state["settings"],
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                access_fence=a.access_fence,
                source_fence=a.source_fence,
            )
        if credential is None:
            raise CollectionFenceLost("credential_changed", "revision_changed")
        run_state["alpha"] = quota.credential_fingerprint(
            credential[0],
            deployment_key=run_state[
                "settings"
            ].connector_credential_encryption_key.get_secret_value(),
        )
        return str(run_state["alpha"])

    return fingerprint


async def _load_attempt(
    factory: async_sessionmaker[AsyncSession],
    admission: CollectionAdmissionRead,
    multi: bool,
) -> Attempt | None:
    """Reload scope, source and fences from the durable request; None when it is no longer ours."""
    async with factory() as session:
        request = await session.get(ConnectorCollectionRequest, admission.request_id)
        if (
            request is None
            or request.status != "running"
            or request.active_admission_token != admission.admission_token
        ):
            await session.rollback()
            return None
        scope = scheduler._request_scope(request)
        captured: Snapshot = (
            request.source_generation,
            request.connector_revision,
            request.backend_revision,
        )
        terms_revision, access_revision = (
            request.terms_revision,
            request.access_configuration_revision,
        )
        source: ConnectorSource | None = None
        source_fence: SourceFence | None = None
        access_fence: AccessFence | None = None
        row: ConnectorProvisioning | None = None
        denied = "permission_lost"
        for resolve in (1, 2):  # a 409 is re-resolved once, then becomes terminal stale_scope
            try:
                await connectors._connector_access(
                    session, scope=scope, multi_workspace_enabled=multi
                )
                source = await sources.get_connector_source(
                    session, request.source_id, scope=scope, multi_workspace_enabled=multi
                )
                source_fence = await sources.get_source_fence(
                    session, request.source_id, scope=scope, multi_workspace_enabled=multi
                )
                access_fence = await read_access_fence(
                    session, scope=scope, multi_workspace_enabled=multi
                )
                row = (
                    await session.get(ConnectorProvisioning, request.source_id)
                    if source is not None
                    else None
                )
                break
            except _HTTP_ERRORS as exc:
                source = None
                if error_status(exc) == 409 and resolve == 1:
                    await session.rollback()
                    continue
                denied = scheduler.denial_code(cast(Any, exc))
                break
        # Copy every ORM field used below before the rollback: rollback expires the rows and the session
        # closes on exit, so a later attribute read raises DetachedInstanceError (seen as executor_error).
        provisioned = (
            None
            if row is None
            else (
                row.desired_revision,
                row.backend_revision,
                (row.desired_configuration or {}).get("auth_method"),
            )
        )
        credential_revision = request.credential_revision
        await session.rollback()
    if source is None or source_fence is None or access_fence is None or provisioned is None:
        await _settle(factory, admission, outcome="cancelled", error_code=denied)
        return None
    desired_revision, backend_revision, auth_method = provisioned
    current: Snapshot = (source.generation, desired_revision, backend_revision)
    if not snapshot_matches(captured, current) or (
        access_revision is not None and access_revision != access_fence.configuration_revision
    ):
        await _settle(factory, admission, outcome="cancelled", error_code="revision_changed")
        return None
    return Attempt(
        ref=CollectionRequestRef(
            request_id=admission.request_id, admission_token=admission.admission_token
        ),
        attempt=admission.attempt,
        scope=scope,
        multi=multi,
        source=source,
        access_fence=access_fence,
        source_fence=source_fence,
        terms_revision=terms_revision,
        connector_revision=desired_revision,
        authenticated=auth_method not in (None, "none")
        or source.provider in {"google_mail", "google_calendar", "google_drive"},
        credential_revision=credential_revision,
    )


async def _settle(
    factory: async_sessionmaker[AsyncSession],
    admission: CollectionAdmissionRead,
    **kwargs: Any,
) -> bool:
    """Settle the admission in its own short session; returns whether the scheduler accepted the settlement."""
    async with factory() as session:
        return await scheduler.settle_admission(
            session, admission.request_id, admission.admission_token, **kwargs
        )


async def _heartbeat(
    factory: async_sessionmaker[AsyncSession],
    attempt: Attempt,
    main: "asyncio.Task[Any]",
    lost: asyncio.Event,
    interval: float = RENEW_EVERY_SECONDS,
    accepted: Callable[[], bool] = lambda: False,
) -> None:
    """Renew the slot independently of response streaming; a failed renewal stops the attempt.

    Once ``accepted()`` is true the accept transaction has committed and settled the request and slot, so a
    failed renewal is expected and must not cancel the attempt (it would skip the realtime snapshot publish).
    """
    while True:
        await asyncio.sleep(interval)
        async with factory() as session:
            ok = await scheduler.renew_admission(
                session,
                attempt.ref.request_id,
                attempt.ref.admission_token,
                multi_workspace_enabled=attempt.multi,
            )
        if not ok:
            if accepted():
                return
            lost.set()
            main.cancel()
            return


async def _record_rate_limit(run: Run, deadline: datetime) -> None:
    """Persist a provider 429/Retry-After on every applicable window; never shortens one."""
    provider_id = run.attempt.source.provider
    if not provider_id or not get_quota_policies(provider_id):
        return
    fingerprint = await run.gate._fingerprint() if run.gate._fingerprint is not None else None
    async with run.factory() as session:
        await quota.defer_provider(
            session, provider_id=provider_id, until=deadline, credential=fingerprint
        )
        await session.commit()


# ---------------------------------------------------------------- generic adapters


def _records(raw: list[dict[str, Any]]) -> list[IngestionRecord]:
    """Validate raw adapter dicts into ``IngestionRecord`` objects stamped with one shared collection time."""
    collected_at = datetime.now(UTC)
    return [IngestionRecord.model_validate({**item, "collected_at": collected_at}) for item in raw]


async def _publish_realtime_snapshot(
    run: Run,
    records: Sequence[IngestionRecord],
    collected_at: datetime,
) -> None:
    """Publish a bounded hot snapshot to Redis and wake up active SSE streams after ingestion commits.

    Uses a 3x-polling-interval TTL (stale-while-revalidate pattern) and never blocks or fails
    a committed collection if Redis is absent or slow.

    Args:
        run: Active collection run context.
        records: Accepted ingestion records committed in this collection batch.
        collected_at: Timestamp of the accepted collection batch.
    """
    from core.realtime.routes import notify_realtime_wakeup

    a = run.attempt
    notify_realtime_wakeup(a.scope.workspace_id)
    if not records:
        return
    ctx = getattr(run, "ctx", None)
    redis: Any = ctx.get("redis") if isinstance(ctx, dict) else None
    if redis is None:
        return
    with suppress(Exception):
        import json

        spec = get_provider_spec(a.source.provider)
        cfg = getattr(a.source, "configuration", None) or {}
        interval_minutes = max(
            int(
                cfg.get("schedule_interval_minutes")
                or (spec.default_interval_minutes if spec is not None else 15)
            ),
            min_interval_minutes(a.source.provider),
        )
        ttl_seconds = max(180, interval_minutes * 180)
        preview = [
            {
                "provider_id": str(rec.provider_id)[:256],
                "observed_at": rec.observed_at.isoformat(),
                "title": str((rec.metadata or {}).get("title") or "")[:240],
            }
            for rec in sorted(
                run.extras.get("preview") or records, key=lambda r: r.observed_at, reverse=True
            )[:25]
        ]
        payload = json.dumps(
            {
                "source_id": str(a.source.id),
                "source_generation": a.source.generation,
                "provider": a.source.provider,
                "realtime_tier": get_realtime_tier(a.source.provider, cfg),
                "collected_at": collected_at.isoformat(),
                "record_count": len(records),
                "items": preview,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        key = f"connector:snapshot:{a.scope.workspace_id}:{a.source.id}"
        channel = f"realtime:workspace:{a.scope.workspace_id}"
        async with asyncio.timeout(1.0):
            if callable(getattr(redis, "set", None)):
                await redis.set(key, payload, ex=ttl_seconds)
            if callable(getattr(redis, "publish", None)):
                await redis.publish(channel, str(a.source.id))


async def _accept_generic(
    run: Run,
    records: list[dict[str, Any]],
    cursor_before: str | None,
    cursor_after: str | None,
    update: CollectionStateUpdate | None = None,
) -> None:
    """Hand a generic collection to the ingestion owner; empty means a no-change settlement."""
    a = run.attempt
    async with run.factory() as session:
        await lock_access_fence(
            session, scope=a.scope, expected=a.access_fence, multi_workspace_enabled=a.multi
        )
        parsed = _records(records) if records else []
        if parsed:
            # Re-polled items already stored add only garbage rows; an all-seen poll settles as no-change.
            parsed = await ingestion.drop_seen_records(
                session,
                source_id=a.source.id,
                source_generation=a.source.generation,
                provider=a.source.provider,
                records=parsed,
            )
            if not parsed and cursor_after != cursor_before:
                # The no-change path carries no batch cursor, so hand it the cursor this walk earned.
                update = replace(update or CollectionStateUpdate(), cursor_after=cursor_after)
        if not parsed:
            await ingestion.accept_collection_no_changes(
                session,
                source_id=a.source.id,
                source_generation=a.source.generation,
                connector_revision=a.connector_revision,
                request_ref=a.ref,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                state_update=update,
                expected_cursor=cursor_before,
            )
            return
        batch = ReceiveBatch(
            source_id=a.source.id,
            source_generation=a.source.generation,
            connector_revision=a.connector_revision,
            batch_key=f"connector-request:{a.ref.request_id}",
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            records=parsed,
        )
        await ingestion.receive_connector_batch(
            session,
            batch,
            None,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            request_ref=a.ref,
            state_update=update,
        )
    # Commit is done; flag it before publishing so the heartbeat cannot cancel the snapshot publish.
    run.extras["accepted"] = True
    await _publish_realtime_snapshot(run, parsed, datetime.now(UTC))


def _generic_update(
    update: CollectionStateUpdate | None,
    selection: dict[str, Any] | None,
    taken: int,
) -> CollectionStateUpdate | None:
    """Attach a generic path's run stats to its state update; no-op when the path recorded none.

    ``selection`` carries ``backlog`` (records left unread/unaccepted), ``failed_calls`` and ``truncated``. The
    stats ride the receipt only, never the records, so record hashes and coverage are unchanged. ``new`` is the
    count handed to ingestion before its seen-key drop.
    """
    if not selection:
        return update
    stats = {
        "new": taken,
        "backlog": selection.get("backlog", 0),
        "failed_calls": selection.get("failed_calls", 0),
    }
    if selection.get("truncated"):
        stats["truncated"] = True
    return replace(update or CollectionStateUpdate(), stats=stats)


async def _state(run: Run) -> CollectionState:
    """Read the durable collection state (cursor, coverage) in a read-only session that is rolled back."""
    a = run.attempt
    async with run.factory() as session:
        state = await ingestion.get_collection_state(
            session, a.source.id, scope=a.scope, multi_workspace_enabled=a.multi
        )
        await session.rollback()
    return state


def _conditional(state: CollectionState, revision: int) -> dict[str, str] | None:
    """Validators for a fresh walk; only trusted when stored under the same connector revision."""
    if state.validators_revision != revision:
        return None
    headers = {}
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified
    return headers or None


@dataclass
class Walk:
    """Outcome of one bounded walk, independent of REST/RSS."""

    records: list[dict[str, Any]]
    next_url: str | None  # set => partial: a cap stopped the walk at a page boundary
    max_time: str | None
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


async def _accept_walk(
    run: Run, state: CollectionState, walk: Walk, resume: rest.Checkpoint | None
) -> None:
    """Commit a walk: complete => cursor + validators advance; partial => checkpoint only.

    The checkpoint, validators, cursor, batch, receipt, request outcome and slot release share the
    ingestion owner's single commit, so a crash replays from the previous checkpoint exactly.
    """
    cursor = state.cursor
    if walk.not_modified:  # 304: cursor, validators and last-good observations stay untouched
        await _accept_generic(
            run, [], cursor, cursor, CollectionStateUpdate(continuation_state=None)
        )
        return
    if walk.next_url is None:
        cursor_after = walk.max_time or cursor
        update = CollectionStateUpdate(
            continuation_state=None,
            update_validators=True,
            etag=walk.etag,
            last_modified=walk.last_modified,
            cursor_after=cursor_after if cursor_after != cursor else None,
        )
        await _accept_generic(
            run,
            walk.records,
            cursor,
            cursor_after,
            _generic_update(update, getattr(run, "extras", {}).get("selection"), len(walk.records)),
        )
        return
    checkpoint = rest.Checkpoint(
        url=walk.next_url,
        revision=run.attempt.connector_revision,
        cursor=cursor,
        max_time=walk.max_time,
        etag=walk.etag,
        last_modified=walk.last_modified,
        ids=[rest.id_hash(str(r["provider_id"])) for r in walk.records]
        or (resume.ids if resume else []),
    )
    await _accept_generic(
        run,
        walk.records,
        cursor,
        cursor,
        _generic_update(
            CollectionStateUpdate(continuation_state=checkpoint.encode(), coverage="partial"),
            getattr(run, "extras", {}).get("selection"),
            len(walk.records),
        ),
    )


async def _rest_secret(run: Run) -> tuple[str, str]:
    """Load the owner-entered header credential for this exact generation/revision, or require re-entry."""
    from modules.connectors.credentials import CredentialEncryptionUnavailable, decrypt_rest_secret
    from modules.connectors.models import ConnectorRestCredential

    a = run.attempt
    async with run.factory() as session:
        row = await session.get(ConnectorRestCredential, a.source.id)
        # Copy the columns before rollback: rollback expires the ORM row and a lazy reload outside the
        # session raises (surfacing as executor_error instead of the credential outcome).
        credential = (
            None
            if row is None
            else SimpleNamespace(
                state=row.state,
                encrypted_secret=row.encrypted_secret,
                source_generation=row.source_generation,
                configuration_revision=row.configuration_revision,
                operation_id=row.operation_id,
                header_name=row.header_name,
            )
        )
        await session.rollback()
    if (
        credential is None
        or credential.state != "ready"
        or credential.encrypted_secret is None
        or credential.source_generation != a.source.generation
        or credential.configuration_revision != a.connector_revision
    ):
        raise rest.ProviderHttpError(401)  # owner re-entry required
    try:
        secret = decrypt_rest_secret(
            run.settings.connector_credential_encryption_key.get_secret_value(),
            credential.encrypted_secret,
            source_id=a.source.id,
            operation_id=credential.operation_id,
            source_generation=credential.source_generation,
            configuration_revision=credential.configuration_revision,
            header_name=credential.header_name,
        )
    except CredentialEncryptionUnavailable as exc:
        raise rest.ProviderHttpError(401) from exc
    return credential.header_name, secret


async def _optional_rest_secret(run: Run) -> tuple[str, str] | None:
    """Read an optional REST credential only when a current encrypted binding exists.

    Mastodon permits public reads without a token. A present but stale, revoked, or mismatched
    credential is an owner-action error and must never silently downgrade to anonymous access.
    """
    from modules.connectors.models import ConnectorRestCredential

    a = run.attempt
    async with run.factory() as session:
        credential = await session.get(ConnectorRestCredential, a.source.id)
        snapshot = (
            None
            if credential is None
            else (
                credential.state,
                credential.source_generation,
                credential.configuration_revision,
            )
        )
        await session.rollback()
    if snapshot is None:
        return None
    if snapshot != ("ready", a.source.generation, a.connector_revision):
        raise CredentialMissing
    return await _rest_secret(run)


def _with_header(
    send: Callable[..., Awaitable[rest.Fetched]],
    header: str,
    secret: str,
) -> Callable[..., Awaitable[rest.Fetched]]:
    """Wrap a gated send so the owner-entered header rides every same-origin request."""

    async def fetch(url: str, headers: dict[str, str] | None = None, **kwargs: Any) -> rest.Fetched:
        """Call the wrapped sender with the owner-supplied header merged over the caller's headers."""
        return await send(url, headers={**(headers or {}), header: secret}, **kwargs)

    return fetch


async def _lease(run: Run) -> Any:
    """Reserve the native source lease before any provider send."""
    a = run.attempt
    async with run.factory() as session:
        lease = await ingestion.acquire_connector_collection(
            session,
            source_id=a.source.id,
            source_generation=a.source.generation,
            connector_revision=a.connector_revision,
            collector_token=None,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            request_ref=a.ref,
        )
    run.extras["lease"] = lease
    return lease


async def _accept_native(
    run: Run,
    lease: Any,
    records: Sequence[IngestionRecord],
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"],
    collected_at: datetime,
    state_update: CollectionStateUpdate | None = None,
    provider_credential_revision: int | None = None,
    cursor_after: str | None = None,
    reddit_binding: tuple[UUID, int] | None = None,
) -> None:
    """Hand a leased native page and any required provider-credential proof to ingestion.

    Reddit's OAuth operation and token revision travel with the page so ingestion can reject
    a grant replacement before committing its receipt or continuation cursor. When ``_select`` ran for this
    attempt, its run stats ride the state update to the acceptance receipt (never the records).
    """
    a, gate = run.attempt, run.gate
    if a.source.provider in SOCIAL_WALK_PROVIDERS and "selection" not in run.extras:
        # Social adapters mark the page ``truncated`` exactly while a backlog walk still has a page to continue.
        run.extras["selection"] = {"backlog": None, "caught_up": coverage != "truncated"}
    stats = _run_stats(run, records)
    if stats is not None:
        state_update = replace(state_update or CollectionStateUpdate(), stats=stats)
    if (fingerprint := run.extras.get("native_fp")) is not None:
        # Same transaction as the cursor: the fingerprint of the native query params the cursor belongs to.
        state_update = replace(
            state_update or CollectionStateUpdate(), continuation_state=fingerprint
        )
    batch = NativeCollectionBatch(
        source_id=a.source.id,
        source_generation=a.source.generation,
        connector_revision=a.connector_revision,
        lease_token=lease.token,
        cursor_before=lease.cursor_before,
        cursor_after=cursor_after if cursor_after is not None else lease.cursor_before,
        records=list(records),
        telegram_deliveries=(),
        telegram_raw_deliveries=(),
        coverage=coverage,
        github_segment=None,
        collected_at=collected_at,
        provider_credential_revision=provider_credential_revision,
        reddit_operation_id=reddit_binding[0] if reddit_binding is not None else None,
        reddit_token_revision=reddit_binding[1] if reddit_binding is not None else None,
    )
    async with run.factory() as session:
        await ingestion.accept_native_collection(
            session,
            batch,
            collector_token=None,
            lease=lease,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            expected_native_operation_id=None,
            expected_world_credential_operation_id=gate.captured_operation
            if a.source.provider == "alpha_vantage"
            else None,
            request_ref=a.ref,
            state_update=state_update,
            provider_credential_revision=provider_credential_revision,
        )
    run.extras["accepted"] = True
    await _publish_realtime_snapshot(run, records, collected_at)


class _Unmetered:
    """Redis stand-in: PostgreSQL ledger debits are the quota authority; Redis may only pre-reject."""

    async def eval(self, *_args: object) -> int:
        """Stand-in for the Redis ``eval`` used by quota scripts: always reports one unit granted (unmetered)."""
        return 1


# ---------------------------------------------------------------- P2/P3 free providers (fixed endpoints)


def _payload(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a pure mapper and translate ``ProviderPayloadError.kind`` into executor failure signals."""
    from modules.connectors.providers.macro import ProviderPayloadError

    try:
        return fn(*args)
    except ProviderPayloadError as exc:
        if exc.kind == "rate_limited":
            raise connectors.ProviderRateLimited(
                datetime.now(UTC) + PAYLOAD_RATE_LIMIT_BACKOFF
            ) from exc
        if exc.kind == "incomplete":
            raise rest.CollectionIncomplete from exc
        if exc.kind in {"credential", "entitlement"}:
            raise rest.ProviderHttpError(401) from exc
        raise  # "invalid": ValueError -> provider_response_invalid


async def _payload_async(fn: Callable[..., Any], *args: Any) -> Any:
    """Run ``_payload`` (decode + map of a provider body) off the event loop.

    Same result and same translated exceptions as the inline call; the thread is joined on
    cancellation so the collection lease/slot is never released under live host work.
    """
    return await to_thread_joined(_payload, fn, *args)


def _coverage(records: Sequence[IngestionRecord]) -> Literal["returned_snapshot", "truncated"]:
    """Page coverage must equal the records' coverage (ingress rejects a mismatch)."""
    return (
        "truncated"
        if any(r.metadata["provider_record"]["coverage"] == "truncated" for r in records)
        else "returned_snapshot"
    )


async def _get_body(
    run: Run,
    request: Any,
    *,
    max_bytes: int,
    timeout: float | None = None,  # noqa: ASYNC109  # forwarded to the HTTP client request timeout
) -> rest.Fetched:
    """One gated send (terms + quota + fences before the wire); no redirects, bounded bytes."""
    extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    if getattr(request, "body", None) is not None:  # read-only POST search (VietnamWorks)
        extra["json_body"] = request.body
    return await run.gate.fetch_bytes(
        request.url, headers=request.headers or None, max_bytes=max_bytes, **extra
    )


def _require_body(fetched: rest.Fetched) -> bytes:
    """Return the page body, treating an unconditional-GET 304 (no body) as an HTTP 304 provider error."""
    if fetched.body is None:  # unconditional GET answered 304: not a usable page
        raise rest.ProviderHttpError(304)
    return fetched.body


SOCIAL_WALK_PROVIDERS = frozenset(
    {"bluesky", "reddit", "mastodon", "x"}
)  # catch-up walk (providers/social_walk.py)
DEDUP_ONLY_PROVIDERS = frozenset(
    {"hn_top", "google_news", "vietnamworks_jobs", "itviec_jobs"}
)  # late-surfacing feeds: a cursor floor would drop items


def _run_stats(run: Run, taken: Sequence[IngestionRecord]) -> dict[str, Any] | None:
    """Receipt telemetry for this attempt, or ``None`` when no incremental selection ran.

    Built from ``run.extras["selection"]`` (set by ``_select``): ``new`` is the accepted count, ``backlog`` the
    records left for the next sync, ``gap`` the missed window ``[from, to]`` and ``duplicates`` the already-seen
    count; ``failed_sections`` lists section slugs whose fetch failed. Small by construction (<= 6 slugs).
    """
    selection = run.extras.get("selection")
    if not selection:
        return None
    gap = selection.get("gap")
    stats: dict[str, Any] = {
        "new": len(taken),
        "backlog": selection.get("backlog", 0),
        "gap": list(gap) if gap else None,
        "duplicates": selection.get("duplicates", 0),
        "failed_sections": list(selection.get("failed_sections", ())),
    }
    if (
        "caught_up" in selection
    ):  # social catch-up walks: backlog size is unknown (None), only the walk state is
        stats["caught_up"] = selection["caught_up"]
    return stats


async def _resume_cursor(run: Run, lease: Any) -> str | None:
    """Return the cursor this run starts from, resetting it when the native query parameters changed.

    Only providers with native query options (see ``provider_options.native_fingerprint``) are touched. The
    fingerprint of the parameters in force is stored beside the cursor (``continuation_state``, written with the
    accepted batch). A stored fingerprint that differs from the current one (``None`` counts as the default
    parameters, which is what sources saved before these options used) means the cursor belongs to another
    query, so the run restarts from "now minus the provider's default window" instead; older items matching
    only the new query are intentionally not backfilled. The reset is applied through this run's own accepted
    cursor (``run.extras["resume_cursor"]``), never by touching a live lease, so the CAS on ``cursor_before``
    still guards it. Providers without native options return the stored cursor without any state read.
    """
    source = run.attempt.source
    provider, cfg = str(source.provider), source.configuration or {}
    fingerprint = provider_options.native_fingerprint(provider, cfg)
    cursor: str | None = lease.cursor_before
    if fingerprint is None:
        return cursor
    run.extras["native_fp"] = fingerprint
    if cursor is None:
        return None
    stored = getattr(await _state(run), "continuation_state", None)
    if (stored or provider_options.native_fingerprint(provider, {})) == fingerprint:
        return cursor
    reset = (datetime.now(UTC) - provider_options.default_window(provider, cfg)).isoformat()
    run.extras["resume_cursor"] = reset
    return reset


async def _select(
    run: Run,
    lease: Any,
    records: Sequence[IngestionRecord],
    collected_at: datetime,
) -> tuple[list[IngestionRecord], str | None]:
    """Choose which mapped records to accept and the cursor to commit with them.

    Providers outside ``INCREMENTAL_PROVIDERS`` pass through as ``(records, None)``. Otherwise the stored
    cursor, the source's selection options and a read-only ``seen_record_keys`` lookup (done BEFORE the cap,
    so a run of already-seen items cannot stall the cap) feed ``select_incremental``. Backlog, gap and
    duplicate counts go to ``run.extras["selection"]`` only - never into record metadata, which is hashed.
    ``hn_top`` and ``google_news`` are ranking/aggregator feeds that surface late items, so they are
    dedup-only: no cursor floor and the stored cursor is left unchanged (``None``). The full mapped list is
    kept in ``run.extras["preview"]`` so the realtime preview shows the newest items, not the capped batch.
    The lookup session is rolled back; it writes nothing.
    """
    source = run.attempt.source
    if source.provider not in INCREMENTAL_PROVIDERS:
        return list(records), None
    run.extras["preview"] = list(records)
    dedup_only = source.provider in DEDUP_ONLY_PROVIDERS
    async with run.factory() as session:
        seen_keys = await ingestion.seen_record_keys(
            session,
            source_id=source.id,
            source_generation=source.generation,
            provider=source.provider,
            keys=[ingestion.native_record_key(r) for r in records],
        )
        await session.rollback()
    chosen = select_incremental(
        records,
        cursor=None if dedup_only else run.extras.get("resume_cursor", lease.cursor_before),
        seen=lambda r: ingestion.native_record_key(r) in seen_keys,
        filters=selection_filters(source.configuration or {}),
        now=collected_at,
    )
    run.extras["selection"] = {
        "backlog": chosen.backlog,
        "gap": chosen.gap,
        "duplicates": chosen.duplicates,
    }
    return list(chosen.taken), None if dedup_only else chosen.cursor_after
