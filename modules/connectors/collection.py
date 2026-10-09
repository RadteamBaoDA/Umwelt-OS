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
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Literal, cast
from urllib.parse import urldefrag, urljoin
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import provider_terms, provisioning, quota, scheduler
from modules.connectors import public as connectors
from modules.connectors.collection_schemas import CollectionAdmissionRead, CollectionRequestRef
from modules.connectors.models import ConnectorCollectionRequest, ConnectorProvisioning
from modules.connectors.provider_specs import get_provider_spec
from modules.connectors.providers import rest
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import (
    CollectionState,
    CollectionStateUpdate,
    IngestionRecord,
    NativeCollectionBatch,
    ReceiveBatch,
)
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceFence

RENEW_EVERY_SECONDS = 20
PAYLOAD_RATE_LIMIT_BACKOFF = timedelta(minutes=15)  # body-level throttling carries no Retry-After; coarse until observed
PURE_MAX_BYTES = {"ecb": 256 * 1024}  # every other P3 body is capped at macro.MAX_BODY_BYTES
PURE_PROVIDERS = ("world_bank", "frankfurter", "ecb", "binance", "alternative_me", "usgs", "coinpaprika")  # coingecko needs a key slot
KEYED_PROVIDERS = {"coingecko": "x-cg-demo-api-key"}  # provider -> fixed header; key in connector_rest_credentials
SIMPLE_NATIVE = frozenset({"youtube", "arxiv", "huggingface", "github_releases", "alpha_vantage", "open_meteo"})
REFUSED_HERE: frozenset[str] = frozenset()  # providers that must never run here (GitHub/Telegram are native now)
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
ADAPTERS: dict[str, Adapter] = {}  # provider id -> explicit adapter (HN and later free providers register here)


class SendGate:
    """Authorize and debit every physical send; commit before the wire, never refund.

    Providers call it before a send and again from an httpx trace at header time. The first call
    of a pair revalidates and debits (new ``send_sequence`` => new ledger row); the paired second
    call only revalidates. Providers abort the whole collection on any send failure, and a gate
    lives for one attempt, so an unpaired armed state cannot leak into a later send.
    """

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], attempt: Attempt, *, settings: Settings,
        deadline: float, quota_provider: str | None, fingerprint: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self._factory, self._a, self._settings = factory, attempt, settings
        self._deadline, self._quota_provider, self._fingerprint = deadline, quota_provider, fingerprint
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
            return fingerprint

        self._fingerprint = bound

    async def fetch_bytes(self, url: str, **kwargs: Any) -> rest.Fetched:
        """Gated, pinned GET for generic adapters (one debited/fenced send per call)."""
        return await rest.fetch_bounded(url, before_send=self, **kwargs)

    async def _authorize(self) -> None:
        a = self._a
        if time.monotonic() > self._deadline:
            raise TimeoutError
        async with self._factory() as session:
            if not await scheduler.renew_admission(
                session, a.ref.request_id, a.ref.admission_token, multi_workspace_enabled=a.multi,
            ):
                raise CollectionFenceLost("admission_lost")
            try:
                source = await sources.get_connector_source(
                    session, a.source.id, scope=a.scope, multi_workspace_enabled=a.multi)
                if source is None:
                    raise CollectionFenceLost("source_inactive", "source_inactive")
                revision = await provider_terms.require_terms_eligible(session, source)
            except HTTPException as exc:
                raise TermsIneligible from exc
            finally:
                await session.rollback()
            if revision != a.terms_revision:
                raise CollectionFenceLost("terms_changed", "revision_changed")
            if source.provider == "alpha_vantage" and self.captured_operation is not None:
                await self._check_world_credential(session, source)
            if source.provider in KEYED_PROVIDERS:
                await self._check_credential_revision(session, source)

    async def _check_credential_revision(self, session: AsyncSession, source: ConnectorSource) -> None:
        """Owner key re-entry bumps ``credential_revision``; any send admitted before it is lost."""
        a = self._a
        try:
            source_fence, row, _ = await provisioning.lock_connector(
                session, source.id, scope=a.scope, multi_workspace_enabled=a.multi,
                expected_access_fence=a.access_fence)
            if source_fence is None or row is None or row.credential_revision != a.credential_revision:
                raise CollectionFenceLost("credential_changed", "revision_changed")
        finally:
            await session.rollback()

    async def _check_world_credential(self, session: AsyncSession, source: ConnectorSource) -> None:
        a = self._a
        try:
            source_fence, row, _ = await provisioning.lock_connector(
                session, source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=a.scope,
                multi_workspace_enabled=a.multi, expected_access_fence=a.access_fence)
            if source_fence is None or row is None or not await connectors.validate_world_credential_operation_in_uow(
                session, source.id, source_generation=source.generation, connector_revision=a.connector_revision,
                expected_operation_id=self.captured_operation,  # type: ignore[arg-type]
                scope=a.scope, multi_workspace_enabled=a.multi, access_fence=a.access_fence,
                source_fence=source_fence,
            ):
                raise CollectionFenceLost("credential_changed", "revision_changed")
        finally:
            await session.rollback()

    async def _debit(self) -> None:
        if self._quota_provider is None:
            return
        a = self._a
        fingerprint = await self._fingerprint() if self._fingerprint is not None else None
        self.sends += 1
        async with self._factory() as session:
            try:
                await quota.debit_send(
                    session, provider_id=self._quota_provider, workspace_id=a.scope.workspace_id,
                    request_id=a.ref.request_id, admission_token=a.ref.admission_token, attempt=a.attempt,
                    send_sequence=self.sends, request_target=self._quota_provider, credential=fingerprint)
                await session.commit()  # the debit is durable BEFORE bytes leave the process
            except BaseException:
                await session.rollback()
                raise


def _alpha_fingerprint(run_state: dict[str, Any]) -> Callable[[], Awaitable[str | None]]:
    async def fingerprint() -> str | None:
        """Deployment-keyed fingerprint of the Alpha key so one key shares one budget across workspaces."""
        from modules.connectors.providers.world_data import get_alpha_vantage_key

        if "alpha" in run_state:
            return str(run_state["alpha"])
        a: Attempt = run_state["attempt"]
        async with run_state["factory"]() as session:
            credential = await get_alpha_vantage_key(
                session, a.source, run_state["settings"], scope=a.scope, multi_workspace_enabled=a.multi,
                access_fence=a.access_fence, source_fence=a.source_fence)
        if credential is None:
            raise CollectionFenceLost("credential_changed", "revision_changed")
        run_state["alpha"] = quota.credential_fingerprint(
            credential[0], deployment_key=run_state["settings"].connector_credential_encryption_key.get_secret_value())
        return str(run_state["alpha"])

    return fingerprint


async def _load_attempt(
    factory: async_sessionmaker[AsyncSession], admission: CollectionAdmissionRead, multi: bool,
) -> Attempt | None:
    """Reload scope, source and fences from the durable request; None when it is no longer ours."""
    async with factory() as session:
        request = await session.get(ConnectorCollectionRequest, admission.request_id)
        if request is None or request.status != "running" or request.active_admission_token != admission.admission_token:
            await session.rollback()
            return None
        scope = scheduler._request_scope(request)
        captured: Snapshot = (request.source_generation, request.connector_revision, request.backend_revision)
        terms_revision, access_revision = request.terms_revision, request.access_configuration_revision
        source: ConnectorSource | None = None
        source_fence: SourceFence | None = None
        access_fence: AccessFence | None = None
        row: ConnectorProvisioning | None = None
        denied = "permission_lost"
        for resolve in (1, 2):  # a 409 is re-resolved once, then becomes terminal stale_scope
            try:
                await connectors._connector_access(session, scope=scope, multi_workspace_enabled=multi)
                source = await sources.get_connector_source(session, request.source_id, scope=scope, multi_workspace_enabled=multi)
                source_fence = await sources.get_source_fence(session, request.source_id, scope=scope, multi_workspace_enabled=multi)
                access_fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi)
                row = await session.get(ConnectorProvisioning, request.source_id) if source is not None else None
                break
            except HTTPException as exc:
                source = None
                if exc.status_code == 409 and resolve == 1:
                    await session.rollback()
                    continue
                denied = scheduler.denial_code(exc)
                break
        await session.rollback()
    if source is None or source_fence is None or access_fence is None or row is None:
        await _settle(factory, admission, outcome="cancelled", error_code=denied)
        return None
    current: Snapshot = (source.generation, row.desired_revision, row.backend_revision)
    if not snapshot_matches(captured, current) or (
        access_revision is not None and access_revision != access_fence.configuration_revision
    ):
        await _settle(factory, admission, outcome="cancelled", error_code="revision_changed")
        return None
    return Attempt(
        ref=CollectionRequestRef(request_id=admission.request_id, admission_token=admission.admission_token),
        attempt=admission.attempt, scope=scope, multi=multi, source=source, access_fence=access_fence,
        source_fence=source_fence, terms_revision=terms_revision, connector_revision=row.desired_revision,
        authenticated=(row.desired_configuration or {}).get("auth_method") not in (None, "none"),
        credential_revision=request.credential_revision,
    )


async def _settle(
    factory: async_sessionmaker[AsyncSession], admission: CollectionAdmissionRead, **kwargs: Any,
) -> bool:
    async with factory() as session:
        return await scheduler.settle_admission(session, admission.request_id, admission.admission_token, **kwargs)


async def _heartbeat(
    factory: async_sessionmaker[AsyncSession], attempt: Attempt, main: "asyncio.Task[Any]", lost: asyncio.Event,
    interval: float = RENEW_EVERY_SECONDS,
) -> None:
    """Renew the slot independently of response streaming; a failed renewal stops the attempt."""
    while True:
        await asyncio.sleep(interval)
        async with factory() as session:
            ok = await scheduler.renew_admission(
                session, attempt.ref.request_id, attempt.ref.admission_token, multi_workspace_enabled=attempt.multi)
        if not ok:
            lost.set()
            main.cancel()
            return


async def _record_rate_limit(run: Run, deadline: datetime) -> None:
    """Persist a provider 429/Retry-After on every applicable window; never shortens one."""
    spec = get_provider_spec(run.attempt.source.provider)
    if spec is None:
        return
    fingerprint = await run.gate._fingerprint() if run.gate._fingerprint is not None else None
    async with run.factory() as session:
        await quota.defer_provider(session, provider_id=spec.id, until=deadline, credential=fingerprint)
        await session.commit()


# ---------------------------------------------------------------- generic adapters

def _records(raw: list[dict[str, Any]]) -> list[IngestionRecord]:
    collected_at = datetime.now(UTC)
    return [IngestionRecord.model_validate({**item, "collected_at": collected_at}) for item in raw]


async def _accept_generic(
    run: Run, records: list[dict[str, Any]], cursor_before: str | None, cursor_after: str | None,
    update: CollectionStateUpdate | None = None,
) -> None:
    """Hand a generic collection to the ingestion owner; empty means a no-change settlement."""
    a = run.attempt
    async with run.factory() as session:
        await lock_access_fence(session, scope=a.scope, expected=a.access_fence, multi_workspace_enabled=a.multi)
        if not records:
            await ingestion.accept_collection_no_changes(
                session, source_id=a.source.id, source_generation=a.source.generation,
                connector_revision=a.connector_revision, request_ref=a.ref, scope=a.scope,
                multi_workspace_enabled=a.multi, state_update=update)
            return
        batch = ReceiveBatch(
            source_id=a.source.id, source_generation=a.source.generation, connector_revision=a.connector_revision,
            batch_key=f"connector-request:{a.ref.request_id}", cursor_before=cursor_before, cursor_after=cursor_after,
            records=_records(records))
        await ingestion.receive_connector_batch(
            session, batch, None, scope=a.scope, multi_workspace_enabled=a.multi, request_ref=a.ref,
            state_update=update)


async def _state(run: Run) -> CollectionState:
    a = run.attempt
    async with run.factory() as session:
        state = await ingestion.get_collection_state(
            session, a.source.id, scope=a.scope, multi_workspace_enabled=a.multi)
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


async def _accept_walk(run: Run, state: CollectionState, walk: Walk, resume: rest.Checkpoint | None) -> None:
    """Commit a walk: complete => cursor + validators advance; partial => checkpoint only.

    The checkpoint, validators, cursor, batch, receipt, request outcome and slot release share the
    ingestion owner's single commit, so a crash replays from the previous checkpoint exactly.
    """
    cursor = state.cursor
    if walk.not_modified:  # 304: cursor, validators and last-good observations stay untouched
        await _accept_generic(run, [], cursor, cursor, CollectionStateUpdate(continuation_state=None))
        return
    if walk.next_url is None:
        cursor_after = walk.max_time or cursor
        update = CollectionStateUpdate(
            continuation_state=None, update_validators=True, etag=walk.etag, last_modified=walk.last_modified,
            cursor_after=cursor_after if cursor_after != cursor else None)
        await _accept_generic(run, walk.records, cursor, cursor_after, update)
        return
    checkpoint = rest.Checkpoint(
        url=walk.next_url, revision=run.attempt.connector_revision, cursor=cursor, max_time=walk.max_time,
        etag=walk.etag, last_modified=walk.last_modified,
        ids=[rest.id_hash(str(r["provider_id"])) for r in walk.records] or (resume.ids if resume else []))
    await _accept_generic(
        run, walk.records, cursor, cursor,
        CollectionStateUpdate(continuation_state=checkpoint.encode(), coverage="partial"))


async def _rest_secret(run: Run) -> tuple[str, str]:
    """Load the owner-entered header credential for this exact generation/revision, or require re-entry."""
    from modules.connectors.credentials import CredentialEncryptionUnavailable, decrypt_rest_secret
    from modules.connectors.models import ConnectorRestCredential

    a = run.attempt
    async with run.factory() as session:
        credential = await session.get(ConnectorRestCredential, a.source.id)
        await session.rollback()
    if (
        credential is None or credential.state != "ready" or credential.encrypted_secret is None
        or credential.source_generation != a.source.generation
        or credential.configuration_revision != a.connector_revision
    ):
        raise rest.ProviderHttpError(401)  # owner re-entry required
    try:
        secret = decrypt_rest_secret(
            run.settings.connector_credential_encryption_key.get_secret_value(), credential.encrypted_secret,
            source_id=a.source.id, operation_id=credential.operation_id, source_generation=credential.source_generation,
            configuration_revision=credential.configuration_revision, header_name=credential.header_name)
    except CredentialEncryptionUnavailable as exc:
        raise rest.ProviderHttpError(401) from exc
    return credential.header_name, secret


def _with_header(
    send: Callable[..., Awaitable[rest.Fetched]], header: str, secret: str,
) -> Callable[..., Awaitable[rest.Fetched]]:
    """Wrap a gated send so the owner-entered header rides every same-origin request."""
    async def fetch(url: str, headers: dict[str, str] | None = None, **kwargs: Any) -> rest.Fetched:
        return await send(url, headers={**(headers or {}), header: secret}, **kwargs)

    return fetch


async def _run_rest(run: Run) -> None:
    from modules.connectors.registry import configuration

    a = run.attempt
    fetch: Callable[..., Awaitable[rest.Fetched]] = run.gate.fetch_bytes
    if a.authenticated:
        fetch = _with_header(run.gate.fetch_bytes, *await _rest_secret(run))
    config = configuration(a.source)
    state = await _state(run)
    resume = rest.Checkpoint.decode(
        state.continuation_state, revision=a.connector_revision, cursor=state.cursor, origin_url=str(config.url))
    collected = await rest.collect_rest(
        config, state.cursor, fetch=fetch, resume=resume,
        conditional=None if resume else _conditional(state, a.connector_revision))
    await _accept_walk(run, state, Walk(
        collected.records, collected.continuation_url, collected.max_time, collected.etag,
        collected.last_modified, collected.not_modified), resume)


class _NotModified(Exception):  # control-flow signal for HTTP 304
    """The first feed page answered 304."""


async def _run_rss(run: Run) -> None:
    from modules.connectors.n8n import read_rss
    from modules.connectors.registry import configuration

    a = run.attempt
    config = configuration(a.source)
    state = await _state(run)
    resume = rest.Checkpoint.decode(
        state.continuation_state, revision=a.connector_revision, cursor=state.cursor, origin_url=None)
    conditional = None if resume else _conditional(state, a.connector_revision)
    stats: dict[str, Any] = {}
    first: dict[str, Any] = {"pending": True}

    async def fetch(url: str) -> bytes:
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
        result = await read_rss(resume.url if resume else str(config.feed_url), state.cursor, fetch=fetch, stats=stats)
    except _NotModified:
        await _accept_walk(run, state, Walk([], None, None, None, None, not_modified=True), resume)
        return
    if stats.get("truncated"):
        raise rest.CollectionIncomplete  # unread items remain inside a page: no exact continuation exists
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
    max_time = rest.later(resume.max_time if resume else None, newest if newest != state.cursor else None)
    await _accept_walk(run, state, Walk(
        records, stats.get("resume_url"), max_time,
        resume.etag if resume else first.get("etag"), resume.last_modified if resume else first.get("lm")), resume)


# ---------------------------------------------------------------- native web + MCP (generic sources)

class _PageText(HTMLParser):
    """Visible text and anchors of one HTML page (stdlib; no optional crawl extra needed)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag == "a":
            self.links.extend(v for k, v in attrs if k == "href" and v)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


async def _run_web(run: Run) -> None:
    """Static web read: same-origin BFS through the gated, pinned transport (browser mode stays on crawl.py)."""
    from modules.connectors.registry import configuration, normalize

    config = configuration(run.attempt.source)
    start = str(config.url)
    state = await _state(run)
    origin = rest._origin(start)
    queue, visited, records = [(start, 0)], {start}, []
    spent = 0
    while queue and len(records) < config.max_pages:
        url, depth = queue.pop(0)
        fetched = await run.gate.fetch_bytes(
            url, headers={"Accept": "text/html,application/xhtml+xml"},
            max_bytes=min(rest.MAX_PAGE_BYTES, rest.MAX_TRANSPORT_BYTES - spent), timeout=config.timeout_seconds)
        body = _require_body(fetched)
        spent += len(body)
        page = _PageText()
        page.feed(body.decode("utf-8", errors="replace"))
        records.append(normalize({
            "provider_id": url, "content": "\n".join(page.parts)[:200_000],
            "observed_at": datetime.now(UTC).isoformat(), "metadata": {"url": url}}))
        if depth < config.max_depth:
            for href in page.links:
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

    async def authorized() -> bool:
        await run.gate()  # raises CollectionFenceLost / TermsIneligible / TimeoutError; never returns False
        return True

    for call in config.calls:
        try:
            read = await tools.read_collection_capability(
                runtime, connection_id=config.connection_id, grant_id=call.grant_id, source_id=a.source.id,
                source_generation=a.source.generation, arguments=call.arguments, authorize_extra=authorized,
                scope=a.scope, multi_workspace_enabled=a.multi)
            records.extend(mcp_collection.normalize(read, call.arguments, collected_at))
        except (CollectionFenceLost, TermsIneligible, TimeoutError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001  # one failing call never blocks the others (as in mcp.collect)
            failed += 1
    if failed == len(config.calls):
        raise mcp_collection.McpCollectionError("mcp_collection_failed")
    await _accept_generic(
        run, records[:mcp_collection.MAX_RECORDS], state.cursor, "mcp:" + collected_at.isoformat())


# ---------------------------------------------------------------- simple native providers

async def _run_native(run: Run) -> None:
    """Collect one registered snapshot provider through the reserved native lease + atomic receipt."""
    a, provider = run.attempt, run.attempt.source.provider
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    gate = run.gate
    async with run.factory() as session:
        if provider in {"youtube", "arxiv"}:
            from modules.connectors.providers.feed_catalog import collect_provider_feed

            page = await collect_provider_feed(
                a.source, collected_at=collected_at, session_factory=run.factory, before_request=gate)
        elif provider == "huggingface":
            from modules.connectors.providers.research import collect_huggingface_models

            page = await collect_huggingface_models(a.source, collected_at=collected_at, before_request=gate)
        elif provider == "github_releases":
            from modules.connectors.providers.social import collect_github_releases

            page = await collect_github_releases(a.source, collected_at=collected_at, before_request=gate)
        else:
            from modules.connectors.providers.world_data import collect_world_data

            page = await collect_world_data(
                a.source, collected_at=collected_at, settings=run.settings, session=session, redis=_Unmetered(),
                scope=a.scope, multi_workspace_enabled=a.multi, access_fence=a.access_fence,
                source_fence=a.source_fence, before_request=gate)
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
            session, source_id=a.source.id, source_generation=a.source.generation,
            connector_revision=a.connector_revision, collector_token=None, scope=a.scope,
            multi_workspace_enabled=a.multi, request_ref=a.ref)
    run.extras["lease"] = lease
    return lease


async def _accept_native(
    run: Run, lease: Any, records: Sequence[IngestionRecord], coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"],
    collected_at: datetime, state_update: CollectionStateUpdate | None = None,
) -> None:
    """Hand a leased native page (possibly empty = no changes) to the ingestion owner."""
    a, gate = run.attempt, run.gate
    batch = NativeCollectionBatch(
        source_id=a.source.id, source_generation=a.source.generation, connector_revision=a.connector_revision,
        lease_token=lease.token, cursor_before=lease.cursor_before, cursor_after=lease.cursor_before,
        records=list(records), telegram_deliveries=(), telegram_raw_deliveries=(),
        coverage=coverage, github_segment=None, collected_at=collected_at)
    async with run.factory() as session:
        await ingestion.accept_native_collection(
            session, batch, collector_token=None, lease=lease, scope=a.scope, multi_workspace_enabled=a.multi,
            expected_native_operation_id=None,
            expected_world_credential_operation_id=gate.captured_operation if a.source.provider == "alpha_vantage" else None,
            request_ref=a.ref, state_update=state_update)
    run.extras["accepted"] = True


class _Unmetered:
    """Redis stand-in: PostgreSQL ledger debits are the quota authority; Redis may only pre-reject."""

    async def eval(self, *_args: object) -> int:
        return 1


# ---------------------------------------------------------------- P2/P3 free providers (fixed endpoints)

def _payload(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a pure mapper and translate ``ProviderPayloadError.kind`` into executor failure signals."""
    from modules.connectors.providers.macro import ProviderPayloadError

    try:
        return fn(*args)
    except ProviderPayloadError as exc:
        if exc.kind == "rate_limited":
            raise connectors.ProviderRateLimited(datetime.now(UTC) + PAYLOAD_RATE_LIMIT_BACKOFF) from exc
        if exc.kind == "incomplete":
            raise rest.CollectionIncomplete from exc
        if exc.kind in {"credential", "entitlement"}:
            raise rest.ProviderHttpError(401) from exc
        raise  # "invalid": ValueError -> provider_response_invalid


def _coverage(records: Sequence[IngestionRecord]) -> Literal["returned_snapshot", "truncated"]:
    """Page coverage must equal the records' coverage (ingress rejects a mismatch)."""
    return "truncated" if any(r.metadata["provider_record"]["coverage"] == "truncated" for r in records) else "returned_snapshot"


async def _get_body(run: Run, request: Any, *, max_bytes: int, timeout: float | None = None) -> rest.Fetched:
    """One gated send (terms + quota + fences before the wire); no redirects, bounded bytes."""
    extra = {} if timeout is None else {"timeout": timeout}
    return await run.gate.fetch_bytes(request.url, headers=request.headers or None, max_bytes=max_bytes, **extra)


def _require_body(fetched: rest.Fetched) -> bytes:
    if fetched.body is None:  # unconditional GET answered 304: not a usable page
        raise rest.ProviderHttpError(304)
    return fetched.body


async def _run_pure(run: Run) -> None:
    """P3 measurement providers: one fixed request, mapped by ``map_pure_provider_body`` (no world_data for news)."""
    from modules.connectors.providers.macro import MAX_BODY_BYTES
    from modules.connectors.providers.world_data import PURE_ADAPTERS, map_pure_provider_body

    provider = str(run.attempt.source.provider)
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    fetched = await _get_body(run, PURE_ADAPTERS[provider].request(), max_bytes=PURE_MAX_BYTES.get(provider, MAX_BODY_BYTES))
    records = _payload(map_pure_provider_body, provider, _require_body(fetched), collected_at)
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _keyed_secret(run: Run) -> str:
    """Owner key for this exact generation/revision, read at send time; typed ``credential_missing`` if unusable."""
    header = KEYED_PROVIDERS[str(run.attempt.source.provider)]
    try:
        stored_header, secret = await _rest_secret(run)
    except rest.ProviderHttpError as exc:
        raise CredentialMissing from exc
    if stored_header != header:
        raise CredentialMissing
    run.gate.bind_credential(quota.credential_fingerprint(
        secret, deployment_key=run.settings.connector_credential_encryption_key.get_secret_value()))
    return secret


async def _run_coingecko(run: Run) -> None:
    """CoinGecko Demo: key read first (no lease, no send without it), header-only, one gated send."""
    from modules.connectors.providers.crypto import coingecko_request
    from modules.connectors.providers.macro import MAX_BODY_BYTES
    from modules.connectors.providers.world_data import map_pure_provider_body

    request = _payload(coingecko_request, await _keyed_secret(run))
    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    fetched = await _get_body(run, request, max_bytes=MAX_BODY_BYTES)
    records = _payload(map_pure_provider_body, "coingecko", _require_body(fetched), collected_at)
    await _accept_native(run, lease, records, _coverage(records), collected_at)


async def _run_feed(run: Run) -> None:
    """P2 RSS/Atom presets: replay stored validators (same URL + revision), 304 keeps the last good records."""
    from modules.connectors.providers import news

    a, provider = run.attempt, str(run.attempt.source.provider)
    collected_at, revision = datetime.now(UTC), str(a.connector_revision)
    state = await _state(run)
    stored = news.FeedValidators(
        news.FEED_URLS[provider], revision, state.etag, state.last_modified,
    ) if state.validators_revision == a.connector_revision else None
    lease = await _lease(run)
    fetched = await _get_body(run, news.feed_request(provider, stored, revision), max_bytes=news.MAX_FEED_BYTES)
    result = _payload(news.map_feed_response, provider, 200 if fetched.body is not None else 304, fetched.body or b"", collected_at)
    if result.not_modified:
        await _accept_native(run, lease, (), "returned_snapshot", collected_at)
        return
    captured = news.capture_validators(
        provider, {"etag": fetched.etag or "", "last-modified": fetched.last_modified or ""}, revision)
    update = CollectionStateUpdate(
        update_validators=True, etag=captured.etag if captured else None,
        last_modified=captured.last_modified if captured else None)
    await _accept_native(run, lease, result.records, _coverage(result.records), collected_at, update)


async def _run_hn(run: Run) -> None:
    """Hacker News: one id-list send plus at most ``HN_MAX_ITEMS`` sequential item sends, each debited."""
    from modules.connectors.providers import news

    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    top = _require_body(await _get_body(run, news.hn_top_request(), max_bytes=64 * 1024))
    ids = _payload(news.parse_hn_top_ids, top)
    bodies: dict[int, bytes | None] = {}
    for item_id in ids:
        try:
            bodies[item_id] = (await _get_body(run, news.hn_item_request(item_id), max_bytes=64 * 1024)).body
        except rest.ProviderHttpError as exc:
            if exc.status_code != 404:
                raise
            bodies[item_id] = None  # removed item: skipped and counted by the mapper
    result = _payload(news.map_hn_stories, ids, bodies, collected_at)
    run.extras["skipped"] = result.skipped
    await _accept_native(run, lease, result.records, "returned_snapshot", collected_at)


async def _run_gdelt(run: Run) -> None:
    """Experimental GDELT: hard 8 s timeout; on timeout nothing is accepted so the last good records stay."""
    from modules.connectors.providers.gdelt import GDELT_TIMEOUT_SECONDS, gdelt_request
    from modules.connectors.providers.macro import MAX_BODY_BYTES
    from modules.connectors.providers.news import map_news_body

    collected_at = datetime.now(UTC)
    lease = await _lease(run)
    fetched = await _get_body(run, gdelt_request(), max_bytes=MAX_BODY_BYTES, timeout=GDELT_TIMEOUT_SECONDS)
    records = _payload(map_news_body, "gdelt_economy", _require_body(fetched), collected_at)
    await _accept_native(run, lease, records, _coverage(records), collected_at)


ADAPTERS.update({
    **dict.fromkeys(PURE_PROVIDERS, _run_pure),
    "coingecko": _run_coingecko,
    "bbc_world": _run_feed, "vnexpress_business": _run_feed, "hn_top": _run_hn, "gdelt_economy": _run_gdelt,
})


# ---------------------------------------------------------------- GitHub / Telegram (proof-bearing)

async def _credential_fence(
    run: Run, *, github: tuple[Any, tuple[UUID, int]] | None = None, telegram: Any = None,
) -> None:
    """Before every send/accept: locked, rolled-back recheck of credential revision, grant/bot proof and lease."""
    from modules.connectors.models import GithubOAuthGrant

    a, lease = run.attempt, run.extras["lease"]
    async with run.factory() as session:
        try:
            current, row, _ = await provisioning.lock_connector(
                session, a.source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=a.scope,
                multi_workspace_enabled=a.multi, expected_access_fence=a.access_fence)
            if (current != a.source_fence or row is None or row.credential_revision != a.credential_revision
                    or not await provisioning.require_collection_fence(
                        session, a.source, lease.source_generation, lease.connector_revision,
                        scope=a.scope, multi_workspace_enabled=a.multi)):
                raise CollectionFenceLost("credential_changed", "revision_changed")
            await ingestion.lock_source_credentials_in_uow(
                session, a.source.id, scope=a.scope, multi_workspace_enabled=a.multi,
                access_fence=a.access_fence, source_fence=a.source_fence)
            if github is not None:
                fence, binding = github
                now_fence = await connectors.lock_github_binding_fence_in_uow(
                    session, a.source.id, source_generation=lease.source_generation,
                    connector_revision=lease.connector_revision, scope=a.scope, multi_workspace_enabled=a.multi,
                    access_fence=a.access_fence, source_fence=a.source_fence)
                grant = await session.get(GithubOAuthGrant, a.source.id, populate_existing=True)
                if now_fence != fence or grant is None or binding != (grant.operation_id, grant.token_revision):
                    raise CollectionFenceLost("credential_changed", "revision_changed")
            if telegram is not None:
                now_native = await connectors.get_native_credential_snapshot(
                    session, a.source.id, source_generation=lease.source_generation,
                    connector_revision=lease.connector_revision, scope=a.scope, multi_workspace_enabled=a.multi)
                if now_native != telegram or telegram.access_fence != a.access_fence:
                    raise CollectionFenceLost("credential_changed", "revision_changed")
            if not await ingestion.validate_connector_collection_in_uow(
                session, lease, collector_token=None, scope=a.scope, multi_workspace_enabled=a.multi,
                access_fence=a.access_fence, source_fence=a.source_fence, request_ref=a.ref,
            ):
                raise CollectionFenceLost("admission_lost")
        finally:
            await session.rollback()


async def _accept_proof(
    run: Run, lease: Any, *, records: Sequence[IngestionRecord],
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"], cursor_after: str | None,
    collected_at: datetime, github_segment: Any = None, telegram: Sequence[Any] = (), raw: Sequence[Any] = (),
    native_operation_id: UUID | None = None,
) -> None:
    """Hand a proof-bearing page to the ingestion owner (one commit: batch, cursor, request outcome, slot)."""
    a = run.attempt
    batch = NativeCollectionBatch(
        source_id=a.source.id, source_generation=lease.source_generation, connector_revision=lease.connector_revision,
        lease_token=lease.token, cursor_before=lease.cursor_before, cursor_after=cursor_after,
        records=list(records), telegram_deliveries=tuple(telegram), telegram_raw_deliveries=tuple(raw),
        coverage=coverage, github_segment=github_segment, collected_at=collected_at)
    async with run.factory() as session:
        await ingestion.accept_native_collection(
            session, batch, collector_token=None, lease=lease, scope=a.scope, multi_workspace_enabled=a.multi,
            expected_native_operation_id=native_operation_id, expected_world_credential_operation_id=None,
            request_ref=a.ref)
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
            session, source_id=source.id, source_generation=lease.source_generation,
            connector_revision=lease.connector_revision, multi_workspace_enabled=a.multi, scope=a.scope)
    async with run.factory() as session:
        try:
            await provisioning.lock_connector(
                session, source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=a.scope,
                multi_workspace_enabled=a.multi, expected_access_fence=a.access_fence)
            fence = await connectors.lock_github_binding_fence_in_uow(
                session, source.id, source_generation=lease.source_generation,
                connector_revision=lease.connector_revision, scope=a.scope, multi_workspace_enabled=a.multi,
                access_fence=a.access_fence, source_fence=a.source_fence)
            if fence is None:
                raise CredentialMissing
            grant = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source.id).with_for_update())
            if (grant is None or grant.state != "ready" or grant.encrypted_tokens is None
                    or grant.source_generation != lease.source_generation
                    or grant.configuration_revision != lease.connector_revision
                    or grant.expires_at is None or grant.expires_at <= datetime.now(UTC)):
                raise CredentialMissing
            binding = (grant.operation_id, grant.token_revision)
            pair = github_oauth._open_token_cipher(
                run.settings.connector_credential_encryption_key.get_secret_value(), grant.encrypted_tokens,
                source.id, grant.operation_id, grant.source_generation, grant.configuration_revision)
        finally:
            await session.rollback()
    token = pair.get("access_token")
    if not isinstance(token, str) or not token:
        raise CredentialMissing
    config = project_github_source_config(source.configuration)
    redis, permit = run.ctx.get("redis"), secrets.token_urlsafe(24)
    held = False

    async def before_send() -> None:
        nonlocal held
        await run.gate()
        await _credential_fence(run, github=(fence, binding))
        if redis is not None:  # cluster-wide single GitHub network slot; expiry outlives the run deadline
            if not await redis.set("connectors:provider:active:github", permit, nx=True, ex=75):
                raise connectors.ProviderRateLimited(datetime.now(UTC) + timedelta(seconds=1))
            held = True

    try:
        proof = await collect_github_segment(
            config, token, fence=fence, cursor_before=lease.cursor_before, collected_at=collected_at,
            before_request=before_send,
            hint_claim=GitHubHintClaimProof.model_validate(claim.model_dump(mode="python")) if claim is not None else None)
    finally:
        if held and redis is not None:
            with suppress(Exception):
                await redis.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
                    1, "connectors:provider:active:github", permit)
    validated = validate_github_segment(fence, config, lease.cursor_before, proof, collected_at=collected_at)
    await _credential_fence(run, github=(fence, binding))
    await _accept_proof(
        run, lease, records=validated.records, coverage=validated.coverage, cursor_after=validated.cursor_after,
        collected_at=collected_at, github_segment=proof)


async def _run_telegram(run: Run) -> None:
    """Telegram: bot token read under locks, getUpdates fenced per send; one accepted page per request.

    Pages that only replay already-ingested updates advance the offset (fresh lease each time, at most
    five pages); the first page with new work is accepted and settles the request.
    """
    from modules.connectors.credentials import decrypt_native_token
    from modules.connectors.providers.telegram import fetch_telegram_updates, map_telegram_update
    from modules.ingestion.schemas import classify_telegram_probe

    a, source = run.attempt, run.attempt.source
    lease = await _lease(run)
    async with run.factory() as session:
        try:
            await provisioning.lock_connector(
                session, source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=a.scope,
                multi_workspace_enabled=a.multi, expected_access_fence=a.access_fence)
            snapshot = await connectors.get_native_credential_snapshot(
                session, source.id, source_generation=lease.source_generation,
                connector_revision=lease.connector_revision, scope=a.scope, multi_workspace_enabled=a.multi)
            if snapshot is None or snapshot.state != "ready" or not snapshot.encrypted_token or not snapshot.verified_bot_id:
                raise CredentialMissing
            token = decrypt_native_token(run.settings.connector_credential_encryption_key.get_secret_value(), snapshot)
        finally:
            await session.rollback()
        cursor = await ingestion.read_telegram_collection_state(
            session, lease, scope=a.scope, multi_workspace_enabled=a.multi)
    allowed = frozenset(cast("list[str]", source.configuration["telegram_chat_ids"]))

    async def before_send() -> None:
        await run.gate()
        await _credential_fence(run, telegram=snapshot)

    offset: int | None = None
    total_bytes = total_updates = 0
    for page_number in range(5):
        remaining = 25 * 1024 * 1024 - total_bytes
        page = await fetch_telegram_updates(
            token, offset=offset, remaining_bytes=min(10 * 1024 * 1024, remaining), before_request=before_send)
        total_bytes += page.transport_bytes
        total_updates += len(page.deliveries)
        if page.transport_bytes > remaining or total_updates > 500:
            raise ValueError("telegram_trigger_limit_exceeded")
        classification = classify_telegram_probe(
            cursor, page.deliveries, received_at=page.collected_at, verified_bot_id=snapshot.verified_bot_id)
        if classification.conflict_code is not None:
            raise ValueError("telegram_stream_conflict")
        replay = set(classification.replay_update_ids)
        if page.deliveries and len(replay) == len(page.deliveries):
            if page_number == 4 or cursor is None or cursor.last_update_id >= 2**63 - 1:
                await _credential_fence(run, telegram=snapshot)
                await _accept_proof(
                    run, lease, records=(), coverage="pending_updates_only", cursor_after=lease.cursor_before,
                    collected_at=page.collected_at, native_operation_id=snapshot.operation_id)
                return
            async with run.factory() as session:
                await ingestion.release_connector_collection(
                    session, lease, error_code=None, scope=a.scope, multi_workspace_enabled=a.multi)
            lease = await _lease(run)
            if lease.configuration_revision != a.access_fence.configuration_revision:
                raise CollectionFenceLost("access_changed", "revision_changed")
            offset = cursor.last_update_id + 1
            continue
        proofs = {p.update_id: p for p in classification.delivery_proofs}
        records = [
            record for d in page.deliveries if d.update_id not in replay
            if (record := map_telegram_update(
                d.update, allowed_chat_ids=allowed, proof=proofs[d.update_id], collected_at=page.collected_at)) is not None
        ]
        await _credential_fence(run, telegram=snapshot)
        await _accept_proof(
            run, lease, records=records, coverage="pending_updates_only",
            cursor_after=classification.cursor_after.model_dump_json() if classification.cursor_after is not None else lease.cursor_before,
            collected_at=page.collected_at, telegram=classification.delivery_proofs, raw=page.deliveries,
            native_operation_id=snapshot.operation_id)
        return


ADAPTERS.update({"github": _run_github, "telegram": _run_telegram})


# ---------------------------------------------------------------- orchestration

def _classify(exc: BaseException) -> dict[str, Any]:
    """Map an adapter failure to ``settle_admission`` arguments (never response text)."""
    if isinstance(exc, rest.CollectionIncomplete):
        return {"outcome": "failed", "error_code": "collection_incomplete"}
    if isinstance(exc, rest.RestSchemaChanged):
        return {"outcome": "failed", "error_code": "schema_changed"}
    if isinstance(exc, CredentialMissing):
        return {"outcome": "failed", "error_code": "credential_missing"}
    if isinstance(exc, TermsIneligible):
        return {"outcome": "failed", "error_code": "terms_not_accepted"}
    if isinstance(exc, rest.UnsafeDestination):
        return {"outcome": "failed", "error_code": "destination_blocked"}
    if isinstance(exc, rest.ProviderHttpError):
        if exc.status_code in {401, 403}:
            return {"outcome": "failed", "error_code": "invalid_credential"}
        return {"outcome": "failed", "error_code": "provider_unavailable" if exc.retryable else "provider_rejected",
                "retryable": exc.retryable}
    if isinstance(exc, (TimeoutError, httpx.HTTPError, OSError)):
        return {"outcome": "failed", "error_code": "provider_timeout", "retryable": True}
    if isinstance(exc, HTTPException):
        return {"outcome": "failed", "error_code": "ingestion_conflict", "retryable": exc.status_code == 409}
    if isinstance(exc, ValueError):
        return {"outcome": "failed", "error_code": "provider_response_invalid"}
    return {"outcome": "failed", "error_code": "executor_error", "retryable": True}


async def _release_lease(run: Run, error_code: str) -> None:
    """Best-effort release of an unaccepted native source lease; it expires on its own if this fails."""
    lease = run.extras.get("lease")
    if lease is None or run.extras.get("accepted"):
        return
    a = run.attempt
    with suppress(BaseException):
        async with asyncio.timeout(3), run.factory() as session:
            await ingestion.release_connector_collection(
                session, lease, error_code=error_code, scope=a.scope, multi_workspace_enabled=a.multi)


def _adapter_for(source: ConnectorSource) -> Adapter | None:
    provider = source.provider
    if provider is None:
        return {"api": _run_rest, "rss": _run_rss, "web": _run_web, "mcp": _run_mcp}.get(source.type)
    if provider in ADAPTERS:
        return ADAPTERS[provider]
    return _run_native if provider in SIMPLE_NATIVE else None


def supports_native(source: ConnectorSource) -> bool:
    """True when the shared executor can collect this source; activation refuses everything else."""
    if source.type == "web" and source.provider is None and (source.configuration or {}).get("js_render"):
        return False  # browser rendering stays on crawl.py
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
    spec = get_provider_spec(attempt.source.provider)
    state: dict[str, Any] = {"attempt": attempt, "factory": factory, "settings": settings}
    gate = SendGate(
        factory, attempt, settings=settings, deadline=time.monotonic() + rest.RUN_DEADLINE_SECONDS,
        quota_provider=spec.id if spec is not None else None,
        fingerprint=_alpha_fingerprint(state) if attempt.source.provider == "alpha_vantage" else None)
    run = Run(ctx=ctx, factory=factory, settings=settings, attempt=attempt, gate=gate)
    lost = asyncio.Event()
    main = asyncio.current_task()
    assert main is not None
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
        settle = {"outcome": "failed", "error_code": "provider_rate_limited", "retryable": True,
                  "provider_deadline": exc.next_eligible_at}
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: every failure must settle the request
        settle = _classify(exc)
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

