"""Credential fences, OAuth token helpers and GitHub/Telegram adapters.

Private part of :mod:`modules.connectors.collection` (a pure move; that module re-exports every name).
"""

import secrets
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select

from modules.connectors import provisioning
from modules.connectors import public as connectors
from modules.connectors._collection_base import (
    ADAPTERS,
    CollectionFenceLost,
    CredentialMissing,
    Run,
    _lease,
)
from modules.ingestion import public as ingestion
from modules.ingestion.public import (
    IngestionRecord,
    NativeCollectionBatch,
)


async def _require_google_grant(
    session: Any, a: Any, lease: Any, binding: tuple[UUID, int]
) -> None:
    """Lock the source's Google OAuth grant and require it to match the captured binding.

    The grant must be ready, at the attempt's credential revision, the captured
    ``(operation_id, token_revision)`` and the lease's generation/revision.

    Raises:
        CollectionFenceLost: ``credential_changed`` on any mismatch.
    """
    from modules.connectors.models import ConnectorOAuthGrant

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
        or (grant.operation_id, grant.token_revision) != binding
        or grant.source_generation != lease.source_generation
        or grant.configuration_revision != lease.connector_revision
    ):
        raise CollectionFenceLost("credential_changed", "revision_changed")


async def _require_reddit_grant(
    session: Any, a: Any, lease: Any, binding: tuple[UUID, int]
) -> None:
    """Lock the source's Reddit OAuth grant and require it to match the captured binding.

    Same checks as Google plus provider, workspace and owner identity.

    Raises:
        CollectionFenceLost: ``credential_changed`` on any mismatch.
    """
    from modules.connectors.models import ConnectorOAuthGrant

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
        or (grant.operation_id, grant.token_revision) != binding
        or grant.source_generation != lease.source_generation
        or grant.configuration_revision != lease.connector_revision
    ):
        raise CollectionFenceLost("credential_changed", "revision_changed")


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
    from modules.connectors.models import GithubOAuthGrant

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
                await _require_google_grant(session, a, lease, google)
            if reddit is not None:
                await _require_reddit_grant(session, a, lease, reddit)
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


def _google_grant_usable(grant: Any, a: Any) -> bool:
    """Return whether a locked Google grant row is ready and bound to this attempt's fence.

    The caller has already rejected a missing row. Checks state, provider, workspace, owner, source generation, configuration and credential
    revision, a stored token cipher and the provider's read scope. Pure; no I/O.
    """
    from modules.connectors.oauth_routes import _PROVIDER_SCOPES

    return not (
        grant.state != "ready"
        or grant.provider != a.source.provider
        or grant.workspace_id != a.scope.workspace_id
        or grant.owner_id != a.scope.actor_user_id
        or grant.source_generation != a.source.generation
        or grant.configuration_revision != a.connector_revision
        or grant.credential_revision != a.credential_revision
        or not grant.encrypted_tokens
        or _PROVIDER_SCOPES.get(grant.provider) not in grant.granted_scopes
    )


async def _google_access_token(run: Run) -> tuple[str, tuple[UUID, int]]:
    """Load a source-bound read-only Google token and serialize refresh under its grant row lock.

    Refresh results are persisted only while the original source/configuration/credential fence
    remains current. Holding the grant lock across Google's token exchange prevents concurrent
    workers from overwriting a rotated refresh token; failures leave the prior grant unchanged.
    """
    from modules.connectors.google_oauth_tokens import refresh_google_token
    from modules.connectors.models import ConnectorOAuthGrant
    from modules.connectors.oauth_routes import _cipher, _open_cipher

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
        if grant is None or not _google_grant_usable(grant, a):
            await session.rollback()
            raise CredentialMissing
        try:
            key = run.settings.connector_credential_encryption_key.get_secret_value()
            tokens = _open_cipher(
                key,
                cast(str, grant.encrypted_tokens),  # non-empty: checked by _google_grant_usable
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


async def _open_github_grant(run: Run, lease: Any) -> tuple[Any, tuple[UUID, int], dict[str, Any]]:
    """Lock the connector and GitHub binding, validate the ready grant and open its token cipher.

    Lock order: connector (all credential slots), binding fence, then the grant row. The grant must
    be ready, unexpired and bound to the lease's generation and revision; the session is rolled back
    before returning so no lock is held across the provider call.

    Returns:
        ``(binding_fence, (operation_id, token_revision), token_pair)``.

    Raises:
        CredentialMissing: when the binding or a usable grant is absent.
    """
    from sqlalchemy import select

    from modules.connectors.github import oauth as github_oauth
    from modules.connectors.models import GithubOAuthGrant

    a = run.attempt
    source = a.source
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
    return fence, binding, pair


async def _run_github(run: Run) -> None:
    """GitHub: lease, claim a due webhook hint, open the OAuth grant, one fenced GET, validate, record."""

    from modules.connectors.github.adapter import collect_github_segment
    from modules.connectors.github.schemas import GitHubHintClaimProof, project_github_source_config
    from modules.connectors.github.sync import validate_github_segment

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
    fence, binding, pair = await _open_github_grant(run, lease)
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


async def _open_telegram_credential(run: Run, lease: Any) -> tuple[Any, str, Any]:
    """Read the ready Telegram bot credential under connector locks and load the stream cursor.

    Locks the connector (all credential slots), reads the native credential snapshot for the lease's
    exact generation/revision and decrypts the bot token; the session is rolled back before the
    cursor read so no lock outlives the credential read.

    Returns:
        ``(snapshot, bot_token, collection_cursor)``.

    Raises:
        CredentialMissing: when the credential is absent, not ready, unencrypted or unverified.
    """
    from modules.connectors.credentials import decrypt_native_token

    a, source = run.attempt, run.attempt.source
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
    return snapshot, token, cursor


async def _run_telegram(run: Run) -> None:
    """Telegram: bot token read under locks, getUpdates fenced per send; one accepted page per request.

    Pages that only replay already-ingested updates advance the offset (fresh lease each time, at most
    five pages); the first page with new work is accepted and settles the request.
    """
    from modules.connectors.providers.telegram import fetch_telegram_updates, map_telegram_update
    from modules.ingestion.public import classify_telegram_probe

    a, source = run.attempt, run.attempt.source
    lease = await _lease(run)
    snapshot, token, cursor = await _open_telegram_credential(run, lease)
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
