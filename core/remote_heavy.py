"""Durable block on new heavy work while remote execution may remain active."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.remote_heavy_models import RemoteHeavyGuard

_PROOF_ISSUER = object()


@dataclass(frozen=True)
class RemoteHeavyGuardRead:
    """Expose guard identity and state without exposing mutable persistence rows."""

    operation_id: UUID
    service_instance_id: str
    remote_job_id: UUID
    state: Literal["active", "uncertain", "cleared"]
    nonce_hash: str
    expires_at: datetime


@dataclass(frozen=True)
class RemoteCleanupProof:
    """Opaque server-issued proof created only after authenticated cleanup evidence."""

    operation_id: UUID
    service_instance_id: str
    remote_job_id: UUID
    _issuer: object


def cleanup_proof_after_ack(
    operation_id: UUID, service_instance_id: str, remote_job_id: UUID
) -> RemoteCleanupProof:
    """Issue the internal proof token after the control client validates exact cleanup identity."""
    return RemoteCleanupProof(operation_id, service_instance_id, remote_job_id, _PROOF_ISSUER)


def _read(row: RemoteHeavyGuard) -> RemoteHeavyGuardRead:
    """Convert a persistence row into its immutable public read shape."""
    return RemoteHeavyGuardRead(
        operation_id=row.operation_id,
        service_instance_id=row.service_instance_id,
        remote_job_id=row.remote_job_id,
        state=row.state,  # type: ignore[arg-type]
        nonce_hash=row.nonce_hash,
        expires_at=row.expires_at,
    )


async def register_remote_heavy_in_uow(
    session: AsyncSession,
    operation_id: UUID,
    service_instance_id: str,
    remote_job_id: UUID,
    nonce_hash: str,
    expires_at: datetime,
) -> RemoteHeavyGuardRead:
    """Flush an active remote-work guard; the caller must commit before dispatch.

    Identity and hashed nonce bind reconciliation to one operation. A repeated
    identical registration is idempotent; changed identity conflicts. Callers set
    expires_at just past the remote job's hard runtime bound; after it, the guard
    stops blocking admission so a dead service cannot wedge heavy work forever.
    """
    row = await session.get(RemoteHeavyGuard, operation_id, with_for_update=True)
    if row is None:
        row = RemoteHeavyGuard(
            operation_id=operation_id,
            service_instance_id=service_instance_id,
            remote_job_id=remote_job_id,
            nonce_hash=nonce_hash,
            expires_at=expires_at,
            state="active",
        )
        session.add(row)
    elif (
        row.service_instance_id != service_instance_id
        or row.remote_job_id != remote_job_id
        or row.nonce_hash != nonce_hash
    ):
        raise ValueError("Remote heavy operation identity conflict")
    await session.flush()
    return _read(row)


async def mark_remote_heavy_uncertain_in_uow(
    session: AsyncSession, operation_id: UUID
) -> RemoteHeavyGuardRead:
    """Retain the admission block after transport or ownership loss without cleanup proof."""
    row = await session.get(RemoteHeavyGuard, operation_id, with_for_update=True)
    if row is None:
        raise LookupError("Remote heavy guard does not exist")
    if row.state != "cleared":
        row.state = "uncertain"
    await session.flush()
    return _read(row)


async def get_remote_heavy_guard(
    session: AsyncSession, operation_id: UUID
) -> RemoteHeavyGuardRead | None:
    """Read one immutable guard identity for authoritative callback validation."""
    row = await session.get(RemoteHeavyGuard, operation_id)
    return _read(row) if row is not None else None


async def clear_remote_heavy_in_uow(
    session: AsyncSession, operation_id: UUID, proof: RemoteCleanupProof
) -> RemoteHeavyGuardRead:
    """Clear a guard only with a server-issued proof for its exact remote identity."""
    row = await session.get(RemoteHeavyGuard, operation_id, with_for_update=True)
    if row is None:
        raise LookupError("Remote heavy guard does not exist")
    if (
        proof._issuer is not _PROOF_ISSUER
        or proof.operation_id != row.operation_id
        or proof.service_instance_id != row.service_instance_id
        or proof.remote_job_id != row.remote_job_id
    ):
        raise PermissionError("Remote cleanup proof does not match the guarded operation")
    row.state = "cleared"
    await session.flush()
    return _read(row)


async def has_blocking_remote_heavy_work(session: AsyncSession) -> bool:
    """Return whether any unexpired active or uncertain operation still blocks heavy admission.

    The remote job has a hard runtime bound, so a guard past its expires_at belongs to a
    service that must have stopped or restarted; ignoring it is the recovery path.
    """
    return bool(await session.scalar(select(RemoteHeavyGuard.operation_id).where(
        RemoteHeavyGuard.state.in_(("active", "uncertain")),
        RemoteHeavyGuard.expires_at > datetime.now(UTC),
    ).limit(1)))
