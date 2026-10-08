"""Shared owner-fenced receipt and deterministic identity contract for explicit demo seeding."""

from datetime import datetime
from hashlib import blake2b
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base

P08_DEMO_NAMESPACE = "bbd-os.demo.phase-8"
P10_DEMO_NAMESPACE = "bbd-os.demo.phase-10"
P12_DEMO_NAMESPACE = "bbd-os.demo.phase-12"


class DemoSeedBusy(RuntimeError):
    """Signal that another transaction currently owns the same demo-seed lock."""


class DemoSeedReceipt(Base):
    """Record completed per-owner seed namespaces so deleted rows are not recreated later.

    Workspace identity is mandatory and survives nullable or detached canonical references.
    """

    __tablename__ = "demo_seed_receipts"
    __table_args__ = (
        CheckConstraint(
            "length(namespace) BETWEEN 1 AND 128",
            name="ck_demo_seed_receipts_namespace_length",
        ),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_w2_demo_seed_receipts_workspace", ondelete="RESTRICT"),
        ForeignKeyConstraint(["workspace_id", "owner_id"], ['workspaces.id', 'workspaces.owner_user_id'], name="fk_w2_demo_seed_receipts_principal", ondelete="RESTRICT"),
        Index("ix_w2_demo_seed_receipts_scope", 'workspace_id', 'owner_id', 'namespace'),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True,
    )
    namespace: Mapped[str] = mapped_column(String(128), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )



def demo_seed_id(kind: str, identity: str) -> UUID:
    """Return the stable UUID for one P08 fixture identity using its shared namespace path."""
    if kind not in {"goal", "milestone", "task", "topic", "automation"} or not 1 <= len(identity) <= 128:
        raise ValueError("Invalid P08 demo seed identity")
    return uuid5(NAMESPACE_URL, f"{P08_DEMO_NAMESPACE}/{kind}/{identity}")


def p12_demo_seed_id(kind: str, identity: str) -> UUID:
    """Return a stable Phase 12 fixture ID without changing any earlier seed identity."""
    if (
        kind not in {"entity", "relationship", "event", "article", "conversation"}
        or not 1 <= len(identity) <= 128
    ):
        raise ValueError("Invalid P12 demo seed identity")
    return uuid5(NAMESPACE_URL, f"{P12_DEMO_NAMESPACE}/{kind}/{identity}")


async def claim_demo_seed(session: AsyncSession, owner_id: int, namespace: str, *, workspace_id: UUID) -> bool:
    """Try-lock one workspace/owner/namespace and report whether it remains unseeded.

    The caller must hold the transaction through all P08 flushes, receipt insertion, and commit.
    Concurrent callers fail immediately with DemoSeedBusy; a completed receipt returns false so
    later calls cannot recreate hard-deleted goals or detached task/topic links. Workspace identity
    scopes both the PostgreSQL lock and receipt lookup, so one workspace's seed never suppresses another.
    """
    if not session.in_transaction():
        raise RuntimeError("Demo seed claiming requires the coordinator transaction")
    if type(owner_id) is not int or owner_id < 1 or not 1 <= len(namespace) <= 128:
        raise ValueError("Invalid owner or demo seed namespace")
    # PostgreSQL's single-key transaction lock uses a stable signed 64-bit digest of this scope.
    lock_key = int.from_bytes(
        blake2b(f"bbd-os.demo-seed:{workspace_id}:{owner_id}:{namespace}".encode(), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    locked = await session.scalar(select(func.pg_try_advisory_xact_lock(lock_key)))
    if not locked:
        raise DemoSeedBusy("A demo seed is already running for this owner and namespace")
    receipt = await session.scalar(select(DemoSeedReceipt.owner_id).where(
        DemoSeedReceipt.workspace_id == workspace_id,
        DemoSeedReceipt.owner_id == owner_id,
        DemoSeedReceipt.namespace == namespace,
    ))
    return receipt is None


async def record_demo_seed_receipt(
    session: AsyncSession, owner_id: int, namespace: str, *, workspace_id: UUID,
) -> None:
    """Flush a workspace-bound completion receipt into the caller transaction without committing."""
    if not session.in_transaction():
        raise RuntimeError("Demo seed receipt requires the coordinator transaction")
    session.add(DemoSeedReceipt(workspace_id=workspace_id, owner_id=owner_id, namespace=namespace))
    await session.flush()
