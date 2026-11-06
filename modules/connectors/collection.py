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
import time
from contextlib import suppress
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import error_status, is_http_error
from core.heavy_work import to_thread_joined  # noqa: F401
from modules.connectors import (  # noqa: F401  (patched via the facade by tests)
    provider_terms,
    provisioning,
    quota,
    scheduler,
)
from modules.connectors import public as connectors
from modules.connectors._collection_base import (
    _HTTP_ERRORS,
    ADAPTERS,
    DEDUP_ONLY_PROVIDERS,
    HEADER_AUTH_PROVIDERS,
    KEYED_PROVIDERS,
    PAYLOAD_RATE_LIMIT_BACKOFF,
    PURE_MAX_BYTES,
    PURE_PROVIDERS,
    REFUSED_HERE,
    RENEW_EVERY_SECONDS,
    REVISIONED_REST_PROVIDERS,
    SIMPLE_NATIVE,
    SOCIAL_WALK_PROVIDERS,
    Adapter,
    Attempt,
    CollectionFenceLost,
    CredentialMissing,
    Run,
    SendGate,
    Snapshot,
    TermsIneligible,
    Walk,
    _accept_generic,
    _accept_native,
    _accept_walk,
    _alpha_fingerprint,
    _conditional,
    _coverage,
    _generic_update,
    _get_body,
    _heartbeat,
    _lease,
    _load_attempt,
    _optional_rest_secret,
    _payload,
    _payload_async,
    _publish_realtime_snapshot,
    _record_rate_limit,
    _records,
    _require_body,
    _rest_secret,
    _resume_cursor,
    _run_stats,
    _select,
    _settle,
    _state,
    _Unmetered,
    _with_header,
    snapshot_matches,
)
from modules.connectors._collection_native import (
    _NATIVE_HANDLERS,
    NativeCall,
    NativeHandler,
    _local_service_failure,
    _native_bluesky,
    _native_feed,
    _native_github_releases,
    _native_google,
    _native_huggingface,
    _native_local_service,
    _native_mastodon_x,
    _native_reddit,
    _native_workspace_rest,
    _native_world_data,
    _run_native,
    register_native_provider,
)
from modules.connectors._collection_oauth import (
    _accept_proof,
    _credential_fence,
    _google_access_token,
    _reddit_access_token,
    _run_github,
    _run_telegram,
)
from modules.connectors._collection_providers import (
    _fetch_sections,
    _keyed_secret,
    _run_coingecko,
    _run_coinpaprika,
    _run_feed,
    _run_gdelt,
    _run_hn,
    _run_itviec,
    _run_keyed,
    _run_openalex,
    _run_pure,
    _run_sec_edgar,
    _run_vietnamworks,
    _run_vn_stock,
)
from modules.connectors._collection_web import (
    _NotModified,
    _PageText,
    _parse_page,
    _run_mcp,
    _run_rest,
    _run_rss,
    _run_web,
)
from modules.connectors.catalog import get_catalog_entry
from modules.connectors.collection_schemas import CollectionAdmissionRead
from modules.connectors.provider_specs import (
    get_provider_spec,
    get_quota_policies,
)
from modules.connectors.providers import rest
from modules.ingestion import public as ingestion
from modules.ingestion.public import CollectionState  # noqa: F401
from modules.sources import public as sources  # noqa: F401
from modules.sources.public import ConnectorSource

logger = logging.getLogger(__name__)

__all__ = [
    "ADAPTERS",
    "Adapter",
    "Attempt",
    "CollectionFenceLost",
    "CredentialMissing",
    "DEDUP_ONLY_PROVIDERS",
    "HEADER_AUTH_PROVIDERS",
    "KEYED_PROVIDERS",
    "NativeCall",
    "NativeHandler",
    "PAYLOAD_RATE_LIMIT_BACKOFF",
    "PURE_MAX_BYTES",
    "PURE_PROVIDERS",
    "REFUSED_HERE",
    "RENEW_EVERY_SECONDS",
    "REVISIONED_REST_PROVIDERS",
    "Run",
    "SIMPLE_NATIVE",
    "SOCIAL_WALK_PROVIDERS",
    "SendGate",
    "Snapshot",
    "TermsIneligible",
    "Walk",
    "_HTTP_ERRORS",
    "_NATIVE_HANDLERS",
    "_NotModified",
    "_PageText",
    "_Unmetered",
    "_accept_generic",
    "_accept_native",
    "_accept_proof",
    "_accept_walk",
    "_alpha_fingerprint",
    "_conditional",
    "_coverage",
    "_credential_fence",
    "_fetch_sections",
    "_generic_update",
    "_get_body",
    "_google_access_token",
    "_heartbeat",
    "_keyed_secret",
    "_lease",
    "_load_attempt",
    "_local_service_failure",
    "_native_bluesky",
    "_native_feed",
    "_native_github_releases",
    "_native_google",
    "_native_huggingface",
    "_native_local_service",
    "_native_mastodon_x",
    "_native_reddit",
    "_native_workspace_rest",
    "_native_world_data",
    "_optional_rest_secret",
    "_parse_page",
    "_payload",
    "_payload_async",
    "_publish_realtime_snapshot",
    "_record_rate_limit",
    "_records",
    "_reddit_access_token",
    "_require_body",
    "_rest_secret",
    "_resume_cursor",
    "_run_coingecko",
    "_run_coinpaprika",
    "_run_feed",
    "_run_gdelt",
    "_run_github",
    "_run_hn",
    "_run_itviec",
    "_run_keyed",
    "_run_mcp",
    "_run_native",
    "_run_openalex",
    "_run_pure",
    "_run_rest",
    "_run_rss",
    "_run_sec_edgar",
    "_run_stats",
    "_run_telegram",
    "_run_vietnamworks",
    "_run_vn_stock",
    "_run_web",
    "_select",
    "_settle",
    "_state",
    "_with_header",
    "logger",
    "register_native_provider",
    "snapshot_matches",
]


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
