from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from modules.connectors import provisioning
from modules.connectors.credentials import N8nCredentials
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)
from modules.connectors.n8n import N8nApi, workflow_matches
from modules.connectors.provisioning import (
    capture_connector_observation,
    commit_connector_observation,
)


async def expire_github_webhook_details(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 50,
) -> int:
    """Scrub expired payload-derived metadata while retaining replay digests and delivery IDs.

    Do not scrub a delivery with unfinished binding fanout. The indexed retention deadline and
    batch cap bound cleanup; digest_count and the unique delivery namespace remain unchanged.
    """
    from sqlalchemy import or_

    from modules.connectors.models import GithubWebhookDelivery, GithubWebhookOutbox

    async with factory() as session:
        rows = list((await session.scalars(
            select(GithubWebhookDelivery)
            .outerjoin(GithubWebhookOutbox, GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id)
            .where(
                GithubWebhookDelivery.detail_expires_at <= now,
                GithubWebhookDelivery.details_scrubbed_at.is_(None),
                or_(GithubWebhookOutbox.id.is_(None), GithubWebhookOutbox.state == "complete"),
            )
            .order_by(GithubWebhookDelivery.detail_expires_at, GithubWebhookDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=GithubWebhookDelivery)
        )).all())
        for delivery in rows:
            delivery.event = "expired"
            delivery.action = None
            delivery.app_id = None
            delivery.installation_id = None
            delivery.repository_id = None
            delivery.targets = []
            delivery.details_scrubbed_at = now
        if rows:
            await session.commit()
        else:
            await session.rollback()
        return len(rows)


# Replay window after detail scrubbing during which a digest still rejects a re-sent delivery ID.
# Past it, a replay is harmless: deliveries only enqueue refresh/reconcile hints that are
# revalidated against the GitHub API, and HMAC verification still blocks forgery.
DIGEST_RETENTION = timedelta(days=30)


async def reclaim_github_webhook_digests(
    factory: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 100,
) -> int:
    """Delete fully fanned-out, long-scrubbed digests and give their capacity back.

    Invariant: ``GithubWebhookCapacity.digest_count`` equals the number of delivery rows. The
    receiver mutates both under the capacity row lock, so this takes the same lock first and
    deletes and decrements in one transaction. Only deliveries whose details were scrubbed more
    than ``DIGEST_RETENTION`` ago and whose outbox is absent or complete are removed (outbox rows
    cascade). The batch cap bounds lock time; leftovers are reclaimed on the next worker pass.
    """
    from sqlalchemy import or_

    async with factory() as session:
        capacity = await session.scalar(
            select(GithubWebhookCapacity).where(GithubWebhookCapacity.id == 1).with_for_update()
        )
        if capacity is None:
            await session.rollback()
            return 0
        rows = list((await session.scalars(
            select(GithubWebhookDelivery)
            .outerjoin(GithubWebhookOutbox, GithubWebhookOutbox.delivery_id == GithubWebhookDelivery.id)
            .where(
                GithubWebhookDelivery.details_scrubbed_at.is_not(None),
                GithubWebhookDelivery.details_scrubbed_at <= now - DIGEST_RETENTION,
                or_(GithubWebhookOutbox.id.is_(None), GithubWebhookOutbox.state == "complete"),
            )
            .order_by(GithubWebhookDelivery.details_scrubbed_at, GithubWebhookDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=GithubWebhookDelivery)
        )).all())
        for delivery in rows:
            await session.delete(delivery)
        capacity.digest_count = max(0, capacity.digest_count - len(rows))
        await session.commit()
        return len(rows)


# The coordinator is keyed by the single local owner (owner.id is fixed to 1 elsewhere).
_OWNER_ID = 1
_DELETED_SOURCE_REVOKE = "provider_revoke_pending_source_deleted"
_COORDINATOR_STALE_AFTER = timedelta(minutes=5)


async def revoke_deleted_source_github_grants(
    factory: async_sessionmaker[AsyncSession], settings: Settings, *, limit: int = 10,
) -> int:
    """Best-effort remote revoke for grants whose last GitHub source was archived or purged.

    GitHub's grant revoke is app/user-wide, so it is serialized with OAuth authorize, refresh and
    disconnect through the owner-wide ``GithubOAuthCoordinator``:
    phase 1 claims ``revoking`` (skipping this grant for the pass if the coordinator is busy),
    rechecks live peers and decrypts; phase 2 calls GitHub with no session or lock held; phase 3
    clears the ciphertext, records an opaque outcome code and releases the coordinator only if it
    still holds this operation. A crash after the claim leaves ``revoking`` with this module's
    error code; any later pass releases such a claim once it is older than five minutes, so the
    coordinator cannot stay wedged. The token is never logged and ciphertext is always cleared.
    """
    from modules.connectors.github import oauth
    from modules.connectors.models import GithubOAuthCoordinator, GithubOAuthGrant

    key = settings.connector_credential_encryption_key.get_secret_value()
    async with factory() as session:
        pending = list((await session.execute(
            select(GithubOAuthGrant.source_id).where(
                GithubOAuthGrant.error_code == _DELETED_SOURCE_REVOKE,
                GithubOAuthGrant.encrypted_tokens.is_not(None),
            ).order_by(GithubOAuthGrant.updated_at).limit(limit)
        )).scalars().all())
    handled = 0
    for source_id in pending:
        token: str | None = None
        claim: UUID | None = None
        outcome = "provider_revoke_failed_source_deleted"
        async with factory() as session:
            coordinator = await session.scalar(
                select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == _OWNER_ID).with_for_update()
            )
            if coordinator is None:
                coordinator = GithubOAuthCoordinator(owner_id=_OWNER_ID, state="idle")
                session.add(coordinator)
                await session.flush()
            if coordinator.state != "idle":
                if (
                    coordinator.state == "revoking" and coordinator.error_code == _DELETED_SOURCE_REVOKE
                    and coordinator.updated_at <= datetime.now(UTC) - _COORDINATOR_STALE_AFTER
                ):
                    # Our own claim from a crashed pass: release it, retry on the next pass.
                    coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None
                    await session.commit()
                else:
                    await session.rollback()
                continue
            grant = await session.get(GithubOAuthGrant, source_id)
            if grant is None or grant.encrypted_tokens is None:
                await session.rollback()
                continue
            if await provisioning.github_grant_has_active_peer(session, grant):
                outcome = "provider_revoke_skipped_source_deleted"
            else:
                try:
                    opened = oauth._open_token_cipher(
                        key, grant.encrypted_tokens, grant.source_id, grant.operation_id,
                        grant.source_generation, grant.configuration_revision,
                    )
                    candidate = opened.get("access_token")
                    token = candidate if isinstance(candidate, str) else None
                except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                    token = None
                if token is not None:
                    claim = uuid4()
                    coordinator.state, coordinator.operation_id = "revoking", claim
                    coordinator.error_code = _DELETED_SOURCE_REVOKE
            await session.commit()
        if token is not None:
            try:
                await oauth.revoke_github_grant(settings, token)
                outcome = "provider_revoked_source_deleted"
            except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                outcome = "provider_revoke_failed_source_deleted"
        async with factory() as session:
            # Lock order matches the routes: grant first, coordinator last.
            await provisioning.finish_deleted_source_grant_revoke(session, source_id, outcome)
            if claim is not None:
                coordinator = await session.scalar(
                    select(GithubOAuthCoordinator).where(GithubOAuthCoordinator.owner_id == _OWNER_ID).with_for_update()
                )
                if coordinator is not None and coordinator.operation_id == claim:
                    coordinator.state, coordinator.operation_id, coordinator.error_code = "idle", None, None
            await session.commit()
        handled += 1
    return handled


async def _delete_credential(
    session: AsyncSession,
    client: N8nCredentials,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
) -> bool:
    """Claim and delete a prepared credential operation, preserving unknown outcomes for reconciliation."""
    claimed = await provisioning.claim_credential_operation(
        session, source_id, slot, operation_id
    )
    if claimed is None:
        return False
    target = claimed.get("target_id")
    if not isinstance(target, str):
        before = await capture_connector_observation(session, source_id)
        changed = await provisioning.fail_credential_operation(
            session, source_id, slot, operation_id, "credential_delete_target_missing", unknown=False
        )
        if changed:
            await commit_connector_observation(session, before, operation_id=operation_id)
        else:
            await session.rollback()
        return False
    try:
        await client.delete(target)
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        rejected = (
            isinstance(exc, httpx.HTTPStatusError)
            and 400 <= exc.response.status_code < 500
            and exc.response.status_code != 408
        )
        before = await capture_connector_observation(session, source_id)
        changed = await provisioning.fail_credential_operation(
            session,
            source_id,
            slot,
            operation_id,
            "credential_delete_rejected" if rejected else "credential_delete_outcome_unknown",
            unknown=not rejected,
        )
        if changed:
            await commit_connector_observation(session, before, operation_id=operation_id)
        else:
            await session.rollback()
        return False
    before = await capture_connector_observation(session, source_id)
    result = await provisioning.acknowledge_credential_delete(
        session, source_id, slot, operation_id, target
    )
    if result:
        await commit_connector_observation(session, before, operation_id=operation_id)
    else:
        await session.rollback()
    return result


async def _resume_activation(
    session: AsyncSession,
    source_id: UUID,
    api: N8nApi,
    credentials: N8nCredentials,
    settings: Settings,
) -> bool:
    """Resume a persisted activation using the configured credential encryption key."""
    from modules.connectors.activation import drive_activation

    return await drive_activation(
        session,
        source_id,
        api,
        credentials,
        settings.connector_credential_encryption_key.get_secret_value(),
    )

async def reconcile_connectors(ctx: dict[str, object]) -> int:
    """Progress connector provisioning and one bounded GitHub hint dispatch pass."""
    settings = cast(Settings, ctx["settings"])
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    from modules.settings.public import read_module_availability

    async with factory() as availability_session:
        lifecycle = await read_module_availability(availability_session)
    enabled = next((item.enabled for item in lifecycle.modules if item.id == "connectors"), False)
    completed = await dispatch_github_webhooks(ctx) if enabled else 0
    api_key = settings.n8n_api_key.get_secret_value()
    if not api_key:
        return completed
    credentials = N8nCredentials(str(settings.n8n_service_url), api_key)
    api = N8nApi(str(settings.n8n_service_url), api_key)
    async with factory() as session:
        credential_rows = list((await session.scalars(
            select(ConnectorManagedCredential)
            .where(
                ConnectorManagedCredential.operation_envelope["state"].astext == "prepared",
                ConnectorManagedCredential.operation_envelope["kind"].astext == "delete",
                ConnectorManagedCredential.operation_id.is_not(None),
            )
            .order_by(ConnectorManagedCredential.updated_at)
            .limit(40)
        )).all())
        workflow_rows = list((await session.scalars(
            select(ConnectorProvisioning)
            .where(
                ConnectorProvisioning.workflow_operation["step"]["state"].astext == "prepared"
            )
            .order_by(ConnectorProvisioning.updated_at)
            .limit(40)
        )).all())
        unknown_create_rows = list((await session.scalars(
            select(ConnectorProvisioning)
            .where(
                ConnectorProvisioning.workflow_operation["step"]["state"].astext == "unknown",
                ConnectorProvisioning.workflow_operation["step"]["kind"].astext == "create",
            )
            .order_by(ConnectorProvisioning.updated_at)
            .limit(40)
        )).all())
        activation_rows = list((await session.scalars(
            select(ConnectorProvisioning)
            .where(
                ConnectorProvisioning.desired_enabled.is_(True),
                ConnectorProvisioning.state == "provisioning",
                ConnectorProvisioning.workflow_operation.is_(None),
                ConnectorProvisioning.activation_intent.is_not(None),
            )
            .order_by(ConnectorProvisioning.updated_at)
            .limit(40)
        )).all())
        credentials_pending = [
            (row.source_id, row.slot, row.operation_id,
             row.operation_envelope.get("kind") if isinstance(row.operation_envelope, dict) else None)
            for row in credential_rows
        ]
        workflow_pending = [
            (
                row.source_id,
                _workflow_operation(row).get("id"),
                _workflow_operation(row).get("step", {}).get("id"),
                _workflow_operation(row).get("step", {}).get("kind"),
                _workflow_operation(row).get("workflow_name"),
                _workflow_operation(row).get("step", {}).get("request"),
            )
            for row in workflow_rows
            if enabled or _workflow_operation(row).get("step", {}).get("kind") == "delete"
        ]
        unknown_create_pending = [
            (
                row.source_id,
                _workflow_operation(row).get("id"),
                _workflow_operation(row).get("step", {}).get("id"),
                _workflow_operation(row).get("workflow_name"),
                _workflow_operation(row).get("step", {}).get("request"),
            )
            for row in unknown_create_rows
        ]
        activation_pending = [row.source_id for row in activation_rows] if enabled else []
        await session.rollback()

    for source_id, slot, operation_id, kind in credentials_pending:
        if operation_id is None or kind != "delete":
            continue
        async with factory() as session:
            completed += await _delete_credential(
                session, credentials, source_id, slot, operation_id
            )

    for source_id, _operation_value, _step_value, _kind, _name, _body in workflow_pending:
        async with factory() as session:
            completed += await provisioning.drive_workflow_operation(session, source_id, api)

    for source_id, operation_value, step_value, name, body in unknown_create_pending:
        created_operation_id: UUID | None = None
        workflow_id: str | None = None
        try:
            created_operation_id = UUID(str(operation_value))
            matches = await api.find_workflows(str(name))
            if (
                len(matches) == 1
                and isinstance(matches[0].get("id"), str)
                and isinstance(body, dict)
            ):
                candidate_id = str(matches[0]["id"])
                actual = await api.get_workflow(candidate_id)
                if workflow_matches(body, actual):
                    workflow_id = candidate_id
        except Exception:  # noqa: BLE001, S110  # best-effort cleanup/optional step; failure intentionally ignored
            pass
        if workflow_id is not None and created_operation_id is not None:
            async with factory() as session:
                before = await capture_connector_observation(session, source_id)
                resolved = await provisioning.resolve_unknown_workflow_create(
                    session, source_id, created_operation_id, str(step_value), workflow_id
                )
                if resolved:
                    await commit_connector_observation(session, before, operation_id=created_operation_id)
                    completed += 1
                    continue
                await session.rollback()
        async with factory() as session:
            deferred = await provisioning.defer_unknown_workflow_create(
                session, source_id, str(operation_value), str(step_value)
            )
            if deferred:
                await session.commit()
            else:
                await session.rollback()

    for source_id in activation_pending:
        async with factory() as session:
            completed += await _resume_activation(
                session, source_id, api, credentials, settings
            )
    return completed


def _workflow_operation(row: ConnectorProvisioning) -> dict[str, Any]:
    """Return the provisioning workflow JSON; every caller selects rows whose workflow step state is set."""
    operation = row.workflow_operation
    assert operation is not None
    return operation


def _webhook_retry_delay(attempt: int) -> int:
    """Bound durable webhook dispatch retry spacing between 30 seconds and one hour."""
    return int(min(30 * (2 ** max(0, attempt - 1)), 3600))


async def dispatch_github_webhooks(ctx: dict[str, object]) -> int:
    """Fan out bounded frozen binding pages with durable target admissions and fair hint recovery.

    Binding snapshots are fetched without Source/outbox locks, then frozen under an exact outbox
    cursor check. Each public binding DTO is explicitly dumped and validated into the stricter
    private persisted DTO, preserving its field, identity, and bounds checks. Per-target admission
    and hint/capacity effects commit together; page advancement clears that ledger only after the
    same cursor/page still owns the result. Deferred hints that cannot revalidate current authority
    get a durable retry deadline and never reserve or wake.
    """
    from datetime import UTC, datetime, timedelta

    from modules.connectors import public as connectors
    from modules.connectors.github.webhooks import GitHubTargetHint
    from modules.connectors.public import (
        _GitHubFanoutBinding,
        _GitHubFanoutPage,
        _validated_github_fanout_page,
    )
    from modules.sources import public as sources

    settings = cast(Settings, ctx["settings"])
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    now = datetime.now(UTC)
    progressed = await expire_github_webhook_details(factory, now=now)
    progressed += await reclaim_github_webhook_digests(factory, now=now)
    progressed += await revoke_deleted_source_github_grants(factory, settings)
    async with factory() as session:
        outbox_ids = list((await session.scalars(select(GithubWebhookOutbox.id).where(
            GithubWebhookOutbox.state.in_(("pending", "dispatched", "needs_attention")),
            GithubWebhookOutbox.next_attempt_at <= now,
        ).order_by(GithubWebhookOutbox.next_attempt_at, GithubWebhookOutbox.id).limit(50))).all())

    for outbox_id in outbox_ids:
        async with factory() as session:
            outbox = await session.get(GithubWebhookOutbox, outbox_id)
            delivery = await session.get(GithubWebhookDelivery, outbox.delivery_id) if outbox else None
            if outbox is None or delivery is None:
                await session.rollback()
                continue
            cursor = outbox.binding_cursor
            stored_page = outbox.fanout_page
            delivery_snapshot: dict[str, Any] = {
                "id": delivery.id, "app_id": delivery.app_id,
                "installation_id": delivery.installation_id,
                "repository_id": delivery.repository_id,
                "targets": delivery.targets,
            }
            await session.rollback()

        if not delivery_snapshot["installation_id"]:
            async with factory() as session:
                current = await session.scalar(select(GithubWebhookOutbox).where(
                    GithubWebhookOutbox.id == outbox_id,
                ).with_for_update())
                if (
                    current is None or current.state not in {"pending", "dispatched", "needs_attention"}
                    or current.binding_cursor != cursor or current.fanout_page != stored_page
                ):
                    await session.rollback()
                    continue
                capacity = await session.scalar(select(GithubWebhookCapacity).where(
                    GithubWebhookCapacity.id == 1,
                ).with_for_update())
                current.state = "complete"
                current.binding_cursor = None
                current.fanout_page = None
                if current.capacity_reserved and capacity is not None and capacity.pending_count > 0:
                    capacity.pending_count -= 1
                if current.capacity_reserved:
                    current.capacity_reserved = False
                await session.commit()
            progressed += 1
            continue

        fanout_page: _GitHubFanoutPage | None = None
        failed = False
        if stored_page is not None:
            try:
                candidate = _validated_github_fanout_page(stored_page)
                if candidate.cursor_before != cursor:
                    raise ValueError("github_fanout_page_cursor_stale")
                fanout_page = candidate
            except ValueError:
                failed = True
        else:
            detached_bindings = None
            try:
                async with factory() as session:
                    detached_bindings = await connectors.list_github_event_bindings(
                        session, app_id=str(delivery_snapshot["app_id"]),
                        installation_id=str(delivery_snapshot["installation_id"]),
                        repository_id=str(delivery_snapshot["repository_id"]) if delivery_snapshot["repository_id"] else None,
                        limit=50, cursor=cursor,
                    )
                    await session.rollback()
            except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                failed = True
            if detached_bindings is not None:
                try:
                    # Convert the public DTO to a mapping explicitly; strict private DTO validation
                    # does not coerce a distinct Pydantic model instance by attribute name.
                    frozen_bindings = tuple(
                        _GitHubFanoutBinding.model_validate(item.model_dump(mode="python"))
                        for item in detached_bindings.items
                    )
                    candidate = _GitHubFanoutPage(
                        cursor_before=cursor,
                        cursor_after=detached_bindings.next_cursor,
                        bindings=frozen_bindings,
                        admissions=(),
                    )
                except (TypeError, ValueError):
                    candidate = None
                    failed = True
                if candidate is None:
                    detached_bindings = None
            if detached_bindings is not None and candidate is not None:
                async with factory() as session:
                    current = await session.scalar(select(GithubWebhookOutbox).where(
                        GithubWebhookOutbox.id == outbox_id,
                    ).with_for_update())
                    if (
                        current is None or current.state not in {"pending", "dispatched", "needs_attention"}
                        or current.binding_cursor != cursor
                    ):
                        await session.rollback()
                        continue
                    if current.fanout_page is None:
                        current.fanout_page = candidate.model_dump(mode="json")
                        await session.commit()
                        fanout_page = candidate
                    else:
                        try:
                            persisted = _validated_github_fanout_page(current.fanout_page)
                            if persisted.cursor_before != cursor:
                                raise ValueError("github_fanout_page_cursor_stale")
                            fanout_page = persisted
                            await session.rollback()
                        except ValueError:
                            await session.rollback()
                            failed = True
        if fanout_page is not None and not failed:
            for binding in fanout_page.bindings:
                if binding.app_id != delivery_snapshot["app_id"] or binding.installation_id != delivery_snapshot["installation_id"]:
                    continue
                if delivery_snapshot["repository_id"] and binding.repository_id != delivery_snapshot["repository_id"]:
                    continue
                for raw_target in delivery_snapshot["targets"]:
                    try:
                        target = GitHubTargetHint.model_validate(raw_target)
                    except (TypeError, ValueError):
                        failed = True
                        break
                    if target.locator_kind == "installation" and target.locator != binding.installation_id:
                        continue
                    if target.locator_kind == "repository" and target.locator != binding.repository_id:
                        continue
                    if delivery_snapshot["repository_id"] and target.locator_kind not in {"installation"} and target.locator_kind != "repository" and binding.repository_id != delivery_snapshot["repository_id"]:
                        continue
                    try:
                        async with factory() as session:
                            result = await connectors.enqueue_github_hint(
                                session, binding=binding,
                                delivery_receipt_id=delivery_snapshot["id"],
                                target=target,
                                expected_binding_cursor=fanout_page.cursor_before,
                            )
                        if result.disposition in {"capacity_exhausted", "stale_progress"}:
                            failed = True
                            break
                    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                        failed = True
                        break
                if failed:
                    break

        async with factory() as session:
            current = await session.scalar(select(GithubWebhookOutbox).where(
                GithubWebhookOutbox.id == outbox_id,
            ).with_for_update())
            if current is None:
                await session.rollback()
                continue
            if fanout_page is not None:
                try:
                    current_page = _validated_github_fanout_page(current.fanout_page)
                except ValueError:
                    current_page = None
                if (
                    current.state not in {"pending", "dispatched", "needs_attention"}
                    or current.binding_cursor != fanout_page.cursor_before
                    or current_page is None
                    or current_page.cursor_before != fanout_page.cursor_before
                    or current_page.cursor_after != fanout_page.cursor_after
                    or current_page.bindings != fanout_page.bindings
                ):
                    await session.rollback()
                    continue
            elif (
                current.state not in {"pending", "dispatched", "needs_attention"}
                or current.binding_cursor != cursor or current.fanout_page != stored_page
            ):
                await session.rollback()
                continue
            capacity = await session.scalar(select(GithubWebhookCapacity).where(
                GithubWebhookCapacity.id == 1,
            ).with_for_update())
            if failed:
                current.attempts = min(5, current.attempts + 1)
                current.state = "needs_attention" if current.attempts >= 5 else "pending"
                current.next_attempt_at = now + timedelta(
                    seconds=3600 if current.state == "needs_attention" else _webhook_retry_delay(current.attempts)
                )
            elif fanout_page is not None and fanout_page.cursor_after is not None:
                current.binding_cursor = fanout_page.cursor_after
                current.fanout_page = None
                current.state = "pending"
                current.next_attempt_at = now
            else:
                current.state = "complete"
                current.binding_cursor = None
                current.fanout_page = None
                if current.capacity_reserved and capacity is not None and capacity.pending_count > 0:
                    capacity.pending_count -= 1
                if current.capacity_reserved:
                    current.capacity_reserved = False
            await session.commit()
            progressed += 1

    async with factory() as session:
        deferred_source_id = await session.scalar(select(GithubSourceHint.source_id).where(
            GithubSourceHint.state == "capacity_deferred",
            GithubSourceHint.capacity_reserved.is_(False),
            GithubSourceHint.next_attempt_at <= now,
        ).order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.updated_at).limit(1))
        if deferred_source_id is not None:
            locked = await sources.lock_source(session, deferred_source_id)
            source = await sources.get_connector_source(session, deferred_source_id) if locked is not None else None
            if source is not None and source.status == "active":
                await connectors.reconcile_github_source_hints_lifecycle(
                    session, source_id=deferred_source_id,
                    source_generation=source.generation, active=True,
                )
            retry_base = datetime.now(UTC)
            retry_at = retry_base + timedelta(seconds=60)
            remaining = list((await session.scalars(select(GithubSourceHint).where(
                GithubSourceHint.source_id == deferred_source_id,
                GithubSourceHint.state == "capacity_deferred",
                GithubSourceHint.capacity_reserved.is_(False),
                GithubSourceHint.next_attempt_at <= retry_base,
            ).order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.id).limit(100).with_for_update(skip_locked=True))).all())
            for deferred_hint in remaining:
                deferred_hint.next_attempt_at = max(deferred_hint.next_attempt_at, retry_at)
            await session.commit()

        hint = await session.scalar(select(GithubSourceHint).where(
            GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention")),
            GithubSourceHint.capacity_reserved.is_(True),
            GithubSourceHint.reconcile_page <= 100,
            GithubSourceHint.next_attempt_at <= now,
        ).order_by(GithubSourceHint.next_attempt_at, GithubSourceHint.updated_at).limit(1).with_for_update(skip_locked=True))
        if hint is None:
            await session.rollback()
            return progressed
        snapshot = (hint.id, hint.source_id, hint.source_generation, hint.connector_revision, hint.dirty_revision)
        needs_attention = hint.state == "needs_attention" or hint.attempts >= 4
        hint.attempts = min(5, hint.attempts + 1)
        attempt = hint.attempts
        needs_attention = needs_attention or attempt >= 5
        hint.state = "needs_attention" if needs_attention else "dispatched"
        hint.next_attempt_at = now + timedelta(
            seconds=3600 if needs_attention else _webhook_retry_delay(attempt)
        )
        await session.commit()
    async with factory() as session:
        wake = await connectors.wake_packaged_collection(
            session, source_id=snapshot[1], source_generation=snapshot[2],
            connector_revision=snapshot[3], settings=settings, timeout_seconds=15,
        )
    async with factory() as session:
        hint = await session.scalar(select(GithubSourceHint).where(
            GithubSourceHint.id == snapshot[0],
        ).with_for_update())
        if hint is not None and hint.dirty_revision == snapshot[4] and hint.state in {"dispatched", "needs_attention"}:
            if wake.outcome == "acknowledged":
                hint.state = "needs_attention" if hint.attempts >= 5 else "dispatched"
                hint.next_attempt_at = now + timedelta(
                    seconds=3600 if hint.state == "needs_attention" else _webhook_retry_delay(max(1, hint.attempts))
                )
            elif wake.outcome == "deferred" and wake.next_eligible_at is not None:
                hint.state = "needs_attention" if hint.attempts >= 5 else "pending"
                hint.next_attempt_at = max(
                    wake.next_eligible_at,
                    now + timedelta(seconds=3600 if hint.state == "needs_attention" else 0),
                )
            elif hint.attempts >= 5:
                hint.state = "needs_attention"
                hint.next_attempt_at = now + timedelta(seconds=3600)
            else:
                hint.state = "pending"
                hint.next_attempt_at = now + timedelta(seconds=_webhook_retry_delay(hint.attempts))
            await session.commit()
        else:
            await session.rollback()
    return progressed + 1
