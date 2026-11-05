"""URL validation, cursor overlap and managed collection request helpers.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from socket import getaddrinfo
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.async_runtime import run_dns_lookup
from core.net_policy import is_public_address
from core.workspaces.schemas import Scope

if TYPE_CHECKING:
    from modules.connectors.collection_schemas import (
        CollectionAdmissionRead,
        CollectionAdmissionRequest,
        CollectionRequestRead,
        CollectionRequestRef,
    )
from modules.connectors.service.access import (
    _connector_access,
    _read_scoped_source,
)
from modules.connectors.service.schemas import (
    DEFAULT_OVERLAP,
)


async def validate_public_url(value: str) -> str:
    """Require credential-free HTTP(S) URLs whose resolved addresses are all public."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only credential-free HTTP(S) URLs are allowed")
    try:
        # wait_for bounds request latency only: a timed-out lookup keeps its thread until the resolver returns,
        # so it runs on the dedicated DNS pool and cannot starve the shared default executor.
        addresses = await asyncio.wait_for(
            run_dns_lookup(
                getaddrinfo,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                0,
                0,
                0,
            ),
            5,
        )
    except (OSError, TimeoutError) as exc:
        raise ValueError("URL host could not be resolved") from exc
    if not addresses or any(not is_public_address(ip_address(item[4][0])) for item in addresses):
        raise ValueError("URL resolves to a non-public address")
    return value


def overlap_floor(cursor: str | None, overlap: timedelta = DEFAULT_OVERLAP) -> datetime | None:
    """Return the UTC overlap start (``cursor - overlap``, one day by default) for a valid aware cursor, else None."""
    if not cursor:
        return None
    try:
        value = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC) - overlap


async def map_github_version(
    session: AsyncSession, ready: object, *, scope: Scope, multi_workspace_enabled: bool
) -> bool:
    """Map a current ready GitHub document version into canonical knowledge (flush-only, idempotent).

    Public entry for the ready-version worker; non-github sources return False. ``ready`` is a
    ``ReadyVersionRef``, which carries no workspace, actor or membership revision, so identity is
    never compared against the DTO. The caller's admitted ``scope`` is authoritative: a fresh
    owner admission is checked, then the Source is read through that scope (a foreign or
    generation-mismatched Source reads as absent) before the mapper runs under the same scope.
    """
    from modules.connectors.github.mapping import map_github_version as _map

    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source_id = getattr(ready, "source_id", None)
    if not isinstance(source_id, UUID):
        return False
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        source is None
        or source.provider != "github"
        or source.status != "active"
        or source.generation != getattr(ready, "source_generation", None)
    ):
        return False
    return await _map(session, ready, scope=scope, multi_workspace_enabled=multi_workspace_enabled)  # type: ignore[arg-type]


async def request_collection(
    session: AsyncSession,
    scope: Scope,
    source_id: UUID,
    trigger: Literal["manual", "scheduled", "retry"],
    expected_revision: int,
    *,
    multi_workspace_enabled: bool,
) -> "CollectionRequestRead":
    """Persist or coalesce one durable collection request for the owner and commit it."""
    from modules.connectors import scheduler

    return await scheduler.request_collection(
        session,
        scope,
        source_id,
        trigger,
        expected_revision,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def get_collection_request(
    session: AsyncSession,
    scope: Scope,
    source_id: UUID,
    request_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> "CollectionRequestRead":
    """Read a collection request after owner source-scope admission."""
    from modules.connectors import scheduler

    return await scheduler.get_collection_request(
        session, scope, source_id, request_id, multi_workspace_enabled=multi_workspace_enabled
    )


async def admit_managed_collection(
    session: AsyncSession,
    scope: Scope,
    source_id: UUID,
    fence: "CollectionAdmissionRequest",
    *,
    trigger: Literal["manual", "scheduled"] = "scheduled",
    multi_workspace_enabled: bool,
) -> "CollectionAdmissionRead":
    """Admit a managed n8n run and return its fenced token, or raise 409 when busy."""
    from modules.connectors import scheduler

    return await scheduler.admit_managed_collection(
        session,
        scope,
        source_id,
        fence,
        trigger=trigger,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def lock_collection_request_in_uow(
    session: AsyncSession,
    ref: "CollectionRequestRef",
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    scope: Scope,
) -> bool:
    """Lock and prove one admitted collection attempt for the ingestion owner; flush-only, no commit."""
    from modules.connectors import scheduler

    return await scheduler.lock_running_request_in_uow(
        session,
        ref.request_id,
        ref.admission_token,
        source_id=source_id,
        source_generation=source_generation,
        connector_revision=connector_revision,
        workspace_id=scope.workspace_id,
    )


async def settle_collection_in_uow(
    session: AsyncSession,
    ref: "CollectionRequestRef",
    *,
    outcome: Literal["succeeded", "no_changes"],
    ingestion_run_id: UUID | None,
    accepted_receipt_id: UUID | None = None,
) -> bool:
    """Flush a request's accepted terminal state and free its slot inside the caller's transaction.

    The ingestion owner calls this immediately before its single commit, so the batch, cursor,
    outbox, request outcome and slot release become durable together or not at all.
    """
    from modules.connectors import scheduler

    return await scheduler.settle_admission_in_uow(
        session,
        ref.request_id,
        ref.admission_token,
        outcome=outcome,
        ingestion_run_id=ingestion_run_id,
        accepted_receipt_id=accepted_receipt_id,
    )


async def settle_collection_exit(
    session: AsyncSession,
    ref: "CollectionRequestRef",
    *,
    outcome: Literal["no_changes", "failed"],
    error_code: str | None = None,
    retryable: bool = False,
    provider_deadline: datetime | None = None,
) -> bool:
    """Settle a failed/retryable/replay-only exit and free its slot now, committing; False if the token is stale.

    Reuses the sweeper's settlement, so action-required codes gate the schedule and retryable
    failures requeue no earlier than ``provider_deadline`` instead of waiting for slot expiry.
    """
    from modules.connectors import scheduler

    return await scheduler.settle_admission(
        session,
        ref.request_id,
        ref.admission_token,
        outcome=outcome,
        error_code=error_code,
        retryable=retryable,
        provider_deadline=provider_deadline,
    )


async def recoverable_collection_request_ids(
    session: AsyncSession, request_ids: list[UUID]
) -> set[UUID]:
    """Return which of ``request_ids`` are still queued/running, i.e. whose receipt must be kept."""
    from modules.connectors import scheduler

    return await scheduler.recoverable_request_ids(session, request_ids)


async def purge_collection_history(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 1000
) -> int:
    """Delete a bounded slice of old terminal requests, provider sends and spent quota windows; caller commits."""
    from modules.connectors import scheduler

    return await scheduler.purge_collection_history(session, now=now, limit=limit)


async def collection_timing(
    session: AsyncSession,
    scope: Scope,
    source_ids: list[UUID],
) -> dict[UUID, tuple[datetime | None, datetime | None]]:
    """Return (next_due_at, retry_at) per source in the caller's workspace, for source read DTOs."""
    from modules.connectors import scheduler

    return await scheduler.collection_timing(session, scope.workspace_id, source_ids)


def default_retention_days(provider: str | None) -> int | None:
    """Return the provider's default history retention in days, or None when unbounded.

    Thin public wrapper over the private provider-spec table so other modules
    (Sources retention policy) never import ``provider_specs`` directly.
    """
    from modules.connectors.provider_specs import get_default_retention_days

    return get_default_retention_days(provider)
