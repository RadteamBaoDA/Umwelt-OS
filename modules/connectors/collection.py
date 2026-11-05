"""Shared native collection service: one executor for admitted requests (C3).

``execute_collection`` is the executor the C2 worker job calls after it admitted a request (slot
claimed, attempt counted, committed). It reloads authority from the durable request, runs the
provider adapter with a per-physical-send gate, and hands the result to the ingestion owner,
which commits batch, cursor, outbox, request outcome and slot release in ONE transaction.
Nothing here wraps a commit-owning ingestion API and assumes outer atomicity.

Boundaries kept deliberately narrow:
- GitHub (OAuth grant/hint proofs) and Telegram (verified bot/update semantics) reuse their proven
  owner code (``github.adapter/sync``, ``providers.telegram``) behind the same lease/send/record flow.
- Static web and MCP run natively; browser rendering stays on crawl.py.
- Providers added by other slices (HN, macro, crypto, disasters) register in ``ADAPTERS``.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any, Literal, cast
from urllib.parse import urldefrag, urljoin
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError, error_status, is_http_error
from core.heavy_work import to_thread_joined
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import provider_options, provider_terms, provisioning, quota, scheduler
from modules.connectors import public as connectors
from modules.connectors.catalog import get_catalog_entry
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
from modules.connectors.providers import news as _news
from modules.connectors.providers import rest
from modules.connectors.providers.json_feed import JSON_FEEDS
from modules.connectors.providers.keyed import KEYED, KEYED_MAX_BYTES
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

    async def __call__(self, credential_operation_id: UUID | None = None) -> None:
        """Run before one physical send (or at its header-time recheck)."""
        if credential_operation_id is not None:
            if self.captured_operation not in (None, credential_operation_id):
                raise CollectionFenceLost("credential_changed", "revision_changed")
            self.captured_operation = credential_operation_id
        if self._armed:
            self._armed = False
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
) -> None:
    """Renew the slot independently of response streaming; a failed renewal stops the attempt."""
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


async def _run_rest(run: Run) -> None:
    """Collect one generic REST source: gated fetch (optional owner header), checkpoint resume, atomic receipt."""
    from modules.connectors.registry import configuration

    a = run.attempt
    fetch: Callable[..., Awaitable[rest.Fetched]] = run.gate.fetch_bytes
    if a.authenticated:
        fetch = _with_header(run.gate.fetch_bytes, *await _rest_secret(run))
    config = configuration(a.source)
    state = await _state(run)
    resume = rest.Checkpoint.decode(
        state.continuation_state,
        revision=a.connector_revision,
        cursor=state.cursor,
        origin_url=str(config.url),
    )
    collected = await rest.collect_rest(
        config,
        state.cursor,
        fetch=fetch,
        resume=resume,
        conditional=None if resume else _conditional(state, a.connector_revision),
    )
    await _accept_walk(
        run,
        state,
        Walk(
            collected.records,
            collected.continuation_url,
            collected.max_time,
            collected.etag,
            collected.last_modified,
            collected.not_modified,
        ),
        resume,
    )


class _NotModified(Exception):  # control-flow signal for HTTP 304
    """The first feed page answered 304."""


async def _run_rss(run: Run) -> None:
    """Generic RSS/Atom collection: bounded walk, checkpoint resume, then a walk commit.

    A page with more than the 500-record cap degrades (cap taken, run stats flag ``truncated``) rather than failing.
    """
    from modules.connectors.n8n import read_rss
    from modules.connectors.registry import configuration

    a = run.attempt
    config = configuration(a.source)
    state = await _state(run)
    resume = rest.Checkpoint.decode(
        state.continuation_state,
        revision=a.connector_revision,
        cursor=state.cursor,
        origin_url=None,
    )
    conditional = None if resume else _conditional(state, a.connector_revision)
    stats: dict[str, Any] = {}
    first: dict[str, Any] = {"pending": True}

    async def fetch(url: str) -> bytes:
        """Fetch one feed page, adding conditional headers on the first page and remembering its validators; 304 raises ``_NotModified``."""
        headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}
        is_first, first["pending"] = first["pending"], False
        if is_first and conditional:
            headers.update(conditional)
        fetched = await run.gate.fetch_bytes(url, headers=headers)
        if is_first and resume is None:
            first["etag"], first["lm"] = fetched.etag, fetched.last_modified
        if fetched.body is None:
            raise _NotModified
        return fetched.body

    try:
        result = await read_rss(
            resume.url if resume else str(config.feed_url), state.cursor, fetch=fetch, stats=stats
        )
    except _NotModified:
        await _accept_walk(run, state, Walk([], None, None, None, None, not_modified=True), resume)
        return
    if stats.get("truncated"):
        # Unread items remain inside a page and no exact continuation exists. Failing here would repeat forever on
        # the same oversized page, so take the 500 read and advance the cursor only to the newest taken (max_time
        # below); the rest is reported and drains on later syncs when it is newer than that cursor.
        run.extras["selection"] = {"backlog": 0, "truncated": True}
    skip = set(resume.ids) if resume else set()
    seen: set[str] = set()
    records = []
    for record in result["records"]:  # type: ignore[attr-defined]
        identifier = str(record["provider_id"])
        if rest.id_hash(identifier) in skip or identifier in seen:
            continue
        seen.add(identifier)
        records.append(record)
    newest = str(result["cursor_after"]) if result["cursor_after"] is not None else None
    max_time = rest.later(
        resume.max_time if resume else None, newest if newest != state.cursor else None
    )
    await _accept_walk(
        run,
        state,
        Walk(
            records,
            stats.get("resume_url"),
            max_time,
            resume.etag if resume else first.get("etag"),
            resume.last_modified if resume else first.get("lm"),
        ),
        resume,
    )


# ---------------------------------------------------------------- native web + MCP (generic sources)


class _PageText(HTMLParser):
    """Visible text and anchors of one HTML page (stdlib; no optional crawl extra needed)."""

    def __init__(self) -> None:
        """Initialise the visible-text and link accumulators."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track skipped-element depth for script/style/noscript and collect anchor hrefs."""
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag == "a":
            self.links.extend(v for k, v in attrs if k == "href" and v)

    def handle_endtag(self, tag: str) -> None:
        """Leave a skipped element when its closing tag arrives."""
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        """Append non-blank text that is outside skipped elements."""
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _parse_page(html: str) -> tuple[list[str], list[str]]:
    """Return the visible text parts and anchor hrefs of one HTML page.

    Pure CPU work (stdlib HTMLParser on up to ~10 MB), so callers run it through
    ``to_thread_joined``; it touches no shared state and raises whatever the parser raises.
    """
    page = _PageText()
    page.feed(html)
    return page.parts, page.links


async def _run_web(run: Run) -> None:
    """Collect static web pages through bounded HTTP or queue a fenced browser-worker crawl.

    Browser mode hands its existing scheduler admission to the durable ingestion
    crawl event; the worker later owns the bounded browser request and receipt.
    """
    from modules.connectors.registry import configuration, normalize

    config = configuration(run.attempt.source)
    start = str(config.url)
    state = await _state(run)
    if config.js_render:
        if not run.settings.browser_shared_token.get_secret_value():
            raise rest.ProviderHttpError(503)
        a = run.attempt
        async with run.factory() as session:
            await ingestion.queue_connector_crawl(
                session,
                a.source.id,
                a.source.generation,
                a.connector_revision,
                state.cursor,
                {
                    "url": start,
                    "mode": "playwright",
                    "max_pages": config.max_pages,
                    "max_depth": config.max_depth,
                    "timeout_seconds": config.timeout_seconds,
                },
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                request_ref=a.ref,
            )
        return
    origin = rest._origin(start)
    queue, visited = [(start, 0)], {start}
    records: list[dict[str, Any]] = []
    spent = 0
    while queue and len(records) < config.max_pages:
        url, depth = queue.pop(0)
        fetched = await run.gate.fetch_bytes(
            url,
            headers={"Accept": "text/html,application/xhtml+xml"},
            max_bytes=min(rest.MAX_PAGE_BYTES, rest.MAX_TRANSPORT_BYTES - spent),
            timeout=config.timeout_seconds,
        )
        body = _require_body(fetched)
        spent += len(body)
        # HTMLParser.feed on a multi-MB page would stall the worker loop (heartbeats, heavy-slot renewal).
        parts, links = await to_thread_joined(_parse_page, body.decode("utf-8", errors="replace"))
        records.append(
            normalize(
                {
                    "provider_id": url,
                    "content": "\n".join(parts)[:200_000],
                    "observed_at": datetime.now(UTC).isoformat(),
                    "metadata": {"url": url},
                }
            )
        )
        if depth < config.max_depth:
            for href in links:
                target = urldefrag(urljoin(url, href))[0]
                if target not in visited and len(queue) < 100 and rest._origin(target) == origin:
                    visited.add(target)
                    queue.append((target, depth + 1))
    await _accept_generic(run, records, state.cursor, state.cursor)


async def _run_mcp(run: Run) -> None:
    """MCP collection: each reviewed call is authorized by the gate, then ingested through the fenced receipt."""
    from modules.connectors import mcp as mcp_collection
    from modules.tools import public as tools

    a = run.attempt
    runtime = run.ctx.get("agent_mcp_runtime")
    if runtime is None:
        raise mcp_collection.McpCollectionError("mcp_runtime_unavailable")
    config = mcp_collection.validate(a.source)
    state = await _state(run)
    collected_at = datetime.now(UTC)
    records: list[dict[str, Any]] = []
    failed = 0
    # mcp_collection.authorized swallows exceptions from authorize_extra; remember lost authority here
    # so a fence loss fails closed instead of counting as a retryable per-call failure.
    lost: CollectionFenceLost | TermsIneligible | TimeoutError | None = None

    async def authorized() -> bool:
        """Per-call authorization hook: run the gate, remember the first lost-authority exception and re-raise it."""
        nonlocal lost
        try:
            await (
                run.gate()
            )  # raises CollectionFenceLost / TermsIneligible / TimeoutError; never returns False
        except (CollectionFenceLost, TermsIneligible, TimeoutError) as exc:
            lost = exc
            raise
        return True

    for call in config.calls:
        try:
            read = await tools.read_collection_capability(
                cast(Any, runtime),
                connection_id=config.connection_id,
                grant_id=call.grant_id,
                source_id=a.source.id,
                source_generation=a.source.generation,
                arguments=call.arguments,
                authorize_extra=authorized,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
            )
            records.extend(mcp_collection.normalize(read, call.arguments, collected_at))
        except (CollectionFenceLost, TermsIneligible, TimeoutError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001  # one failing call never blocks the others (as in mcp.collect)
            if lost is not None:
                raise lost from None
            failed += 1
    if failed == len(config.calls):
        raise mcp_collection.McpCollectionError("mcp_collection_failed")
    kept = records[: mcp_collection.MAX_RECORDS]
    # The generic accept path carries no coverage, so the dropped tail and failed calls are reported in receipt stats.
    selection = {"backlog": len(records) - len(kept), "failed_calls": failed}
    await _accept_generic(
        run,
        kept,
        state.cursor,
        "mcp:" + collected_at.isoformat(),
        _generic_update(None, selection, len(kept)),
    )


# ---------------------------------------------------------------- simple native providers


@dataclass(frozen=True)
class NativeCall:
    """Inputs one native provider handler receives from ``_run_native``.

    ``session`` is the open, never-committed session the dispatcher owns for the whole provider
    call (handlers must not commit it); ``lease`` is the reserved source lease.
    """

    run: Run
    lease: Any
    collected_at: datetime
    session: Any


# A handler returns the provider page for the generic accept path, or ``None`` when it already
# fenced credentials and accepted the receipt itself (the dispatcher then returns immediately).
NativeHandler = Callable[[NativeCall], Awaitable[Any | None]]
_NATIVE_HANDLERS: dict[str, NativeHandler] = {}


def register_native_provider(
    providers: Sequence[str] | frozenset[str], handler: NativeHandler
) -> None:
    """Register ``handler`` for every provider name in ``providers`` (new native providers plug in here).

    Later registrations replace earlier ones for the same name; no dispatcher edit is needed.
    """
    for name in providers:
        _NATIVE_HANDLERS[name] = handler


async def _native_feed(call: NativeCall) -> Any:
    """YouTube/arXiv catalogue feeds; returns the page for the generic accept path."""
    from modules.connectors.providers.feed_catalog import collect_provider_feed

    run = call.run
    return await collect_provider_feed(
        run.attempt.source,
        collected_at=call.collected_at,
        session_factory=run.factory,
        before_request=run.gate,
    )


async def _native_huggingface(call: NativeCall) -> Any:
    """Hugging Face model search; returns the page for the generic accept path."""
    from modules.connectors.providers.research import collect_huggingface_models

    return await collect_huggingface_models(
        call.run.attempt.source, collected_at=call.collected_at, before_request=call.run.gate
    )


async def _native_bluesky(call: NativeCall) -> None:
    """Bluesky profile feed; fences the credential and accepts the receipt itself."""
    from modules.connectors.providers.social_feeds import collect_bluesky_profile

    run, gate = call.run, call.run.gate
    config = connectors.ConnectorConfig.model_validate(run.attempt.source.configuration)
    page = await collect_bluesky_profile(
        config.bluesky_actor or "",
        collected_at=call.collected_at,
        cursor=call.lease.cursor_before,
        fetch_bytes=lambda url: gate.fetch_bytes(
            url, max_bytes=5 * 1024 * 1024, headers={"Accept": "application/json"}
        ),
    )
    await _credential_fence(run)
    await _accept_native(
        run,
        call.lease,
        page.records,
        page.coverage,
        call.collected_at,
        cursor_after=page.cursor_after,
    )


async def _native_reddit(call: NativeCall) -> None:
    """Reddit subreddit listing under the exact Reddit grant; accepts the receipt itself."""
    from modules.connectors.providers.social_networks import collect_reddit_subreddit

    run, gate = call.run, call.run.gate
    config = connectors.ConnectorConfig.model_validate(run.attempt.source.configuration)
    token, binding = await _reddit_access_token(run)

    async def before_reddit_send() -> None:
        """Debit admission and recheck the exact Reddit grant before every HTTP send."""
        await gate()
        await _credential_fence(run, reddit=binding)

    page = await collect_reddit_subreddit(
        config.reddit_subreddit or "",
        token,
        cursor=call.lease.cursor_before,
        collected_at=call.collected_at,
        fetch_bytes=lambda url, **kwargs: rest.fetch_bounded(
            url, before_send=before_reddit_send, **kwargs
        ),
    )
    await _credential_fence(run, reddit=binding)
    await _accept_native(
        run,
        call.lease,
        page.records,
        page.coverage,
        call.collected_at,
        cursor_after=page.cursor_after,
        reddit_binding=binding,
    )


async def _native_mastodon_x(call: NativeCall) -> None:
    """Mastodon or X scope read with its optional/required bearer; accepts the receipt itself."""
    from modules.connectors.providers.social_networks import (
        collect_mastodon_scope,
        collect_x_scope,
    )

    run, gate, a = call.run, call.run.gate, call.run.attempt
    config = connectors.ConnectorConfig.model_validate(a.source.configuration)
    if a.source.provider == "mastodon":
        optional_credential = await _optional_rest_secret(run)
        if optional_credential is not None and optional_credential[0] != "Authorization":
            raise CredentialMissing
        social_page = await collect_mastodon_scope(
            config.mastodon_instance_url or "",
            account=config.mastodon_account,
            hashtag=config.mastodon_hashtag,
            access_token=optional_credential[1] if optional_credential is not None else None,
            cursor=call.lease.cursor_before,
            collected_at=call.collected_at,
            fetch_bytes=gate.fetch_bytes,
        )
        credential_revision = a.credential_revision if optional_credential is not None else None
    else:
        try:
            header_name, bearer_token = await _rest_secret(run)
        except rest.ProviderHttpError as exc:
            raise CredentialMissing from exc
        if header_name != "Authorization" or not bearer_token or a.credential_revision is None:
            raise CredentialMissing
        social_page = await collect_x_scope(
            account=config.x_account,
            query=config.x_query,
            access_token=bearer_token,
            cursor=call.lease.cursor_before,
            collected_at=call.collected_at,
            fetch_bytes=gate.fetch_bytes,
        )
        credential_revision = a.credential_revision
    await _credential_fence(run)
    await _accept_native(
        run,
        call.lease,
        social_page.records,
        social_page.coverage,
        call.collected_at,
        provider_credential_revision=credential_revision,
        cursor_after=social_page.cursor_after,
    )


async def _native_google(call: NativeCall) -> None:
    """Gmail, Calendar or Drive read with the encrypted grant; commits the proof receipt itself."""
    from modules.connectors.providers import google_api

    run, gate, a = call.run, call.run.gate, call.run.attempt
    provider = a.source.provider
    token, binding = await _google_access_token(run)

    async def before_google_send() -> None:
        """Debit/revalidate each Google API send against the current grant and lease."""
        await gate()
        await _credential_fence(run, google=binding)

    config = connectors.ConnectorConfig.model_validate(a.source.configuration)
    if provider == "google_mail":
        page = await google_api.collect_gmail(
            token,
            config.google_mail_label_ids or (),
            before_request=before_google_send,
            cursor=call.lease.cursor_before,
            collected_at=call.collected_at,
        )
    elif provider == "google_calendar":
        page = await google_api.collect_calendar(
            token,
            config.google_calendar_ids or (),
            before_request=before_google_send,
            cursor=call.lease.cursor_before,
            collected_at=call.collected_at,
        )
    else:
        page = await google_api.collect_drive(
            token,
            config.google_drive_ids or (),
            before_request=before_google_send,
            cursor=call.lease.cursor_before,
            collected_at=call.collected_at,
        )
    await _credential_fence(run, google=binding)
    await _accept_proof(
        run,
        call.lease,
        records=page.records,
        coverage=page.coverage,
        cursor_after=page.cursor_after,
        collected_at=call.collected_at,
        google_binding=binding,
    )


async def _native_workspace_rest(call: NativeCall) -> None:
    """Notion or Slack read with a revisioned bearer credential; accepts the receipt itself."""
    from modules.connectors.providers.workspace_apps import (
        NotionScope,
        SlackScope,
        collect_notion_pages,
        collect_slack_channel_messages,
    )

    run, gate, a = call.run, call.run.gate, call.run.attempt
    header_name, bearer_token = await _rest_secret(run)
    if header_name != "Authorization" or not bearer_token or a.credential_revision is None:
        raise CredentialMissing
    config = connectors.ConnectorConfig.model_validate(a.source.configuration)
    if a.source.provider == "notion":
        page = await collect_notion_pages(
            bearer_token,
            NotionScope(tuple(config.notion_page_ids or ())),
            fetch_bytes=gate.fetch_bytes,
            collected_at=call.collected_at,
        )
    else:
        page = await collect_slack_channel_messages(
            bearer_token,
            SlackScope(tuple(config.slack_channel_ids or ())),
            fetch_bytes=gate.fetch_bytes,
            collected_at=call.collected_at,
        )
    await _credential_fence(run)
    await _accept_native(
        run,
        call.lease,
        page.records,
        page.coverage,
        call.collected_at,
        provider_credential_revision=a.credential_revision,
    )


def _local_service_failure(reason: str) -> Exception | None:
    """Map a ``LocalServiceError`` reason to the collection error to raise, or ``None`` to re-raise unchanged."""
    if reason == "authorization_rejected":
        return rest.ProviderHttpError(401)
    if reason == "rate_limited":
        return connectors.ProviderRateLimited(datetime.now(UTC) + PAYLOAD_RATE_LIMIT_BACKOFF)
    if reason == "request_failed":
        return TimeoutError("local_service_request_failed")
    if reason.startswith("destination_"):
        return rest.UnsafeDestination(reason)
    if reason.startswith("http_") and reason[5:].isdigit():
        return rest.ProviderHttpError(int(reason[5:]))
    return None


async def _native_local_service(call: NativeCall) -> None:
    """FHIR observations or Home Assistant snapshot against the configured local origin; accepts itself.

    These providers may only reach private/loopback addresses, which is the operator's own LAN. In
    multi-workspace mode any invited account owns a workspace, so a non-operator actor would otherwise
    reach internal services (database, redis, docker gateway); refuse as an unsafe destination.
    """
    from core.auth.dependencies import is_instance_operator_id
    from modules.connectors.providers.local_services import (
        LocalServiceError,
        collect_fhir_observations,
        collect_home_assistant_snapshot,
    )

    run, gate, a = call.run, call.run.gate, call.run.attempt
    provider = a.source.provider
    if not is_instance_operator_id(a.scope.actor_user_id):
        raise rest.UnsafeDestination("destination_not_allowed")
    header_name, bearer_token = await _rest_secret(run)
    if header_name != "Authorization" or not bearer_token:
        raise CredentialMissing

    async def before_local_send() -> None:
        """Recheck schedule admission and source/credential revisions before each local request."""
        await gate()
        await _credential_fence(run)

    config = connectors.ConnectorConfig.model_validate(a.source.configuration)
    try:
        if provider == "health":
            records = await collect_fhir_observations(
                config.fhir_base_url or "",
                config.fhir_patient_id or "",
                bearer_token=bearer_token,
                before_send=before_local_send,
                collected_at=call.collected_at,
            )
        else:
            page = await collect_home_assistant_snapshot(
                config.ha_base_url or "",
                tuple(config.ha_entity_ids or ()),
                bearer_token=bearer_token,
                before_send=before_local_send,
                cursor=call.lease.cursor_before,
                collected_at=call.collected_at,
            )
    except LocalServiceError as exc:
        mapped = _local_service_failure(str(exc))
        if mapped is None:
            raise
        raise mapped from exc
    await _credential_fence(run)
    if provider == "health":
        await _accept_native(run, call.lease, records, "returned_snapshot", call.collected_at)
    else:
        await _accept_native(
            run,
            call.lease,
            page.records,
            page.coverage,
            call.collected_at,
            cursor_after=page.cursor_after,
        )


async def _native_github_releases(call: NativeCall) -> Any:
    """GitHub releases feed; returns the page for the generic accept path."""
    from modules.connectors.providers.social import collect_github_releases

    return await collect_github_releases(
        call.run.attempt.source, collected_at=call.collected_at, before_request=call.run.gate
    )


async def _native_world_data(call: NativeCall) -> Any:
    """Default handler: keyed/free world-data providers; returns the page for the generic accept path."""
    from modules.connectors.providers.world_data import collect_world_data

    run, a = call.run, call.run.attempt
    return await collect_world_data(
        a.source,
        collected_at=call.collected_at,
        settings=run.settings,
        session=call.session,
        redis=_Unmetered(),
        scope=a.scope,
        multi_workspace_enabled=a.multi,
        access_fence=a.access_fence,
        source_fence=a.source_fence,
        before_request=run.gate,
    )


register_native_provider(("youtube", "arxiv"), _native_feed)
register_native_provider(("huggingface",), _native_huggingface)
register_native_provider(("bluesky",), _native_bluesky)
register_native_provider(("reddit",), _native_reddit)
register_native_provider(("mastodon", "x"), _native_mastodon_x)
register_native_provider(("google_mail", "google_calendar", "google_drive"), _native_google)
register_native_provider(("notion", "slack"), _native_workspace_rest)
register_native_provider(HEADER_AUTH_PROVIDERS, _native_local_service)
register_native_provider(("github_releases",), _native_github_releases)


async def _run_native(run: Run) -> None:
    """Collect one registered native provider through a reserved lease and atomic receipt.

    Dispatch is registry-driven (``register_native_provider``): the handler for the source
    provider runs while one session stays open and is never committed. A handler that already
    fenced credentials and accepted its receipt returns ``None``; otherwise the returned page is
    rolled back, checked for provider back-off / Alpha Vantage operation capture and accepted
    through the generic path. Providers without a handler fall to the world-data adapter.
    """
    provider = run.attempt.source.provider
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    gate = run.gate
    handler = _NATIVE_HANDLERS.get(provider or "", _native_world_data)
    async with run.factory() as session:
        page = await handler(NativeCall(run, lease, collected_at, session))
        if page is None:
            return
        await session.rollback()
    if page.next_eligible_at is not None:
        run.next_eligible_at = page.next_eligible_at
        raise connectors.ProviderRateLimited(page.next_eligible_at)
    if provider == "alpha_vantage" and (
        gate.captured_operation is None or page.credential_operation_id != gate.captured_operation
    ):
        raise CollectionFenceLost("credential_changed", "revision_changed")
    await _accept_native(run, lease, page.records, page.coverage, collected_at)


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


async def _run_pure(run: Run) -> None:
    """P3 measurement providers: one fixed request, mapped by ``map_pure_provider_body`` (no world_data for news)."""
    from modules.connectors.providers.macro import MAX_BODY_BYTES
    from modules.connectors.providers.world_data import PURE_ADAPTERS, map_pure_provider_body

    provider = str(run.attempt.source.provider)
    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    cursor = await _resume_cursor(run, lease)
    request = PURE_ADAPTERS[provider].request()
    if (
        url := provider_options.build_url(provider, cfg, cursor=cursor, now=collected_at)
    ) is not None:
        request = replace(
            request, url=url
        )  # owner options / native resume; headers stay the adapter's
    fetched = await _get_body(run, request, max_bytes=PURE_MAX_BYTES.get(provider, MAX_BODY_BYTES))
    records = await _payload_async(
        lambda *a: map_pure_provider_body(*a, **provider_options.map_options(provider, cfg)),
        provider,
        _require_body(fetched),
        collected_at,
    )  # mapper scope follows the requested scope
    records = provider_options.filter_records(
        provider, cfg, records
    )  # region / KEV / NVD / FX / Kp (client)
    records, cursor_after = await _select(run, lease, records, collected_at)
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _run_coinpaprika(run: Run) -> None:
    """CoinPaprika tickers: one gated, debited GET per selected coin (<= 5), spaced past the 1 call/second pilot cap.

    The pacing matters: a quota deferral restarts the whole run, so back-to-back calls inside one second would
    defer forever. Any call failing fails the run (a partial coin set would silently drop a series). The
    ``coin_ids`` order is the curated order, so equal selections send equal request sequences.
    """
    from modules.connectors.providers.crypto import map_coinpaprika
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json
    from modules.connectors.providers.world_data import PURE_ADAPTERS

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    records: list[IngestionRecord] = []
    for index, (coin, url) in enumerate(
        zip(provider_options.selected_coins(cfg), provider_options.coin_urls(cfg), strict=True)
    ):
        if index:
            await asyncio.sleep(provider_options.COINPAPRIKA_GAP_SECONDS)
        fetched = await _get_body(
            run, replace(PURE_ADAPTERS["coinpaprika"].request(), url=url), max_bytes=MAX_BODY_BYTES
        )
        records += await _payload_async(
            lambda body, coin=coin: map_coinpaprika(decode_json(body), collected_at, coin_id=coin),
            _require_body(fetched),
        )
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_vn_stock(run: Run) -> None:
    """VN stocks/indices: one gated, debited GET per non-empty group (stocks, indices), so at most two per run.

    The VNDirect finfo API serves stocks and indices from different endpoints. Any failing call fails the run
    (a partial symbol set would silently drop a series); calls are spaced one second apart to stay polite to an
    unofficial endpoint. Symbol order is the owner's, so equal selections send equal request sequences.
    """
    from modules.connectors.providers import vn_stock
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    records: list[IngestionRecord] = []
    requests = _payload(vn_stock.vn_stock_requests, provider_options.selected_vn_symbols(cfg))
    for index, (group, request) in enumerate(requests):
        if index:
            await asyncio.sleep(1.0)
        fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES)
        records += await _payload_async(
            lambda body, group=group: vn_stock.map_vn_stock(
                decode_json(body), collected_at, symbols=group
            ),
            _require_body(fetched),
        )
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_vietnamworks(run: Run) -> None:
    """VietnamWorks jobs: one gated, debited POST search per owner keyword (<= 5), spaced one second apart.

    Results are ranked by relevance, not time, so the provider is dedup-only (no cursor floor). City, level and
    salary filters are applied while mapping; per-keyword results are merged by job id. Any failing call fails the
    run so a partial keyword set never silently drops a search.
    """
    from modules.connectors.providers import jobs
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json

    cfg: dict[str, Any] = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    batches: list[list[IngestionRecord]] = []
    for index, request in enumerate(
        _payload(jobs.vietnamworks_requests, provider_options.selected_job_keywords(cfg))
    ):
        if index:
            await asyncio.sleep(1.0)
        fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES)
        batches.append(
            await _payload_async(
                lambda body: jobs.map_vietnamworks(
                    decode_json(body),
                    collected_at,
                    cities=cfg.get("job_cities") or (),
                    levels=cfg.get("job_levels") or (),
                    min_salary_usd=cfg.get("job_min_salary_usd"),
                ),
                _require_body(fetched),
            )
        )
    records, cursor_after = await _select(
        run, lease, jobs.merge_keyword_records(batches), collected_at
    )
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _run_itviec(run: Run) -> None:
    """ITviec jobs: <= 3 gated search pages, then <= 10 gated posting pages, every request >= 2 s apart.

    Owner-approved static crawl (docs/connectors/free-sources.md): plain HTTP GET, JSON-LD only. Search pages yield
    vetted posting URLs; only URLs this source has not stored yet are fetched, rotated by a 6-hour slot so
    postings rejected by the salary/level filters (never stored, so never "seen") cannot starve newer ones. A
    posting that is gone (404/410) is skipped; any other failing request fails the run, as for VietnamWorks.
    """
    from modules.connectors.providers import jobs
    from modules.connectors.providers.macro import MAX_BODY_BYTES

    cfg: dict[str, Any] = run.attempt.source.configuration or {}
    source = run.attempt.source
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    searches = _payload(
        jobs.itviec_search_requests,
        provider_options.selected_job_keywords(cfg),
        cfg.get("job_cities") or (),
    )
    urls: dict[str, None] = {}
    first = True

    async def page(request: Any) -> str:
        """Fetch one listing page (spacing requests after the first) and return its decoded text."""
        nonlocal first
        if not first:
            await asyncio.sleep(jobs.ITVIEC_REQUEST_GAP_SECONDS)
        first = False
        return _require_body(await _get_body(run, request, max_bytes=MAX_BODY_BYTES)).decode(
            "utf-8", errors="replace"
        )

    for request in searches:
        urls.update(dict.fromkeys(jobs.parse_itviec_item_list(await page(request))))
    async with run.factory() as session:
        seen = await ingestion.seen_provider_ids(
            session,
            source_id=source.id,
            source_generation=source.generation,
            provider_ids=[f"itviec:{u.rsplit('/', 1)[1]}" for u in urls],
        )
        await session.rollback()
    fresh = [u for u in urls if f"itviec:{u.rsplit('/', 1)[1]}" not in seen]
    if fresh:
        shift = int(collected_at.timestamp() // 21600) % len(fresh)
        fresh = fresh[shift:] + fresh[:shift]
    pages: list[tuple[str, str]] = []
    for url in fresh[: jobs.ITVIEC_MAX_DETAIL_FETCHES]:
        try:
            pages.append((url, await page(jobs.itviec_detail_request(url))))
        except rest.ProviderHttpError as exc:
            if exc.status_code not in {404, 410}:
                raise
    records = await _payload_async(
        lambda: jobs.map_itviec(
            pages,
            collected_at,
            levels=cfg.get("job_levels") or (),
            min_salary_usd=cfg.get("job_min_salary_usd"),
        )
    )
    records, cursor_after = await _select(run, lease, records, collected_at)
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _keyed_secret(run: Run) -> str:
    """Owner key for this exact generation/revision, read at send time; typed ``credential_missing`` if unusable.

    The stored ``header_name`` must equal the provider's fixed placement (header name, ``query:<param>`` or
    ``path:<token>``), so a credential saved for another placement is never replayed here.
    """
    placement = KEYED_PROVIDERS[str(run.attempt.source.provider)]
    try:
        stored_placement, secret = await _rest_secret(run)
    except rest.ProviderHttpError as exc:
        raise CredentialMissing from exc
    if stored_placement != placement:
        raise CredentialMissing
    run.gate.bind_credential(
        quota.credential_fingerprint(
            secret,
            deployment_key=run.settings.connector_credential_encryption_key.get_secret_value(),
        )
    )
    return secret


async def _run_keyed(run: Run) -> None:
    """Keyed free-tier providers: key read first (no lease and no send without it), one gated send.

    Order matters: the credential and the request (which validates key shape and owner scope) are resolved
    before the lease so a missing key or bad scope fails closed without consuming quota or a lease.
    """
    from modules.connectors.providers.macro import decode_json

    provider = str(run.attempt.source.provider)
    row = KEYED[provider]
    cfg = getattr(run.attempt.source, "configuration", None) or {}
    request = _payload(row.build, await _keyed_secret(run), cfg)
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    fetched = await _get_body(run, request, max_bytes=KEYED_MAX_BYTES)
    body = _require_body(fetched)
    payload = body if row.binary else (await _payload_async(decode_json, body))
    records = await _payload_async(row.mapper, payload, collected_at, cfg)
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_coingecko(run: Run) -> None:
    """CoinGecko Demo: the first keyed preset; kept as a named entry point for its executor wiring and tests."""
    await _run_keyed(run)


async def _fetch_sections(
    run: Run,
    provider: str,
    sections: list[tuple[str, str]],
    revision: str,
    collected_at: datetime,
) -> tuple[list[IngestionRecord], list[str]]:
    """GET every selected section unconditionally, merge by stable item id, and report failed slugs.

    Each GET is debited by the gate. Records of the same ``provider_id`` seen in several sections collapse to one
    (lowest ``version``, as ``map_feed`` does for in-feed duplicates); the section slug is never written to a
    record, so the hashed metadata equals a single-section fetch of the same article. A section failing with an
    HTTP/parse/scope error is skipped and reported; fence loss, timeouts and rate limits propagate. When every
    section fails the first error is re-raised, so the run fails exactly as a single feed would.
    """
    merged: dict[str, IngestionRecord] = {}
    failed: list[str] = []
    first_error: Exception | None = None
    for slug, url in sections:
        try:
            fetched = await _get_body(
                run,
                _news.feed_request(provider, None, revision, url),
                max_bytes=_news.MAX_FEED_BYTES,
            )
            result = await asyncio.to_thread(
                _payload,
                _news.map_feed_response,
                provider,
                200,
                _require_body(fetched),
                collected_at,
            )
        except (rest.ProviderHttpError, rest.CollectionIncomplete, ValueError) as exc:
            failed.append(slug)
            first_error = first_error or exc
            continue
        for record in result.records:
            old = merged.get(record.provider_id)
            if old is None or (record.version or "") < (old.version or ""):
                merged[record.provider_id] = record
    if not merged and first_error is not None:
        raise first_error
    return list(merged.values()), failed


async def _run_feed(run: Run) -> None:
    """P2 RSS/Atom presets: replay stored validators (same URL + revision), 304 keeps the last good records.

    A catalog preset with several selected sections fetches each one unconditionally (validators are single-URL),
    merges and dedups them, then runs the usual selection; failed section slugs are reported in the run stats.
    """
    from modules.connectors.providers import news

    a, provider = run.attempt, str(run.attempt.source.provider)
    collected_at, revision = datetime.now(UTC), str(a.connector_revision)
    state = await _state(run)
    sections = _payload(news.section_urls, provider, a.source.configuration)
    url = (
        sections[0][1] if sections else _payload(news.preset_url, provider, a.source.configuration)
    )
    stored = (
        news.FeedValidators(
            url,
            revision,
            state.etag,
            state.last_modified,
        )
        if state.validators_revision == a.connector_revision
        else None
    )
    lease = await _lease(run)
    if len(sections) > 1:
        found, failed = await _fetch_sections(run, provider, sections, revision, collected_at)
        update = None
    else:
        failed = []
        fetched = await _get_body(
            run, news.feed_request(provider, stored, revision, url), max_bytes=news.MAX_FEED_BYTES
        )
        # feedparser-style XML parse of up to MAX_FEED_BYTES is CPU-bound: keep it off the event loop.
        result = await asyncio.to_thread(
            _payload,
            news.map_feed_response,
            provider,
            200 if fetched.body is not None else 304,
            fetched.body or b"",
            collected_at,
        )
        if result.not_modified:
            await _accept_native(run, lease, (), "returned_snapshot", collected_at)
            return
        captured = news.capture_validators(
            provider,
            {"etag": fetched.etag or "", "last-modified": fetched.last_modified or ""},
            revision,
            url,
        )
        update = CollectionStateUpdate(
            update_validators=True,
            etag=captured.etag if captured else None,
            last_modified=captured.last_modified if captured else None,
        )
        found = list(result.records)
    found = provider_options.filter_records(
        provider, a.source.configuration or {}, found
    )  # e.g. advisory level
    records, cursor_after = await _select(run, lease, found, collected_at)
    if "selection" in run.extras:
        run.extras["selection"]["failed_sections"] = failed
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, update, cursor_after=cursor_after
    )


async def _run_hn(run: Run) -> None:
    """Hacker News: one id-list send plus ``max_items`` (default 10, hard cap 30) sequential item sends, each debited."""
    from modules.connectors.providers import news

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    listing = provider_options.build_url("hn_top", cfg, cursor=None, now=collected_at)
    top = _require_body(
        await _get_body(
            run,
            news.hn_top_request(listing) if listing else news.hn_top_request(),
            max_bytes=64 * 1024,
        )
    )
    ids = _payload(news.parse_hn_top_ids, top, provider_options.hn_limit(cfg))
    bodies: dict[int, bytes | None] = {}
    for item_id in ids:
        try:
            bodies[item_id] = (
                await _get_body(run, news.hn_item_request(item_id), max_bytes=64 * 1024)
            ).body
        except rest.ProviderHttpError as exc:
            if exc.status_code != 404:
                raise
            bodies[item_id] = None  # removed item: skipped and counted by the mapper
    result = await _payload_async(
        news.map_hn_stories, ids, bodies, collected_at, provider_options.hn_limit(cfg)
    )
    run.extras["skipped"] = result.skipped
    records, cursor_after = await _select(run, lease, result.records, collected_at)
    await _accept_native(
        run, lease, records, "returned_snapshot", collected_at, cursor_after=cursor_after
    )


async def _run_gdelt(run: Run) -> None:
    """Experimental GDELT: hard 8 s timeout; on timeout nothing is accepted so the last good records stay."""
    from modules.connectors.providers.gdelt import GDELT_TIMEOUT_SECONDS, gdelt_request
    from modules.connectors.providers.macro import MAX_BODY_BYTES
    from modules.connectors.providers.news import map_news_body

    cfg = run.attempt.source.configuration or {}
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    cursor = await _resume_cursor(run, lease)
    request = gdelt_request()
    if (
        url := provider_options.build_url("gdelt_economy", cfg, cursor=cursor, now=collected_at)
    ) is not None:
        request = replace(request, url=url)
    fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES, timeout=GDELT_TIMEOUT_SECONDS)
    records = await _payload_async(
        map_news_body, "gdelt_economy", _require_body(fetched), collected_at
    )
    records, cursor_after = await _select(run, lease, records, collected_at)
    await _accept_native(
        run, lease, records, _coverage(records), collected_at, cursor_after=cursor_after
    )


async def _run_sec_edgar(run: Run) -> None:
    """SEC EDGAR recent filings: one gated send with the owner-declared User-Agent; missing scope fails closed."""
    from modules.connectors.providers import filings
    from modules.connectors.providers.macro import MAX_BODY_BYTES, decode_json

    cfg = run.attempt.source.configuration or {}
    request = _payload(filings.sec_edgar_request, cfg.get("sec_cik"), cfg.get("sec_contact_email"))
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES)
    payload = await _payload_async(decode_json, _require_body(fetched))
    records = await _payload_async(filings.map_sec_edgar, payload, collected_at, cfg["sec_cik"])
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_openalex(run: Run) -> None:
    """OpenAlex works search: one gated send to a server-built URL; no validators, first page only."""
    from modules.connectors.providers import scholarly

    collected_at = datetime.now(UTC)
    request = _payload(
        scholarly.openalex_request, (run.attempt.source.configuration or {}).get("scholarly_query")
    )
    lease = await _lease(run)
    fetched = await _get_body(run, request, max_bytes=scholarly.OPENALEX_MAX_BODY)
    records = await _payload_async(scholarly.map_openalex, _require_body(fetched), collected_at)
    await _accept_native(run, lease, records, _coverage(records), collected_at)


ADAPTERS.update(
    {
        **dict.fromkeys(PURE_PROVIDERS, _run_pure),
        "vietnamworks_jobs": _run_vietnamworks,
        "itviec_jobs": _run_itviec,
        "vn_stock": _run_vn_stock,  # two endpoints (stocks, indices); overrides the single-request runner
        **dict.fromkeys(KEYED, _run_keyed),
        "coingecko": _run_coingecko,
        **dict.fromkeys(
            _news.FEED_URLS, _run_feed
        ),  # every fixed-URL feed preset shares one adapter
        "coinpaprika": _run_coinpaprika,  # one call per selected coin
        "google_news": _run_feed,
        "openalex": _run_openalex,
        "sec_edgar": _run_sec_edgar,
        "hn_top": _run_hn,
        "gdelt_economy": _run_gdelt,
    }
)


# ---------------------------------------------------------------- GitHub / Telegram (proof-bearing)


async def _credential_fence(
    run: Run,
    *,
    github: tuple[Any, tuple[UUID, int]] | None = None,
    telegram: Any = None,
    google: tuple[UUID, int] | None = None,
    reddit: tuple[UUID, int] | None = None,
) -> None:
    """Before every send/accept, recheck source credentials and provider grant under ordered locks.

    GitHub, Telegram, Google and Reddit bindings are compared with their captured operation/revision;
    the lease and request must still be current before network bytes or a receipt are accepted.
    """
    from modules.connectors.models import ConnectorOAuthGrant, GithubOAuthGrant

    a, lease = run.attempt, run.extras["lease"]
    async with run.factory() as session:
        try:
            current, row, _ = await provisioning.lock_connector(
                session,
                a.source.id,
                provisioning._ALL_CREDENTIAL_SLOTS,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                expected_access_fence=a.access_fence,
            )
            if (
                current != a.source_fence
                or row is None
                or row.credential_revision != a.credential_revision
                or not await provisioning.require_collection_fence(
                    session,
                    a.source,
                    lease.source_generation,
                    lease.connector_revision,
                    scope=a.scope,
                    multi_workspace_enabled=a.multi,
                )
            ):
                raise CollectionFenceLost("credential_changed", "revision_changed")
            await ingestion.lock_source_credentials_in_uow(
                session,
                a.source.id,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                access_fence=a.access_fence,
                source_fence=a.source_fence,
            )
            if github is not None:
                fence, binding = github
                now_fence = await connectors.lock_github_binding_fence_in_uow(
                    session,
                    a.source.id,
                    source_generation=lease.source_generation,
                    connector_revision=lease.connector_revision,
                    scope=a.scope,
                    multi_workspace_enabled=a.multi,
                    access_fence=a.access_fence,
                    source_fence=a.source_fence,
                )
                grant = await session.get(GithubOAuthGrant, a.source.id, populate_existing=True)
                if (
                    now_fence != fence
                    or grant is None
                    or binding != (grant.operation_id, grant.token_revision)
                ):
                    raise CollectionFenceLost("credential_changed", "revision_changed")
            if telegram is not None:
                now_native = await connectors.get_native_credential_snapshot(
                    session,
                    a.source.id,
                    source_generation=lease.source_generation,
                    connector_revision=lease.connector_revision,
                    scope=a.scope,
                    multi_workspace_enabled=a.multi,
                )
                if now_native != telegram or telegram.access_fence != a.access_fence:
                    raise CollectionFenceLost("credential_changed", "revision_changed")
            if google is not None:
                grant = await session.scalar(
                    select(ConnectorOAuthGrant)
                    .where(
                        ConnectorOAuthGrant.source_id == a.source.id,
                    )
                    .with_for_update()
                )
                if (
                    grant is None
                    or grant.state != "ready"
                    or grant.credential_revision != a.credential_revision
                    or (grant.operation_id, grant.token_revision) != google
                    or grant.source_generation != lease.source_generation
                    or grant.configuration_revision != lease.connector_revision
                ):
                    raise CollectionFenceLost("credential_changed", "revision_changed")
            if reddit is not None:
                grant = await session.scalar(
                    select(ConnectorOAuthGrant)
                    .where(
                        ConnectorOAuthGrant.source_id == a.source.id,
                    )
                    .with_for_update()
                )
                if (
                    grant is None
                    or grant.provider != "reddit"
                    or grant.state != "ready"
                    or grant.workspace_id != a.scope.workspace_id
                    or grant.owner_id != a.scope.actor_user_id
                    or grant.credential_revision != a.credential_revision
                    or (grant.operation_id, grant.token_revision) != reddit
                    or grant.source_generation != lease.source_generation
                    or grant.configuration_revision != lease.connector_revision
                ):
                    raise CollectionFenceLost("credential_changed", "revision_changed")
            if not await ingestion.validate_connector_collection_in_uow(
                session,
                lease,
                collector_token=None,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                access_fence=a.access_fence,
                source_fence=a.source_fence,
                request_ref=a.ref,
            ):
                raise CollectionFenceLost("admission_lost")
        finally:
            await session.rollback()


async def _google_access_token(run: Run) -> tuple[str, tuple[UUID, int]]:
    """Load a source-bound read-only Google token and serialize refresh under its grant row lock.

    Refresh results are persisted only while the original source/configuration/credential fence
    remains current. Holding the grant lock across Google's token exchange prevents concurrent
    workers from overwriting a rotated refresh token; failures leave the prior grant unchanged.
    """
    from modules.connectors.google_oauth_tokens import refresh_google_token
    from modules.connectors.models import ConnectorOAuthGrant
    from modules.connectors.oauth_routes import _PROVIDER_SCOPES, _cipher, _open_cipher

    a = run.attempt
    async with run.factory() as session:
        current, row, _ = await provisioning.lock_connector(
            session,
            a.source.id,
            provisioning._ALL_CREDENTIAL_SLOTS,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            expected_access_fence=a.access_fence,
        )
        if (
            current != a.source_fence
            or row is None
            or row.credential_revision != a.credential_revision
            or not await provisioning.require_collection_fence(
                session,
                a.source,
                a.source.generation,
                a.connector_revision,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
            )
        ):
            await session.rollback()
            raise CollectionFenceLost("credential_changed", "revision_changed")
        await ingestion.lock_source_credentials_in_uow(
            session,
            a.source.id,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            access_fence=a.access_fence,
            source_fence=a.source_fence,
        )
        grant = await session.scalar(
            select(ConnectorOAuthGrant)
            .where(
                ConnectorOAuthGrant.source_id == a.source.id,
            )
            .with_for_update()
        )
        if (
            grant is None
            or grant.state != "ready"
            or grant.provider != a.source.provider
            or grant.workspace_id != a.scope.workspace_id
            or grant.owner_id != a.scope.actor_user_id
            or grant.source_generation != a.source.generation
            or grant.configuration_revision != a.connector_revision
            or grant.credential_revision != a.credential_revision
            or not grant.encrypted_tokens
            or _PROVIDER_SCOPES.get(grant.provider) not in grant.granted_scopes
        ):
            await session.rollback()
            raise CredentialMissing
        try:
            key = run.settings.connector_credential_encryption_key.get_secret_value()
            tokens = _open_cipher(
                key,
                grant.encrypted_tokens,
                a.source.id,
                grant.operation_id,
                grant.source_generation,
                grant.configuration_revision,
            )
            access = tokens.get("access_token")
            refresh = tokens.get("refresh_token")
            if not isinstance(access, str) or not isinstance(refresh, str):
                raise ValueError("google_token_missing")
            expiry = grant.token_expires_at
            if expiry is not None and expiry > datetime.now(UTC) + timedelta(seconds=60):
                binding = (grant.operation_id, grant.token_revision)
                await session.rollback()
                return access, binding
            rotated = await refresh_google_token(run.settings, grant.provider, refresh)
            tokens.update(rotated)
            grant.token_revision += 1
            grant.encrypted_tokens = _cipher(
                key,
                a.source.id,
                grant.operation_id,
                grant.source_generation,
                grant.configuration_revision,
                tokens,
            )
            grant.token_expires_at = datetime.fromisoformat(rotated["access_expires_at"])
            binding = (grant.operation_id, grant.token_revision)
            access = rotated["access_token"]
            await session.commit()
            return access, binding
        except Exception:
            await session.rollback()
            raise


async def _reddit_access_token(run: Run) -> tuple[str, tuple[UUID, int]]:
    """Load or serialize-refresh Reddit's source-bound read-only grant for one admitted collection.

    The current source and owner credentials are locked before the grant row. Refresh uses the
    grant lock and persists its new token revision before returning, so every HTTP send and the
    eventual receipt can prove the same token revision.
    """
    from modules.connectors.reddit_oauth_routes import refresh_reddit_grant

    a = run.attempt
    async with run.factory() as session:
        current, row, _ = await provisioning.lock_connector(
            session,
            a.source.id,
            provisioning._ALL_CREDENTIAL_SLOTS,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            expected_access_fence=a.access_fence,
        )
        if (
            current != a.source_fence
            or row is None
            or a.credential_revision is None
            or row.credential_revision != a.credential_revision
            or not await provisioning.require_collection_fence(
                session,
                a.source,
                a.source.generation,
                a.connector_revision,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
            )
        ):
            await session.rollback()
            raise CollectionFenceLost("credential_changed", "revision_changed")
        await ingestion.lock_source_credentials_in_uow(
            session,
            a.source.id,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            access_fence=a.access_fence,
            source_fence=a.source_fence,
        )
        try:
            access_token, operation_id, token_revision = await refresh_reddit_grant(
                session,
                run.settings,
                a.source.id,
                workspace_id=a.scope.workspace_id,
                owner_id=a.scope.actor_user_id,
                source_generation=a.source.generation,
                configuration_revision=a.connector_revision,
                credential_revision=a.credential_revision,
            )
        except ValueError as exc:
            await session.rollback()
            raise CredentialMissing from exc
        return access_token, (operation_id, token_revision)


async def _accept_proof(
    run: Run,
    lease: Any,
    *,
    records: Sequence[IngestionRecord],
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"],
    cursor_after: str | None,
    collected_at: datetime,
    github_segment: Any = None,
    telegram: Sequence[Any] = (),
    raw: Sequence[Any] = (),
    native_operation_id: UUID | None = None,
    google_binding: tuple[UUID, int] | None = None,
    reddit_binding: tuple[UUID, int] | None = None,
) -> None:
    """Hand a proof-bearing page to ingestion for one atomic receipt and cursor commit.

    OAuth operation/token revisions are included only for their matching source provider and
    are revalidated by the ingestion owner before durable receipt acceptance.
    """
    a = run.attempt
    batch = NativeCollectionBatch(
        source_id=a.source.id,
        source_generation=lease.source_generation,
        connector_revision=lease.connector_revision,
        lease_token=lease.token,
        cursor_before=lease.cursor_before,
        cursor_after=cursor_after,
        records=list(records),
        telegram_deliveries=tuple(telegram),
        telegram_raw_deliveries=tuple(raw),
        coverage=coverage,
        github_segment=github_segment,
        google_operation_id=google_binding[0] if google_binding is not None else None,
        google_token_revision=google_binding[1] if google_binding is not None else None,
        reddit_operation_id=reddit_binding[0] if reddit_binding is not None else None,
        reddit_token_revision=reddit_binding[1] if reddit_binding is not None else None,
        collected_at=collected_at,
    )
    async with run.factory() as session:
        await ingestion.accept_native_collection(
            session,
            batch,
            collector_token=None,
            lease=lease,
            scope=a.scope,
            multi_workspace_enabled=a.multi,
            expected_native_operation_id=native_operation_id,
            expected_world_credential_operation_id=None,
            request_ref=a.ref,
        )
    run.extras["accepted"] = True


async def _run_github(run: Run) -> None:
    """GitHub: lease, claim a due webhook hint, open the OAuth grant, one fenced GET, validate, record."""
    from sqlalchemy import select

    from modules.connectors.github import oauth as github_oauth
    from modules.connectors.github.adapter import collect_github_segment
    from modules.connectors.github.schemas import GitHubHintClaimProof, project_github_source_config
    from modules.connectors.github.sync import validate_github_segment
    from modules.connectors.models import GithubOAuthGrant

    a = run.attempt
    source, collected_at = a.source, datetime.now(UTC)
    lease = await _lease(run)
    async with run.factory() as session:
        claim = await connectors.claim_github_hint(
            session,
            source_id=source.id,
            source_generation=lease.source_generation,
            connector_revision=lease.connector_revision,
            multi_workspace_enabled=a.multi,
            scope=a.scope,
        )
    async with run.factory() as session:
        try:
            await provisioning.lock_connector(
                session,
                source.id,
                provisioning._ALL_CREDENTIAL_SLOTS,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                expected_access_fence=a.access_fence,
            )
            fence = await connectors.lock_github_binding_fence_in_uow(
                session,
                source.id,
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                access_fence=a.access_fence,
                source_fence=a.source_fence,
            )
            if fence is None:
                raise CredentialMissing
            grant = await session.scalar(
                select(GithubOAuthGrant)
                .where(GithubOAuthGrant.source_id == source.id)
                .with_for_update()
            )
            if (
                grant is None
                or grant.state != "ready"
                or grant.encrypted_tokens is None
                or grant.source_generation != lease.source_generation
                or grant.configuration_revision != lease.connector_revision
                or grant.expires_at is None
                or grant.expires_at <= datetime.now(UTC)
            ):
                raise CredentialMissing
            binding = (grant.operation_id, grant.token_revision)
            pair = github_oauth._open_token_cipher(
                run.settings.connector_credential_encryption_key.get_secret_value(),
                grant.encrypted_tokens,
                source.id,
                grant.operation_id,
                grant.source_generation,
                grant.configuration_revision,
            )
        finally:
            await session.rollback()
    token = pair.get("access_token")
    if not isinstance(token, str) or not token:
        raise CredentialMissing
    config = project_github_source_config(source.configuration)
    redis, permit = cast(Any, run.ctx.get("redis")), secrets.token_urlsafe(24)
    held = False

    async def before_send() -> None:
        """Gate, fence the GitHub binding and take the cluster-wide single GitHub network slot before each send."""
        nonlocal held
        await run.gate()
        await _credential_fence(run, github=(fence, binding))
        if (
            redis is not None
        ):  # cluster-wide single GitHub network slot; expiry outlives the run deadline
            if not await redis.set("connectors:provider:active:github", permit, nx=True, ex=75):
                raise connectors.ProviderRateLimited(datetime.now(UTC) + timedelta(seconds=1))
            held = True

    try:
        proof = await collect_github_segment(
            config,
            token,
            fence=fence,
            cursor_before=lease.cursor_before,
            collected_at=collected_at,
            before_request=before_send,
            hint_claim=GitHubHintClaimProof.model_validate(claim.model_dump(mode="python"))
            if claim is not None
            else None,
        )
    finally:
        if held and redis is not None:
            with suppress(Exception):
                await redis.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
                    1,
                    "connectors:provider:active:github",
                    permit,
                )
    validated = validate_github_segment(
        fence, config, lease.cursor_before, proof, collected_at=collected_at
    )
    await _credential_fence(run, github=(fence, binding))
    await _accept_proof(
        run,
        lease,
        records=validated.records,
        coverage=validated.coverage,
        cursor_after=validated.cursor_after,
        collected_at=collected_at,
        github_segment=proof,
    )


async def _run_telegram(run: Run) -> None:
    """Telegram: bot token read under locks, getUpdates fenced per send; one accepted page per request.

    Pages that only replay already-ingested updates advance the offset (fresh lease each time, at most
    five pages); the first page with new work is accepted and settles the request.
    """
    from modules.connectors.credentials import decrypt_native_token
    from modules.connectors.providers.telegram import fetch_telegram_updates, map_telegram_update
    from modules.ingestion.public import classify_telegram_probe

    a, source = run.attempt, run.attempt.source
    lease = await _lease(run)
    async with run.factory() as session:
        try:
            await provisioning.lock_connector(
                session,
                source.id,
                provisioning._ALL_CREDENTIAL_SLOTS,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
                expected_access_fence=a.access_fence,
            )
            snapshot = await connectors.get_native_credential_snapshot(
                session,
                source.id,
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
            )
            if (
                snapshot is None
                or snapshot.state != "ready"
                or not snapshot.encrypted_token
                or not snapshot.verified_bot_id
            ):
                raise CredentialMissing
            token = decrypt_native_token(
                run.settings.connector_credential_encryption_key.get_secret_value(), snapshot
            )
        finally:
            await session.rollback()
        cursor = await ingestion.read_telegram_collection_state(
            session, lease, scope=a.scope, multi_workspace_enabled=a.multi
        )
    allowed = frozenset(cast("list[str]", source.configuration["telegram_chat_ids"]))

    async def before_send() -> None:
        """Gate and fence the Telegram snapshot before each Bot API send."""
        await run.gate()
        await _credential_fence(run, telegram=snapshot)

    offset: int | None = None
    total_bytes = total_updates = 0
    for page_number in range(5):
        remaining = 25 * 1024 * 1024 - total_bytes
        page = await fetch_telegram_updates(
            token,
            offset=offset,
            remaining_bytes=min(10 * 1024 * 1024, remaining),
            before_request=before_send,
        )
        total_bytes += page.transport_bytes
        total_updates += len(page.deliveries)
        if page.transport_bytes > remaining or total_updates > 500:
            raise ValueError("telegram_trigger_limit_exceeded")
        classification = classify_telegram_probe(
            cursor,
            page.deliveries,
            received_at=page.collected_at,
            verified_bot_id=snapshot.verified_bot_id,
        )
        if classification.conflict_code is not None:
            raise ValueError("telegram_stream_conflict")
        replay = set(classification.replay_update_ids)
        if page.deliveries and len(replay) == len(page.deliveries):
            if page_number == 4 or cursor is None or cursor.last_update_id >= 2**63 - 1:
                await _credential_fence(run, telegram=snapshot)
                await _accept_proof(
                    run,
                    lease,
                    records=(),
                    coverage="pending_updates_only",
                    cursor_after=lease.cursor_before,
                    collected_at=page.collected_at,
                    native_operation_id=snapshot.operation_id,
                )
                return
            async with run.factory() as session:
                await ingestion.release_connector_collection(
                    session, lease, error_code=None, scope=a.scope, multi_workspace_enabled=a.multi
                )
            lease = await _lease(run)
            if lease.configuration_revision != a.access_fence.configuration_revision:
                raise CollectionFenceLost("access_changed", "revision_changed")
            offset = cursor.last_update_id + 1
            continue
        proofs = {p.update_id: p for p in classification.delivery_proofs}
        records = [
            record
            for d in page.deliveries
            if d.update_id not in replay
            if (
                record := map_telegram_update(
                    d.update,
                    allowed_chat_ids=allowed,
                    proof=proofs[d.update_id],
                    collected_at=page.collected_at,
                )
            )
            is not None
        ]
        await _credential_fence(run, telegram=snapshot)
        await _accept_proof(
            run,
            lease,
            records=records,
            coverage="pending_updates_only",
            cursor_after=classification.cursor_after.model_dump_json()
            if classification.cursor_after is not None
            else lease.cursor_before,
            collected_at=page.collected_at,
            telegram=classification.delivery_proofs,
            raw=page.deliveries,
            native_operation_id=snapshot.operation_id,
        )
        return


ADAPTERS.update({"github": _run_github, "telegram": _run_telegram})


# ---------------------------------------------------------------- orchestration


def _classify(exc: BaseException, *, keyless: bool = False) -> dict[str, Any]:
    """Map an adapter failure to ``settle_admission`` arguments (never response text).

    ``keyless`` marks a source that sends no credential: a 401/403 from it is the provider blocking or
    refusing the request (geo/IP policy, missing user agent), not a bad owner credential, so it settles as
    ``provider_rejected`` instead of the misleading ``invalid_credential``.
    """
    from modules.connectors.mcp import McpCollectionError

    if isinstance(exc, McpCollectionError):
        # Stable secret-free MCP code; all-calls-failed or stale config will not heal by retrying.
        return {"outcome": "failed", "error_code": exc.code}
    if isinstance(exc, rest.CollectionIncomplete):
        return {"outcome": "failed", "error_code": "collection_incomplete"}
    if isinstance(exc, rest.RestSchemaChanged):
        return {"outcome": "failed", "error_code": "schema_changed"}
    if isinstance(exc, CredentialMissing):
        return {"outcome": "failed", "error_code": "credential_missing"}
    if isinstance(exc, TermsIneligible):
        return {"outcome": "failed", "error_code": "terms_not_accepted"}
    if isinstance(exc, rest.UnsafeDestination):
        if str(exc) == "host_unresolvable":
            # A DNS failure/timeout is transient network trouble, not a policy block; every retry re-resolves
            # and re-checks that all addresses are public, so retrying never weakens the destination guard.
            return {"outcome": "failed", "error_code": "provider_timeout", "retryable": True}
        return {"outcome": "failed", "error_code": "destination_blocked"}
    if isinstance(exc, rest.ProviderHttpError):
        if exc.status_code in {401, 403} and not keyless:
            return {"outcome": "failed", "error_code": "invalid_credential"}
        return {
            "outcome": "failed",
            "error_code": "provider_unavailable" if exc.retryable else "provider_rejected",
            "retryable": exc.retryable,
        }
    if isinstance(exc, (TimeoutError, httpx.HTTPError, OSError)):
        return {"outcome": "failed", "error_code": "provider_timeout", "retryable": True}
    if is_http_error(exc):
        return {
            "outcome": "failed",
            "error_code": "ingestion_conflict",
            "retryable": error_status(exc) == 409,
        }
    if isinstance(exc, ValueError):
        return {"outcome": "failed", "error_code": "provider_response_invalid"}
    return {"outcome": "failed", "error_code": "executor_error", "retryable": True}


def _sends_no_credential(source: Any) -> bool:
    """True for RSS/web sources and providers that need no key; generic REST may carry an owner header.

    Free-provider specs decide first. Catalog-only providers (youtube, arxiv, ...) have no spec, so they are keyless
    only when the catalog lists ``("none",)`` as the sole auth method; entries with an optional header/bearer stay False.
    """
    if source.provider is None:
        return source.type in {"rss", "web"}
    spec = get_provider_spec(source.provider)
    if spec is not None:
        return not spec.key_required
    entry = get_catalog_entry(source.provider)
    return entry is not None and entry.auth_methods == ("none",)


async def _release_lease(run: Run, error_code: str) -> None:
    """Best-effort release of an unaccepted native source lease; it expires on its own if this fails."""
    lease = run.extras.get("lease")
    if lease is None or run.extras.get("accepted"):
        return
    a = run.attempt
    with suppress(BaseException):
        async with asyncio.timeout(3), run.factory() as session:
            await ingestion.release_connector_collection(
                session,
                lease,
                error_code=error_code,
                scope=a.scope,
                multi_workspace_enabled=a.multi,
            )


def _adapter_for(source: ConnectorSource) -> Adapter | None:
    """Resolve the collection adapter for a source: type-based for provider-less sources, else registered or native; ``None`` if unsupported."""
    provider = source.provider
    if provider is None:
        return {"api": _run_rest, "rss": _run_rss, "web": _run_web, "mcp": _run_mcp}.get(
            source.type
        )
    if provider in ADAPTERS:
        return ADAPTERS[provider]
    return _run_native if provider in SIMPLE_NATIVE else None


def provider_collection_available(provider_id: str) -> bool:
    """Report whether a provider ID reaches a registered production collection adapter."""
    return provider_id in ADAPTERS or provider_id in SIMPLE_NATIVE


def supports_native(source: ConnectorSource) -> bool:
    """True when the shared executor can collect this source; activation refuses everything else."""
    return _adapter_for(source) is not None and source.provider not in REFUSED_HERE


async def execute_collection(ctx: dict[str, object], admission: CollectionAdmissionRead) -> None:
    """Run one admitted attempt to a settled request, or leave recovery to C2 if authority was lost."""
    settings: Settings = ctx["settings"]  # type: ignore[assignment]
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]  # type: ignore[assignment]
    attempt = await _load_attempt(factory, admission, settings.multi_workspace_enabled)
    if attempt is None:
        return
    adapter = _adapter_for(attempt.source)
    if adapter is None or attempt.source.provider in REFUSED_HERE:
        await _settle(factory, admission, outcome="failed", error_code="collection_unsupported")
        return
    quota_policies = get_quota_policies(attempt.source.provider)
    state: dict[str, Any] = {"attempt": attempt, "factory": factory, "settings": settings}
    gate = SendGate(
        factory,
        attempt,
        settings=settings,
        deadline=time.monotonic() + rest.RUN_DEADLINE_SECONDS,
        quota_provider=attempt.source.provider if quota_policies else None,
        fingerprint=_alpha_fingerprint(state)
        if attempt.source.provider == "alpha_vantage"
        else None,
    )
    run = Run(ctx=ctx, factory=factory, settings=settings, attempt=attempt, gate=gate)
    lost = asyncio.Event()
    main = asyncio.current_task()
    if main is None:
        raise RuntimeError("invariant violated: main must not be None")
    beat = asyncio.create_task(_heartbeat(factory, attempt, main, lost))
    settle: dict[str, Any] | None = None
    try:
        async with asyncio.timeout(rest.RUN_DEADLINE_SECONDS):
            await adapter(run)
        return  # accepted: the ingestion owner already settled request + slot in its own commit
    except asyncio.CancelledError:
        if not lost.is_set():
            raise
        await _release_lease(run, "collection_admission_lost")
        return  # renewal failure: C2 recovery or cancellation owns the request
    except CollectionFenceLost as exc:
        await _release_lease(run, "collection_admission_lost")
        if exc.cancel_as is not None:
            await _settle(factory, admission, outcome="cancelled", error_code=exc.cancel_as)
        return
    except quota.QuotaDeferred as exc:
        settle = {"outcome": "deferred", "provider_deadline": exc.defer_until}
    except connectors.ProviderRateLimited as exc:
        with suppress(Exception):
            await _record_rate_limit(run, exc.next_eligible_at)
        settle = {
            "outcome": "failed",
            "error_code": "provider_rate_limited",
            "retryable": True,
            "provider_deadline": exc.next_eligible_at,
        }
    except Exception as exc:  # deliberate boundary: every failure must settle the request
        settle = _classify(exc, keyless=_sends_no_credential(attempt.source))
        if isinstance(exc, rest.ProviderHttpError):
            # The settled code hides the upstream status; log it (never the body or URL) to tell throttles from rejects.
            logger.warning(
                "Collection provider HTTP %s; provider=%s request_id=%s error_code=%s",
                exc.status_code,
                attempt.source.provider,
                admission.request_id,
                settle.get("error_code"),
            )
        if settle["error_code"] == "executor_error":
            # Unclassified failure: the settled code alone hides the cause, so keep the traceback (no response body).
            logger.exception(
                "Collection adapter failed; provider=%s request_id=%s",
                attempt.source.provider,
                admission.request_id,
            )
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError):
            await beat
    await _release_lease(run, str(settle.get("error_code") or "collection_deferred"))
    await _settle(factory, admission, **settle)


async def collect_source(ctx: dict[str, object], request_id: str) -> None:
    """ARQ entrypoint: admit the durable request and execute it (C2 job with the C3 executor bound)."""
    from modules.connectors.worker import process_collection_request

    await process_collection_request({**ctx, "collection_executor": execute_collection}, request_id)
