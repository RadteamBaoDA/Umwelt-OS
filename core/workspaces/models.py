"""Workspace identity persistence; lifecycle callers must use the public contract."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class Workspace(Base):
    """One private default workspace per account, with a CAS revision for administration."""

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_workspaces_owner_user_id"),
        UniqueConstraint("id", "owner_user_id", name="uq_workspaces_id_owner"),
        CheckConstraint("is_default", name="ck_workspaces_default_only"),
        CheckConstraint("configuration_revision > 0", name="ck_workspaces_positive_revision"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_workspaces_name"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="RESTRICT"), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkspaceMembership(Base):
    """Membership identity and revision; the owner marker must match the workspace's owner.

    Members need explicit resource shares before any domain read is authorized. The nullable
    owner marker permits members while enforcing owner identity through a composite FK.
    """

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'member')", name="ck_workspace_memberships_role"),
        CheckConstraint("revision > 0", name="ck_workspace_memberships_positive_revision"),
        CheckConstraint(
            "(role = 'owner' AND owner_user_id IS NOT NULL AND owner_user_id = user_id) OR "
            "(role = 'member' AND owner_user_id IS NULL)", name="ck_workspace_memberships_owner_marker",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_workspace_memberships_matching_owner", ondelete="CASCADE",
        ),
        Index("uq_workspace_memberships_owner", "workspace_id", unique=True, postgresql_where=text("role = 'owner'")),
        Index("ix_workspace_memberships_user_id", "user_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkspaceInvitation(Base):
    """Hashed single-use invitation state; bearer possession is not email verification.

    Lifecycle checks seven-day issuance, CAS, ordered locks and matching authenticated email
    before single-use consumption. Password invitation possession does not verify the mailbox.
    """

    __tablename__ = "workspace_invitations"
    __table_args__ = (
        CheckConstraint("email = lower(btrim(email)) AND length(email) > 0", name="ck_workspace_invitations_normalized_email"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_workspace_invitations_token_hash"),
        CheckConstraint("expires_at > created_at", name="ck_workspace_invitations_expiry"),
        CheckConstraint(
            "(accepted_at IS NULL AND accepted_by_user_id IS NULL) OR "
            "(accepted_at IS NOT NULL AND accepted_by_user_id IS NOT NULL AND revoked_at IS NULL)",
            name="ck_workspace_invitations_acceptance",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "invited_by_user_id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_workspace_invitations_matching_owner", ondelete="CASCADE",
        ),
        Index("uq_workspace_invitations_token_hash", "token_hash", unique=True),
        Index("ix_workspace_invitations_workspace_id", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    invited_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("owner.id", ondelete="RESTRICT"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceShare(Base):
    """Owner-granted read access of one member to one document or brief.

    Bound to the member's membership revision; member removal cascades the row away, so a
    rejoin never revives an old grant. Share changes never bump membership/config revisions.
    """

    __tablename__ = "workspace_shares"
    __table_args__ = (
        CheckConstraint("resource_type IN ('document', 'brief')", name="ck_workspace_shares_resource_type"),
        CheckConstraint("revision > 0", name="ck_workspace_shares_positive_revision"),
        CheckConstraint("resource_revision > 0", name="ck_workspace_shares_positive_resource_revision"),
        CheckConstraint("membership_revision > 0", name="ck_workspace_shares_positive_membership_revision"),
        CheckConstraint("member_user_id <> granted_by_user_id", name="ck_workspace_shares_not_self"),
        ForeignKeyConstraint(
            ["workspace_id", "member_user_id"], ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_workspace_shares_member", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "granted_by_user_id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_workspace_shares_matching_owner", ondelete="CASCADE",
        ),
        Index(
            "ix_workspace_shares_member_active", "workspace_id", "member_user_id", "resource_type", "resource_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_workspace_shares_resource", "workspace_id", "resource_type", "resource_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    resource_id: Mapped[UUID] = mapped_column(primary_key=True)
    member_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    granted_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    resource_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    membership_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
