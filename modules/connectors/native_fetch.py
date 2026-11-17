"""Native provider fetch orchestration behind ``POST /sources/{id}/provider-fetch``.

The route parses, authorizes the collector bearer and loads the Source; this module owns
everything after that: admission fences, the durable lease, the bounded collection window,
acceptance by Ingestion and the failure/release paths. Provider-specific work lives in
``ProviderCollector`` implementations registered per provider id (see
``modules.connectors.native_collectors``), so adding a provider never edits the dispatcher.

Invariants preserved from the original route body:

* GitHub's single 60-second segment deadline starts before connector fencing and bounds every
  pre-lease await.
* SQL is rolled back after admission and before any provider network I/O.
* The active lease is released on every failure exit path; the cluster-shared GitHub network
  permit is released in ``finally``.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
    DomainError,
    UnavailableError,
    ValidationFailedError,
    error_detail,
    error_status,
)
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors import provisioning, registry
from modules.connectors import public as connectors_public
from modules.connectors.backends import GATED_EXECUTOR_PROVIDERS
from modules.connectors.collection_schemas import CollectionRequestRef
from modules.connectors.models import GithubOAuthGrant
from modules.connectors.public import (
    NativeCredentialSnapshot,
    ProviderRateLimited,
    get_native_credential_snapshot,
    is_native_provider,
)
from modules.ingestion import public as ingestion
from modules.ingestion.public import (
    ConnectorCollectionLease,
    NativeCollectionBatch,
    NativeCollectionReceipt,
)
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence

Coverage = Literal["returned_snapshot", "pending_updates_only", "truncated"]
GITHUB_RECONNECT_DETAIL = "GitHub grant is expired or requires reconnection"


class ProviderFetchRequest(BaseModel):
    """Accept only source-generation and connector-revision fences plus the admission from n8n."""

    model_config = ConfigDict(extra="forbid")

    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    backend_revision: int | None = Field(default=None, ge=1)
    admission_request_id: UUID
    admission_token: UUID


class ProviderFetchRead(BaseModel):
    """Expose verified receipt workspace/status without content, cursor or credential material."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    status: Literal["succeeded", "queued", "no_changes", "rate_limited"]
    batch_id: UUID | None = None
    run_id: UUID | None = None
    received_update_count: int = Field(ge=0, le=500)
    record_count: int = Field(ge=0, le=500)
    coverage: Coverage
    next_eligible_at: datetime | None = None

    @field_validator("next_eligible_at")
    @classmethod
    def aware_deadline(cls, value: datetime | None) -> datetime | None:
        """Reject naive retry times so clients do not misread provider deadlines."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("next_eligible_at must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None


class ProviderAdmissionBusy(RuntimeError):
    """Signal that the shared single-provider network permit is held by another collector."""


# ---------------------------------------------------------------------------
# Shared Redis cooldown / permit helpers
# ---------------------------------------------------------------------------


async def extend_provider_cooldown(request: Request, provider: str, deadline: datetime) -> datetime:
    """Atomically retain the later UTC deadline across all sources using one fixed provider."""
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("Provider retry deadline must be timezone-aware")
    deadline_ms = int(deadline.astimezone(UTC).timestamp() * 1000)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    script = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local incoming = tonumber(ARGV[1])
if incoming > current then
  local ttl = incoming - tonumber(ARGV[2])
  if ttl < 1 then ttl = 1 end
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ttl)
  current = incoming
end
return current
"""
    try:
        stored = int(
            await request.app.state.redis.eval(
                script, 1, f"connectors:provider:cooldown:{provider}", deadline_ms, now_ms
            )
        )
    except Exception as exc:
        raise UnavailableError("provider_rate_state_unavailable") from exc
    return datetime.fromtimestamp(stored / 1000, UTC)


async def provider_cooldown(request: Request, provider: str) -> datetime | None:
    """Read the fixed-provider shared retry deadline; Redis failure closes collection."""
    try:
        raw = await request.app.state.redis.get(f"connectors:provider:cooldown:{provider}")
        if raw is None:
            return None
        milliseconds = int(raw)
        return datetime.fromtimestamp(milliseconds / 1000, UTC) if milliseconds > 0 else None
    except Exception as exc:
        raise UnavailableError("provider_rate_state_unavailable") from exc


async def acquire_github_network_permit(request: Request) -> str | None:
    """Acquire one cluster-shared GitHub network slot with an expiry beyond the segment deadline."""
    token = secrets.token_urlsafe(24)
    try:
        acquired = await request.app.state.redis.set(
            "connectors:provider:active:github", token, nx=True, ex=75
        )
    except Exception as exc:
        raise UnavailableError("provider_admission_unavailable") from exc
    return token if acquired else None


async def release_github_network_permit(request: Request, token: str) -> None:
    """Release only the GitHub network slot still owned by this collector token."""
    script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
    try:
        await request.app.state.redis.eval(script, 1, "connectors:provider:active:github", token)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        # The bounded permit expires automatically; never release another collector's token.
        return


async def await_with_github_segment_deadline[T](
    operation: Awaitable[T], *, deadline: float | None
) -> T:
    """Bound one pre-lease await by the shared GitHub segment deadline.

    A missing deadline leaves other providers' admission behavior unchanged. When the
    deadline expires, the awaited operation is cancelled and its caller rolls back;
    after lease acquisition, the route releases only the exact active lease token.
    """
    if deadline is None:
        return await operation
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(operation, timeout=remaining)


# ---------------------------------------------------------------------------
# Error classification and best-effort settle/release
# ---------------------------------------------------------------------------


def collection_error_code(exc: BaseException) -> str:
    """Map auth-type failures (HTTP 401/403, Telegram credentials rejected, GitHub reconnect) to a reconnect-needed code."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
        return "provider_unauthorized"
    if getattr(exc, "code", None) == "telegram_credentials_rejected":
        return "provider_unauthorized"
    if error_status(exc) is not None and error_detail(exc) == GITHUB_RECONNECT_DETAIL:
        return "provider_unauthorized"
    return "provider_collection_failed"


def request_failure(exc: BaseException) -> tuple[str, bool]:
    """Map an exit exception to (request error code, retryable): auth/schema/terms never retry."""
    code = collection_error_code(exc)
    if code == "provider_unauthorized":
        return "invalid_credential", False
    status = error_status(exc)
    if status is not None:
        if error_detail(exc) == "terms_not_accepted":
            return "terms_not_accepted", False
        if status == 422:
            return "schema_changed", False
        return code, status >= 500
    return code, True


async def settle_request_exit(
    session: AsyncSession,
    ref: CollectionRequestRef,
    *,
    outcome: Literal["no_changes", "failed"],
    error_code: str | None = None,
    retryable: bool = False,
    provider_deadline: datetime | None = None,
) -> None:
    """Best-effort immediate settle of a non-accepted exit so the slot frees without waiting for expiry."""
    try:
        async with asyncio.timeout(3):
            await session.rollback()
            await connectors_public.settle_collection_exit(
                session,
                ref,
                outcome=outcome,
                error_code=error_code,
                retryable=retryable,
                provider_deadline=provider_deadline,
            )
    except BaseException:  # noqa: BLE001  # deliberate boundary: the expiry sweeper still settles it
        return


async def release_failed_collection(
    session: AsyncSession,
    lease: ConnectorCollectionLease,
    *,
    error_code: str,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Best-effort release after failure without masking the original outcome.

    Rollback clears failed SQLAlchemy transaction state before the ingestion
    owner checks the lease token. Cleanup is bounded; an unreleased reservation
    still expires under its existing lease policy.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    try:
        async with asyncio.timeout(3):
            await session.rollback()
            await ingestion.release_connector_collection(
                session,
                lease,
                error_code=error_code,
                multi_workspace_enabled=multi_workspace_enabled,
                scope=scope,
            )
    except BaseException:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return


def provider_fetch_read(
    receipt: NativeCollectionReceipt, next_eligible_at: datetime | None = None
) -> ProviderFetchRead:
    """Expose actual original receipt workspace/status without private cursor/content."""
    return ProviderFetchRead(
        workspace_id=receipt.workspace_id,
        status=receipt.status,
        batch_id=receipt.batch_id,
        run_id=receipt.run_id,
        received_update_count=receipt.received_update_count,
        record_count=receipt.record_count,
        coverage=receipt.coverage,
        next_eligible_at=next_eligible_at or receipt.next_eligible_at,
    )


def rate_limited_read(scope: Scope, coverage: Coverage, deadline: datetime) -> ProviderFetchRead:
    """Build the deterministic ``rate_limited`` response: no batch, run, updates or records."""
    return ProviderFetchRead(
        workspace_id=scope.workspace_id,
        status="rate_limited",
        batch_id=None,
        run_id=None,
        received_update_count=0,
        record_count=0,
        coverage=coverage,
        next_eligible_at=deadline,
    )


# ---------------------------------------------------------------------------
# Send fence
# ---------------------------------------------------------------------------


async def native_send_fence(
    session: AsyncSession,
    source: ConnectorSource,
    lease: ConnectorCollectionLease,
    collector_token: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
    native_snapshot: NativeCredentialSnapshot | None = None,
    world_operation_id: UUID | None = None,
    github_fence: object = None,
    github_binding: tuple[UUID, int] | None = None,
    request_ref: CollectionRequestRef | None = None,
) -> None:
    """Revalidate original native authority in parent order and release SQL before every send.

    The actual pre-I/O AccessFence/SourceFence and committed lease remain immutable.
    Lock admission/Source/provisioning/sorted slots, then applicable provider credential,
    then Ingestion's exact bearer/state reservation through its held-parent public seam.
    No current scope reconstruction, state ORM import, health/cursor mutation or I/O occurs.
    False/missing/changed proof raises 409; authorization/storage failures propagate.
    """
    try:
        current_source, row, _slots = await provisioning.lock_connector(
            session,
            source.id,
            provisioning._ALL_CREDENTIAL_SLOTS,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        if (
            current_source != source_fence
            or row is None
            or not await provisioning.require_collection_fence(
                session,
                source,
                lease.source_generation,
                lease.connector_revision,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        ):
            raise ConflictError("Original provider Source/configuration changed")
        if native_snapshot is not None:
            current_native = await get_native_credential_snapshot(
                session,
                source.id,
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if current_native != native_snapshot or native_snapshot.access_fence != access_fence:
                raise ConflictError("Original native credential changed")
        if source.provider == "alpha_vantage" and (
            not isinstance(world_operation_id, UUID)
            or not await connectors_public.validate_world_credential_operation_in_uow(
                session,
                source.id,
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
                expected_operation_id=world_operation_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence,
                source_fence=source_fence,
            )
        ):
            raise ConflictError("Original world credential changed")
        # Collector credentials are a later owner lock than provider credentials and
        # must precede the GitHub grant. The held validator below then acquires only
        # SourceIngestionState, preserving the accepted token -> grant -> state order.
        await ingestion.lock_source_credentials_in_uow(
            session,
            source.id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            source_fence=source_fence,
        )
        if github_fence is not None:
            current_binding = await connectors_public.lock_github_binding_fence_in_uow(
                session,
                source.id,
                source_generation=lease.source_generation,
                connector_revision=lease.connector_revision,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence,
                source_fence=source_fence,
            )
            grant = await session.get(GithubOAuthGrant, source.id, populate_existing=True)
            if (
                current_binding != github_fence
                or grant is None
                or github_binding != (grant.operation_id, grant.token_revision)
            ):
                raise ConflictError("Original GitHub grant changed")
        if not await ingestion.validate_connector_collection_in_uow(
            session,
            lease,
            collector_token=collector_token,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            source_fence=source_fence,
            request_ref=request_ref,
        ):
            raise ConflictError("Original collection reservation changed")
    finally:
        await session.rollback()


# ---------------------------------------------------------------------------
# Collector protocol, context and registry
# ---------------------------------------------------------------------------


@dataclass
class FetchContext:
    """Per-request state shared by the dispatcher and the selected ``ProviderCollector``.

    ``lease`` is the admitted reservation; ``active_lease[0]`` always names the reservation the
    failure paths must release (Telegram continuation replaces it after each accepted page).
    ``github_permit_token`` is set by the GitHub collector and released by the dispatcher in
    ``finally``. ``prepared`` carries whatever ``ProviderCollector.prepare`` produced.
    """

    session: AsyncSession
    request: Request
    source: ConnectorSource
    payload: ProviderFetchRequest
    collector_token: str
    request_ref: CollectionRequestRef
    scope: Scope
    multi_workspace_enabled: bool
    access_fence: AccessFence
    source_fence: SourceFence
    lease: ConnectorCollectionLease
    active_lease: list[ConnectorCollectionLease]
    segment_deadline: float | None
    github_permit_token: str | None = None
    prepared: Any = None

    async def send_fence(
        self, *, lease: ConnectorCollectionLease | None = None, **extra: Any
    ) -> None:
        """Run ``native_send_fence`` for ``lease`` (default: the admitted lease) with this request's fences.

        ``extra`` forwards the provider-specific proofs (native snapshot, world operation id,
        GitHub fence/binding). Releases SQL in ``native_send_fence``'s ``finally``.
        """
        await native_send_fence(
            self.session,
            self.source,
            self.lease if lease is None else lease,
            self.collector_token,
            request_ref=self.request_ref,
            scope=self.scope,
            multi_workspace_enabled=self.multi_workspace_enabled,
            access_fence=self.access_fence,
            source_fence=self.source_fence,
            **extra,
        )

    def before_request(self) -> Callable[[], Awaitable[None]]:
        """Return the plain per-send fence hook used by providers without extra proofs."""
        return self.send_fence


Hook = Callable[[], Awaitable[None]]


@dataclass
class CollectedPage:
    """A provider page the dispatcher must still gate, fence and hand to Ingestion.

    ``post_collect`` runs immediately after collection (before the rate-limit check) and
    ``pre_accept`` after it (before building the batch); either may raise to abort. When
    ``has_cursor_after`` is false the batch keeps ``lease.cursor_before``.
    """

    records: list[Any]
    coverage: Coverage
    collected_at: datetime
    next_eligible_at: datetime | None = None
    github_segment: Any = None
    cursor_after: str | None = None
    has_cursor_after: bool = False
    world_operation_id: UUID | None = None
    post_collect: Hook | None = None
    pre_accept: Hook | None = None


@dataclass
class AcceptedCollection:
    """Outcome of a collector that already accepted its own batch (Telegram) or hit a rate limit.

    Exactly one of ``receipt`` / ``rate_limited`` is set. ``eligible`` is the optional retry time
    merged into the response of an accepted receipt.
    """

    receipt: NativeCollectionReceipt | None = None
    eligible: datetime | None = None
    rate_limited: ProviderFetchRead | None = None


class ProviderCollector(Protocol):
    """Strategy for collecting one bounded page for a native provider.

    Implementations are registered per provider id with ``register_collector`` so the
    dispatcher never changes when a provider is added.
    """

    async def prepare(self, ctx: FetchContext) -> None:
        """Run after admission and before the collection window; failures release the lease."""

    async def collect(self, ctx: FetchContext) -> CollectedPage | AcceptedCollection:
        """Collect one page inside the collection timeout; every provider send must be fenced."""


class BaseProviderCollector:
    """Convenience base: ``prepare`` is a no-op; subclasses implement ``collect``."""

    async def prepare(self, ctx: FetchContext) -> None:
        """Do nothing: most providers need no pre-window claim."""
        del ctx

    async def collect(self, ctx: FetchContext) -> CollectedPage | AcceptedCollection:
        """Collect one page; implemented by each provider strategy."""
        raise NotImplementedError


_COLLECTORS: dict[str, ProviderCollector] = {}
_DEFAULT_COLLECTOR: list[ProviderCollector] = []


def register_collector(providers: tuple[str, ...], collector: ProviderCollector) -> None:
    """Bind ``collector`` to each provider id; later registration replaces an earlier one."""
    for provider in providers:
        _COLLECTORS[provider] = collector


def register_default_collector(collector: ProviderCollector) -> None:
    """Set the collector used for native providers without an explicit registration."""
    _DEFAULT_COLLECTOR[:] = [collector]


def resolve_collector(provider: str | None) -> ProviderCollector:
    """Return the collector for ``provider`` (default collector when unregistered)."""
    from modules.connectors import (
        native_collectors,  # noqa: F401  # registers built-in collectors on first use
    )

    if provider is not None and provider in _COLLECTORS:
        return _COLLECTORS[provider]
    return _DEFAULT_COLLECTOR[0]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def validate_native_source(source: ConnectorSource) -> None:
    """Reject sources that are not natively collectable through this endpoint.

    Raises 409 for non-native or scheduler-only providers and 422 for an invalid stored
    configuration, with the exact historical details.
    """
    if not is_native_provider(source.provider):
        raise ConflictError("Native provider is not configured")
    if (
        source.provider in GATED_EXECUTOR_PROVIDERS
    ):  # free providers run only through the gated shared executor
        raise ConflictError("Provider is collected by the scheduler only")
    try:
        registry.validate(source)
    except (ValueError, TypeError) as exc:
        raise ValidationFailedError("Provider configuration is invalid") from exc


@dataclass
class _Admitted:
    """Result of successful admission: the committed lease and the pre-I/O source fence."""

    lease: ConnectorCollectionLease
    source_fence: SourceFence


async def _admit_collection(
    session: AsyncSession,
    request: Request,
    source: ConnectorSource,
    payload: ProviderFetchRequest,
    *,
    collector_token: str,
    request_ref: CollectionRequestRef,
    scope: Scope,
    access_fence: AccessFence,
    multi_workspace_enabled: bool,
    segment_deadline: float | None,
) -> _Admitted | ProviderFetchRead:
    """Fence the connector, honour the shared cooldown and acquire the durable lease.

    Locks the connector (``lock=True``), rolls back, reads the cooldown and acquires the
    lease, all bounded by the GitHub segment deadline. A live cooldown settles the request
    and returns ``rate_limited`` without a lease. On return the session has been rolled back
    so no SQL is held across provider I/O. Raises 409 for stale fences, 503 on deadline.
    """
    deadline = segment_deadline
    try:
        if not await await_with_github_segment_deadline(
            provisioning.require_collection_fence(
                session,
                source,
                payload.source_generation,
                payload.connector_revision,
                lock=True,
                backend_revision=payload.backend_revision,
                multi_workspace_enabled=multi_workspace_enabled,
                scope=scope,
            ),
            deadline=deadline,
        ):
            raise ConflictError("Connector collection fence is stale")
        await await_with_github_segment_deadline(session.rollback(), deadline=deadline)
        now = datetime.now(UTC)
        cooldown = await await_with_github_segment_deadline(
            provider_cooldown(request, cast("str", source.provider)), deadline=deadline
        )
        if cooldown is not None and cooldown > now:
            await settle_request_exit(
                session,
                request_ref,
                outcome="failed",
                error_code="provider_rate_limited",
                retryable=True,
                provider_deadline=cooldown,
            )
            return rate_limited_read(
                scope,
                "pending_updates_only" if source.provider == "telegram" else "returned_snapshot",
                cooldown,
            )
        lease = await await_with_github_segment_deadline(
            ingestion.acquire_connector_collection(
                session,
                source_id=source.id,
                source_generation=payload.source_generation,
                connector_revision=payload.connector_revision,
                collector_token=collector_token,
                request_ref=request_ref,
                multi_workspace_enabled=multi_workspace_enabled,
                scope=scope,
            ),
            deadline=deadline,
        )
    except TimeoutError as exc:
        await session.rollback()
        raise UnavailableError("Provider collection admission exceeded its deadline") from exc
    source_fence = await sources.get_source_fence(
        session,
        source.id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        source_fence is None
        or source_fence.generation != lease.source_generation
        or source_fence.workspace_id != lease.workspace_id
        or access_fence.configuration_revision != lease.configuration_revision
    ):
        await session.rollback()
        raise ConflictError("Original collection reservation changed")
    await session.rollback()
    return _Admitted(lease=lease, source_fence=source_fence)


async def _accept_page(ctx: FetchContext, page: CollectedPage) -> ProviderFetchRead:
    """Gate a collected page on the provider cooldown, fence it and have Ingestion accept it.

    Order: ``post_collect`` hook, provider retry deadline (extends the shared cooldown,
    releases the lease and settles the request WITHOUT publishing a receipt, cursor or success
    health), ``pre_accept`` hook, then ``accept_native_collection``.
    """
    if page.post_collect is not None:
        await page.post_collect()
    if page.next_eligible_at is not None:
        eligible = await extend_provider_cooldown(
            ctx.request, cast("str", ctx.source.provider), page.next_eligible_at
        )
        # Rate limited pages never publish an ingestion receipt, cursor, or success health.
        await ingestion.release_connector_collection(
            ctx.session,
            ctx.lease,
            error_code="provider_rate_limited",
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            scope=ctx.scope,
        )
        await settle_request_exit(
            ctx.session,
            ctx.request_ref,
            outcome="failed",
            error_code="provider_rate_limited",
            retryable=True,
            provider_deadline=eligible,
        )
        return rate_limited_read(ctx.scope, page.coverage, eligible)
    if page.pre_accept is not None:
        await page.pre_accept()
    lease = ctx.lease
    native_batch = NativeCollectionBatch(
        source_id=ctx.source.id,
        source_generation=ctx.source.generation,
        connector_revision=ctx.payload.connector_revision,
        lease_token=lease.token,
        cursor_before=lease.cursor_before,
        cursor_after=page.cursor_after if page.has_cursor_after else lease.cursor_before,
        records=list(page.records),
        telegram_deliveries=(),
        telegram_raw_deliveries=(),
        coverage=page.coverage,
        github_segment=page.github_segment,
        collected_at=page.collected_at,
    )
    receipt = await ingestion.accept_native_collection(
        ctx.session,
        native_batch,
        collector_token=ctx.collector_token,
        lease=lease,
        multi_workspace_enabled=ctx.multi_workspace_enabled,
        scope=ctx.scope,
        expected_native_operation_id=None,
        request_ref=ctx.request_ref,
        expected_world_credential_operation_id=page.world_operation_id,
    )
    return provider_fetch_read(receipt, None)


def _collection_window(segment_deadline: float | None) -> AbstractAsyncContextManager[Any]:
    """Return the collection timeout: the GitHub segment deadline, else a flat 60 seconds."""
    return (
        asyncio.timeout_at(segment_deadline)
        if segment_deadline is not None
        else asyncio.timeout(60)
    )


async def _run_collection(ctx: FetchContext, collector: ProviderCollector) -> ProviderFetchRead:
    """Run the collector inside the bounded window and turn its outcome into a response.

    A page is accepted inside the window; a Telegram receipt/rate-limit outcome that was
    already accepted by the collector is settled after the window closes (replay-only pages
    with no batch settle as ``no_changes`` because nothing else settled the admission).
    """
    async with _collection_window(ctx.segment_deadline):
        outcome = await collector.collect(ctx)
        if isinstance(outcome, CollectedPage):
            return await _accept_page(ctx, outcome)
        if outcome.rate_limited is not None:
            await settle_request_exit(
                ctx.session,
                ctx.request_ref,
                outcome="failed",
                error_code="provider_rate_limited",
                retryable=True,
                provider_deadline=outcome.rate_limited.next_eligible_at,
            )
            return outcome.rate_limited
    receipt = outcome.receipt
    if receipt is None:
        raise RuntimeError("invariant violated: receipt must not be None")
    if (
        receipt.batch_id is None
    ):  # replay-only Telegram page: nothing accepted, so nothing settled it
        await settle_request_exit(ctx.session, ctx.request_ref, outcome="no_changes")
    return provider_fetch_read(receipt, outcome.eligible)


async def _release_and_settle(
    ctx: FetchContext,
    release_code: str,
    *,
    outcome_code: str,
    retryable: bool,
    provider_deadline: datetime | None = None,
) -> None:
    """Release the currently active lease, then settle the admission as failed.

    Release precedes settle in every failure path; both are best-effort and never mask
    the original outcome.
    """
    await release_failed_collection(
        ctx.session,
        ctx.active_lease[0],
        error_code=release_code,
        multi_workspace_enabled=ctx.multi_workspace_enabled,
        scope=ctx.scope,
    )
    await settle_request_exit(
        ctx.session,
        ctx.request_ref,
        outcome="failed",
        error_code=outcome_code,
        retryable=retryable,
        provider_deadline=provider_deadline,
    )


async def run_provider_fetch(
    session: AsyncSession,
    request: Request,
    source: ConnectorSource,
    payload: ProviderFetchRequest,
    *,
    collector_token: str,
    request_ref: CollectionRequestRef,
    scope: Scope,
    access_fence: AccessFence,
) -> ProviderFetchRead:
    """Collect one bounded native page for an already authorized Source.

    The caller (the route) authenticated the collector bearer, loaded the Source and checked
    the Ingestion module gate. This validates the Source, admits the collection (fence,
    cooldown, durable lease), rolls back, then runs the registered ``ProviderCollector`` and
    maps provider cooldown/busy/HTTP/transport failures to release+settle exits. The
    GitHub permit is released in ``finally``. ``HTTPException``/``DomainError`` re-raise
    unchanged after cleanup; transport errors become 503 "Provider collection failed".
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    segment_deadline = (
        asyncio.get_running_loop().time() + 60 if source.provider == "github" else None
    )
    validate_native_source(source)
    admitted = await _admit_collection(
        session,
        request,
        source,
        payload,
        collector_token=collector_token,
        request_ref=request_ref,
        scope=scope,
        access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled,
        segment_deadline=segment_deadline,
    )
    if isinstance(admitted, ProviderFetchRead):
        return admitted
    ctx = FetchContext(
        session=session,
        request=request,
        source=source,
        payload=payload,
        collector_token=collector_token,
        request_ref=request_ref,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=admitted.source_fence,
        lease=admitted.lease,
        active_lease=[admitted.lease],
        segment_deadline=segment_deadline,
    )
    collector = resolve_collector(source.provider)
    try:
        await collector.prepare(ctx)
    except BaseException:
        await release_failed_collection(
            session,
            ctx.active_lease[0],
            error_code="provider_collection_failed",
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
        raise
    try:
        return await _run_collection(ctx, collector)
    except ProviderAdmissionBusy:
        eligible = datetime.now(UTC) + timedelta(seconds=1)
        await _release_and_settle(
            ctx,
            "provider_admission_busy",
            outcome_code="provider_admission_busy",
            retryable=True,
            provider_deadline=eligible,
        )
        return rate_limited_read(scope, "returned_snapshot", eligible)
    except ProviderRateLimited as exc:
        deadline = await extend_provider_cooldown(
            request, cast("str", source.provider), exc.next_eligible_at
        )
        await _release_and_settle(
            ctx,
            "provider_rate_limited",
            outcome_code="provider_rate_limited",
            retryable=True,
            provider_deadline=deadline,
        )
        return rate_limited_read(
            scope,
            "pending_updates_only" if source.provider == "telegram" else "returned_snapshot",
            deadline,
        )
    except (HTTPException, DomainError) as exc:
        code, retryable = request_failure(exc)
        await release_failed_collection(
            session,
            ctx.active_lease[0],
            error_code=collection_error_code(exc),
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
        await settle_request_exit(
            session, ctx.request_ref, outcome="failed", error_code=code, retryable=retryable
        )
        raise
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        code, retryable = request_failure(exc)
        await release_failed_collection(
            session,
            ctx.active_lease[0],
            error_code=collection_error_code(exc),
            multi_workspace_enabled=multi_workspace_enabled,
            scope=scope,
        )
        await settle_request_exit(
            session, ctx.request_ref, outcome="failed", error_code=code, retryable=retryable
        )
        raise UnavailableError("Provider collection failed") from exc
    except BaseException:
        await _release_and_settle(
            ctx,
            "provider_collection_failed",
            outcome_code="provider_collection_failed",
            retryable=True,
        )
        raise
    finally:
        if ctx.github_permit_token is not None:
            await release_github_network_permit(request, ctx.github_permit_token)
