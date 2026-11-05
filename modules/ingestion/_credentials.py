"""Collector credential issuance, locking and revocation (internal; import through ``public``)."""

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from core.workspaces.schemas import AccessFence, Scope
from modules.ingestion._scope import (
    _admit_ingestion_scope,
    _source_in_scope,
)
from modules.ingestion.models import (
    CollectorCredential,
)
from modules.sources import public as sources
from modules.sources.schemas import SourceFence


async def create_collector_credential(
    session: AsyncSession, source_id: UUID, *,
    credential_scope: Literal["ingestion:write", "mcp:collect"] = "ingestion:write",
    scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Acquire real admission/Source then rotate one literal collector capability, flush only.

    Enter without domain locks. Missing/archived Sources retain LookupError compatibility;
    active/paused nonlocal issuance is eligible. Local-only/stale issuance fails409 and an
    invalid capability raises ValueError.
    Genuine locked access/Source fences are passed to the held writer, never fabricated.
    Return the bearer once; only its hash persists. Caller owns commit/rollback, no I/O.
    """
    if credential_scope not in ("ingestion:write", "mcp:collect"):
        raise ValueError("Unsupported collector credential capability")
    source = await sources.lock_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or source.status == "archived":
        raise LookupError("Source not found")
    access_fence = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return await create_collector_credential_in_uow(
        session, source_id, credential_scope=credential_scope, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source,
    )


async def create_collector_credential_in_uow(
    session: AsyncSession, source_id: UUID, *,
    credential_scope: Literal["ingestion:write", "mcp:collect"] = "ingestion:write",
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> str:
    """Rotate an exact active/paused nonlocal Source capability under held earlier parents.

    Caller retains original admission/Source and applicable provisioning/slots/native/world;
    enter before Tools/GitHub/state. Complete access/Source proof is freshly compared without
    parent locks. Missing Source fails404, stale/archived/local-only fails409, invalid literal
    raises ValueError. Lock only active exact-capability rows by token hash, revoke with one
    timestamp, insert one new hash/literal and return plaintext once after flush. Sibling
    capabilities survive. No readiness, activation, commit/replay or network authority is
    granted; failures after mutation require the caller to roll back the whole transaction.
    """
    if credential_scope not in ("ingestion:write", "mcp:collect"):
        raise ValueError("Unsupported collector credential capability")
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_source is None:
        raise NotFoundError(detail="Source not found")
    if (current_access != access_fence or current_source != source_fence
            or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or current_source.status not in {"active", "paused"} or current_source.local_only):
        raise ConflictError(detail="Collector credential issuance fence is stale")
    now = datetime.now(UTC)
    credentials = list(
        (
            await session.scalars(
                select(CollectorCredential)
                .where(
                    CollectorCredential.source_id == source_id,
                    CollectorCredential.scope == credential_scope,
                    CollectorCredential.revoked_at.is_(None),
                )
                .order_by(CollectorCredential.token_hash).with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    for credential in credentials:
        credential.revoked_at = now
    token = secrets.token_urlsafe(32)
    session.add(CollectorCredential(
        token_hash=hashlib.sha256(token.encode()).hexdigest(), source_id=source_id, scope=credential_scope,
    ))
    await session.flush()
    return token


async def revoke_collector_credential(session: AsyncSession, token: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Discover a token Source, acquire ordered admission/Source, then revoke its exact row.

    Entry holds no prior domain locks. A foreign Source fails scoped admission; no raw
    token persists and this helper leaves commit to its caller.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    source_id = await session.scalar(
        select(CollectorCredential.source_id).where(CollectorCredential.token_hash == token_hash)
    )
    if source_id is None:
        return
    await sources.lock_source_set(
        session, (source_id,), scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    row = await session.scalar(
        select(CollectorCredential)
        .where(CollectorCredential.token_hash == token_hash, CollectorCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is not None:
        row.revoked_at = datetime.now(UTC)


async def lock_source_credentials_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare exact Source token rows, sorted by hash, without revoking or committing.

    Source lifecycle preparation holds admission/Source and optional provisioning plus
    Connector cleanup rows before this call. Fresh nonlocking owner proof for active,
    paused or archived G must match both complete captured fences; stale/unavailable
    proof raises409. Archived eligibility is destructive cleanup only, not issuance or
    collection authority. Visibility active G->paused G+1 and identical paused G preparation
    remain supported without changing Source or tokens.
    Only credential rows are locked, before Tools and GitHub state/hints/capacity; no earlier
    parent lock or authority token is acquired.
    """
    current_access = await _admit_ingestion_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (current_access != access_fence or current_source is None
            or current_source != source_fence or current_source.status not in {"active", "paused", "archived"}):
        raise ConflictError(detail="Source credential preparation is stale")
    (await session.scalars(select(CollectorCredential).where(
        CollectorCredential.source_id == source_id,
    ).order_by(CollectorCredential.token_hash).with_for_update()
                          .execution_options(populate_existing=True))).all()


async def revoke_source_credentials(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Revoke tokens under caller-held admission/Source locks; no earlier lock acquisition.

    Source lifecycle callers own ordered locks and commit. Nonlocking owner DTO proof
    checks exact scope/generation; this helper never recursively locks Source or auth.
    """
    if await _source_in_scope(session, source_id, scope=scope,
                              multi_workspace_enabled=multi_workspace_enabled) is None:
        return
    await session.execute(
        update(CollectorCredential)
        .where(CollectorCredential.source_id == source_id, CollectorCredential.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def collector_can_ingest(
    session: AsyncSession, source_id: UUID, token: str, *, credential_scope: str = "ingestion:write", scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Check literal credential capability and active Source under actual scoped owner admission.

    This nonlocking read is not intake admission. Collection wrappers acquire ordered
    access/Source locks and recheck token revocation before effects.
    """
    await _admit_ingestion_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    credential_valid = bool(await session.scalar(
        select(CollectorCredential.token_hash).where(
            CollectorCredential.token_hash == token_hash,
            CollectorCredential.source_id == source_id,
            CollectorCredential.scope == credential_scope,
            CollectorCredential.revoked_at.is_(None),
        )
    ))
    source = await _source_in_scope(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled) if credential_valid else None
    return source is not None and source.status == "active"
