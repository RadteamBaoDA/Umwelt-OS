"""Native-provider dispatch and handlers.

Private part of :mod:`modules.connectors.collection` (a pure move; that module re-exports every name).
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from modules.connectors import public as connectors
from modules.connectors._collection_base import (
    HEADER_AUTH_PROVIDERS,
    PAYLOAD_RATE_LIMIT_BACKOFF,
    CollectionFenceLost,
    CredentialMissing,
    Run,
    _accept_native,
    _lease,
    _optional_rest_secret,
    _rest_secret,
    _Unmetered,
)
from modules.connectors._collection_oauth import (
    _accept_proof,
    _credential_fence,
    _google_access_token,
    _reddit_access_token,
)
from modules.connectors.providers import rest


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
