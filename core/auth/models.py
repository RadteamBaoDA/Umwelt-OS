from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class Owner(Base):
    """Password account retaining legacy IDs; account 1 alone holds instance operator authority.

    The nullable default pointer supports atomic provisioning, not an incomplete active account.
    Its composite foreign key proves that the referenced workspace belongs to this account.
    Invitation possession never populates email verification provenance.
    """
    __tablename__ = "owner"
    __table_args__ = (
        CheckConstraint("id > 0", name="ck_owner_positive_id"),
        CheckConstraint("account_state IN ('active', 'disabled')", name="ck_owner_account_state"),
        CheckConstraint("email IS NULL OR (email = lower(btrim(email)) AND length(email) > 0)", name="ck_owner_normalized_email"),
        CheckConstraint(
            "(email_verified_at IS NULL AND email_verification_source IS NULL) OR "
            "(email IS NOT NULL AND email_verified_at IS NOT NULL AND email_verification_source IS NOT NULL "
            "AND email_verification_source = 'google_oidc')",
            name="ck_owner_email_verification",
        ),
        ForeignKeyConstraint(
            ["default_workspace_id", "id"], ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_owner_owned_default_workspace", use_alter=True, ondelete="RESTRICT",
        ),
        Index("uq_owner_email", "email", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    account_state: Mapped[str] = mapped_column(String(16), default="active", server_default="active", nullable=False)
    default_workspace_id: Mapped[UUID | None] = mapped_column()
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_verification_source: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuthSession(Base):
    """Persisted owner session using token and CSRF hashes, reauthentication time, and expiry."""
    __tablename__ = "auth_session"
    __table_args__ = (Index("ix_auth_session_expires_at", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), nullable=False
    )
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reauthenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GoogleIdentity(Base):
    """Unique Google issuer/subject linked to one account, never resolved by email alone.

    Linked email is provider evidence, not a source-data grant or automatic account mailbox
    verification. Auth lifecycle validates active account and recent password reauth for linking.
    """
    __tablename__ = "google_identity"
    __table_args__ = (
        Index("uq_google_identity_issuer_subject", "issuer", "subject", unique=True),
    )

    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True
    )
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
