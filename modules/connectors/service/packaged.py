"""Packaged-workflow (n8n) wake requests and callback URL helpers.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope

if TYPE_CHECKING:
    from core.config import Settings
from modules.connectors.service.access import (
    _connector_access,
    _read_scoped_source,
)
from modules.connectors.service.config import (
    require_collection_fence,
)
from modules.connectors.service.github_hints import (
    PackagedCollectionWakeResult,
)
from modules.connectors.service.schemas import (
    CollectionFence,
)


def n8n_callback_base() -> str:
    """Return the fixed base URL packaged n8n workflows call back to (read-only, for Settings display)."""
    from modules.connectors.n8n import CALLBACK_BASE

    return CALLBACK_BASE


async def _post_wake(client: Any, service_url: str, paths: tuple[str, ...], body: dict[str, object], token: str) -> Any:
    """POST the wake body to the first webhook path n8n has registered and return that response.

    Workflows built before the Umwelt-OS rename still listen on the legacy path. n8n answers 404 for
    an unregistered path, so only a 404 moves on to the next path; any other status (including the
    409/425/429 deferrals) is returned as-is. The last response is returned when every path is 404.
    """
    response = None
    for webhook_path in paths:
        response = await client.post(
            f"{service_url.rstrip('/')}/webhook/{webhook_path}",
            json=body,
            headers={"X-BBD-Webhook-Token": token},
        )
        if response.status_code != 404:
            break
    return response


def _deferred_wake_result(response: Any, workspace_id: UUID, source_id: UUID) -> PackagedCollectionWakeResult:
    """Build a ``deferred`` wake result, honoring a decimal ``Retry-After`` capped at one hour."""
    retry_at = None
    retry_after = response.headers.get("retry-after")
    if retry_after and retry_after.isdecimal():
        retry_at = datetime.now(UTC) + timedelta(seconds=min(int(retry_after), 3600))
    return PackagedCollectionWakeResult(
        workspace_id=workspace_id, source_id=source_id, outcome="deferred", next_eligible_at=retry_at,
    )


def _acknowledged_wake_result(response: Any, workspace_id: UUID, source_id: UUID) -> PackagedCollectionWakeResult:
    """Build an ``acknowledged`` wake result from the workflow's optional JSON metadata.

    Non-JSON bodies, non-object bodies and malformed IDs are tolerated (fields stay ``None``);
    ``status`` defaults to ``queued`` and ``status``/``receipt_id`` are truncated to 32/128 chars.
    """
    metadata: dict[str, object] = {}
    try:
        body = response.json()
        if isinstance(body, dict):
            metadata = body
    except ValueError:
        pass
    try:
        run_id = UUID(str(metadata["run_id"])) if metadata.get("run_id") else None
        batch_id = UUID(str(metadata["batch_id"])) if metadata.get("batch_id") else None
    except (TypeError, ValueError):
        run_id = batch_id = None
    status = metadata.get("status")
    receipt_id = metadata.get("receipt_id")
    return PackagedCollectionWakeResult(
        workspace_id=workspace_id, source_id=source_id,
        outcome="acknowledged", run_id=run_id, batch_id=batch_id,
        status=status[:32] if isinstance(status, str) else "queued",
        receipt_id=receipt_id[:128] if isinstance(receipt_id, str) else None,
    )


async def _wake_still_current(
    session: AsyncSession, source_id: UUID, source: Any, locked: Any, access_fence: AccessFence,
    *, source_generation: int, connector_revision: int, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Re-lock after the webhook send and report whether authority still matches the pre-send view.

    The Source fence, Source snapshot, collection fence and module enablement must be unchanged.
    The session is always rolled back before returning (or raising) so no lock outlives the check.
    """
    from modules.settings.public import module_is_enabled
    from modules.sources import public as sources

    try:
        current_source_fence = await sources.lock_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        current_source = await _read_scoped_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=locked,
        )
        current = current_source_fence == locked and current_source == source and await require_collection_fence(
            session, source, CollectionFence(source_generation=source_generation, connector_revision=connector_revision),
            lock=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        current = current and await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    finally:
        await session.rollback()
    return bool(current)


async def wake_packaged_collection(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    settings: "Settings",
    timeout_seconds: int = 15,
    scope: Scope, multi_workspace_enabled: bool,
) -> PackagedCollectionWakeResult:
    """Wake only an active, fully applied packaged n8n collection through its real webhook token.

    Returns ``unavailable`` when n8n is disabled by the owner kill switch or has no webhook token.

    Source and provisioning locks are released before network I/O. The wake carries only source
    fences; n8n continues through its actual provider-fetch route and collector credential. A
    timeout is ambiguous and must keep the durable hint pending for idempotent retry.
    """
    import httpx

    from modules.connectors.n8n import workflow_webhook_paths
    from modules.connectors.provisioning import activation_status
    from modules.sources import public as sources

    if not 1 <= timeout_seconds <= 75:
        raise ValueError("Packaged collection wake timeout is out of range")
    from modules.settings.public import module_is_enabled, resolve_n8n

    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    locked = await sources.lock_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    source = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled) if locked is not None else None
    provisioned = await activation_status(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled) if source is not None else None
    if (
        source is None or locked is None or locked.local_only or source.status != "active" or source.generation != source_generation
        or not await module_is_enabled(session, "connectors", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        or provisioned is None or provisioned.state != "active"
        or provisioned.applied_revision != connector_revision
        or provisioned.desired_revision != connector_revision
        or provisioned.source_generation != source_generation
        or not await require_collection_fence(
            session, source, CollectionFence(
                source_generation=source_generation, connector_revision=connector_revision,
            ), lock=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    ):
        await session.rollback()
        return PackagedCollectionWakeResult(workspace_id=scope.workspace_id, source_id=source_id, outcome="unavailable")
    source_type = source.type
    await session.rollback()
    n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    await session.rollback()
    token = n8n.webhook_token
    # Kill switch: a disabled n8n is never woken; the durable hint stays pending until re-enabled.
    if not token or not n8n.enabled:
        return PackagedCollectionWakeResult(workspace_id=scope.workspace_id, source_id=source_id, outcome="unavailable")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            wake_body = {
                "source_id": str(source_id), "source_generation": source_generation,
                "connector_revision": connector_revision,
            }
            response = await _post_wake(
                client, n8n.service_url, workflow_webhook_paths(source_id, source_type), wake_body, token,
            )
            if response.status_code in {409, 425, 429}:
                return _deferred_wake_result(response, scope.workspace_id, source_id)
            response.raise_for_status()
            current = await _wake_still_current(
                session, source_id, source, locked, access_fence, source_generation=source_generation,
                connector_revision=connector_revision, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if not current:
                return PackagedCollectionWakeResult(workspace_id=scope.workspace_id, source_id=source_id, outcome="unavailable")
            return _acknowledged_wake_result(response, scope.workspace_id, source_id)
    except httpx.TimeoutException:
        return PackagedCollectionWakeResult(workspace_id=scope.workspace_id, source_id=source_id, outcome="ambiguous")
    except httpx.HTTPError:
        return PackagedCollectionWakeResult(workspace_id=scope.workspace_id, source_id=source_id, outcome="unavailable")
