"""Workspace-bound live-map settings; provider payloads never enter persistence."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.database import Base
from core.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    UnavailableError,
    ValidationFailedError,
)
from core.workspaces import public as workspaces
from core.workspaces.schemas import AdminWorkspaceReadContext, WorkspaceContext
from modules.connectors.public import live_terms_catalog


class LiveMapCredential(Base):
    """One encrypted, revisioned map configuration bound to its workspace owner."""

    __tablename__ = "dashboard_live_credentials"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "owner_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            name="fk_live_map_credentials_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint("revision > 0", name="ck_live_map_credentials_revision"),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    encrypted_payload: Mapped[str | None] = mapped_column(Text, nullable=True)


class LiveMapTerms(Base):
    """Owner acknowledgement and separate operator decision, without provider credentials."""

    __tablename__ = "dashboard_live_terms"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "owner_id"],
            ["workspaces.id", "workspaces.owner_user_id"],
            ondelete="CASCADE",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    declared_use: Mapped[str] = mapped_column(String(32), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reviewer_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("owner.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_ref: Mapped[str | None] = mapped_column(String(512))


@dataclass(frozen=True, slots=True)
class LiveCredentials:
    """Detached read snapshot; secret fields never appear in repr or API projections."""

    revision: int
    openweather_key: str | None = field(default=None, repr=False)
    firms_key: str | None = field(default=None, repr=False)
    sec_contact_email: str | None = field(default=None, repr=False)
    terms: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)


class LiveCredentialUpdate(BaseModel):
    """CAS update; absent fields keep their old value while explicit null removes it."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    openweather_key: SecretStr | None = None
    firms_key: SecretStr | None = None
    sec_contact_email: (
        Annotated[str, Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")]
        | None
    ) = None
    terms_accept: list[str] | None = Field(default=None, max_length=12)
    terms_revoke: list[str] | None = Field(default=None, max_length=12)
    terms_use: Literal["personal", "noncommercial", "commercial"] | None = None
    terms_review_confirmed: bool = False
    terms_evidence_ref: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @field_validator("openweather_key", "firms_key")
    @classmethod
    def validate_secret(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject empty, overlong and whitespace-bearing credentials before encryption."""
        if value is not None:
            raw = value.get_secret_value()
            if not 1 <= len(raw.encode("utf-8")) <= 512 or any(
                c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw
            ):
                raise ValueError("Credential is invalid")
        return value


def _cipher(key: str) -> MultiFernet:
    """Create a rotation-capable connector-key cipher or reject missing deployment keys."""
    try:
        return MultiFernet([Fernet(part.strip().encode("ascii")) for part in key.split(",")])
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise UnavailableError(detail="Credential encryption unavailable") from exc


def _seal_secrets(key: str, workspace_id: UUID, revision: int, values: dict[str, str]) -> str:
    """Encrypt settings with workspace and revision binding to prevent ciphertext substitution."""
    envelope = {"workspace_id": str(workspace_id), "revision": revision, "values": values}
    return (
        _cipher(key)
        .encrypt(json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode())
        .decode("ascii")
    )


def _open_secrets(key: str, workspace_id: UUID, revision: int, encrypted: str) -> dict[str, str]:
    """Decrypt only the matching workspace and revision, failing closed on invalid data."""
    try:
        envelope = json.loads(_cipher(key).decrypt(encrypted.encode("ascii")))
        values = envelope["values"]
        if (
            envelope["workspace_id"] != str(workspace_id)
            or envelope["revision"] != revision
            or not isinstance(values, dict)
            or set(values) - {"openweather_key", "firms_key", "sec_contact_email", "terms_json"}
            or any(not isinstance(value, str) for value in values.values())
        ):
            raise ValueError("Invalid binding")
        return values
    except (
        InvalidToken,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        raise UnavailableError(detail="Stored map credentials unavailable") from exc


def _read_row(
    row: LiveMapCredential | None, settings: Settings, workspace_id: UUID
) -> LiveCredentials:
    """Detach a settings row and decrypt only its bound secret envelope."""
    if row is None:
        return LiveCredentials(revision=1)
    values = (
        _open_secrets(
            settings.connector_credential_encryption_key.get_secret_value(),
            workspace_id,
            row.revision,
            row.encrypted_payload,
        )
        if row.encrypted_payload
        else {}
    )
    try:
        terms = json.loads(values.pop("terms_json", "{}"))
        if not isinstance(terms, dict) or any(
            not isinstance(record, dict) for record in terms.values()
        ):
            raise ValueError("Invalid terms records")
    except (ValueError, TypeError) as exc:
        raise UnavailableError(detail="Stored map terms unavailable") from exc
    return LiveCredentials(revision=row.revision, terms=terms, **values)


def require_live_terms(credentials: LiveCredentials, provider: str) -> None:
    """Deny absent, stale, incompatible or unreviewed provider terms before cache or network access."""
    item = live_terms_catalog().get(provider)
    record = credentials.terms.get(provider)
    if not item or not isinstance(record, dict) or record.get("version") != item["version"]:
        raise ConflictError(detail="provider_terms_ineligible")
    use = record.get("declared_use")
    if use not in {"personal", "noncommercial", "commercial"}:
        raise ConflictError(detail="provider_terms_ineligible")
    eligibility = item["eligibility"]
    allowed = (
        eligibility == "open"
        or (eligibility == "personal" and use == "personal")
        or (eligibility == "noncommercial" and use in {"personal", "noncommercial"})
        or (
            eligibility == "review"
            and record.get("review_state") == "approved"
            and record.get("reviewer_user_id") == 1
            and isinstance(record.get("evidence_ref"), str)
            and bool(record["evidence_ref"].strip())
        )
    )
    if not allowed:
        raise ConflictError(detail="provider_terms_ineligible")


async def read_live_credentials(
    session: AsyncSession,
    *,
    scope: WorkspaceContext | AdminWorkspaceReadContext,
    settings: Settings,
    multi_workspace_enabled: bool,
) -> LiveCredentials:
    """Read a server-only owner snapshot for an admitted owner or read-only instance administrator."""
    if not isinstance(scope, AdminWorkspaceReadContext) and (
        not isinstance(scope, WorkspaceContext) or scope.role != "owner"
    ):
        raise ForbiddenError(detail="Workspace owner required")
    await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    row = await session.scalar(
        select(LiveMapCredential)
        .where(
            LiveMapCredential.workspace_id == scope.workspace_id,
            LiveMapCredential.owner_id == scope.user_id,
        )
        .execution_options(populate_existing=True)
    )
    value = _read_row(row, settings, scope.workspace_id)
    rows = await session.scalars(
        select(LiveMapTerms).where(LiveMapTerms.workspace_id == scope.workspace_id)
    )
    terms = {
        r.provider_id: {
            "version": r.version,
            "declared_use": r.declared_use,
            "review_state": r.review_state,
            "reviewer_user_id": r.reviewer_user_id,
            "evidence_ref": r.evidence_ref,
        }
        for r in rows
    }
    return LiveCredentials(
        revision=value.revision,
        openweather_key=value.openweather_key,
        firms_key=value.firms_key,
        sec_contact_email=value.sec_contact_email,
        terms=terms,
    )


def public_live_credentials(value: LiveCredentials, *, is_operator: bool) -> dict[str, Any]:
    """Expose revision, configured flags and SEC contact, never decrypted API keys."""
    catalog = []
    for provider, item in live_terms_catalog().items():
        try:
            require_live_terms(value, provider)
            accepted = True
        except (DomainError, HTTPException):
            accepted = False
        record = value.terms.get(provider, {})
        catalog.append(
            {
                "id": provider,
                **item,
                "accepted": accepted,
                "acknowledged": record.get("version") == item["version"],
                "review_state": record.get("review_state"),
            }
        )
    return {
        "revision": value.revision,
        "openweather_configured": value.openweather_key is not None,
        "firms_configured": value.firms_key is not None,
        "sec_contact_email": value.sec_contact_email,
        "is_operator": is_operator,
        "terms_catalog": catalog,
    }


async def save_live_credentials(
    session: AsyncSession,
    value: LiveCredentialUpdate,
    *,
    scope: WorkspaceContext,
    settings: Settings,
    multi_workspace_enabled: bool,
    auth_sessions: tuple[AccountSessionRef, ...],
    is_operator: bool,
) -> LiveCredentials:
    """CAS-update encrypted settings under an exclusive workspace fence; caller owns commit.

    Bumping the workspace revision atomically prevents an in-flight result from publishing
    after a concurrent credential or contact update. No provider request is made here.
    """
    changed_fields = value.model_fields_set & {
        "openweather_key",
        "firms_key",
        "sec_contact_email",
        "terms_accept",
        "terms_revoke",
    }
    if not changed_fields:
        raise ValidationFailedError(detail="At least one setting is required")
    accepted = value.terms_accept or []
    revoked = value.terms_revoke or []
    catalog = live_terms_catalog()
    if (
        len(accepted) != len(set(accepted))
        or len(revoked) != len(set(revoked))
        or set(accepted) & set(revoked)
        or (set(accepted) | set(revoked)) - set(catalog)
    ):
        raise ValidationFailedError(detail="Invalid provider terms selection")
    if accepted and value.terms_use is None:
        raise ValidationFailedError(detail="Declared use is required")
    for provider in accepted:
        eligibility = catalog[provider]["eligibility"]
        if (eligibility == "personal" and value.terms_use != "personal") or (
            eligibility == "noncommercial" and value.terms_use == "commercial"
        ):
            raise ConflictError(detail="provider_terms_ineligible")
    if value.terms_review_confirmed and not (
        is_operator and value.terms_evidence_ref and value.terms_evidence_ref.strip()
    ):
        raise ForbiddenError(detail="operator_review_required")
    await workspaces.bump_configuration_revision(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        auth_sessions=auth_sessions,
    )
    await session.execute(
        insert(LiveMapCredential)
        .values(
            workspace_id=scope.workspace_id,
            owner_id=scope.user_id,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id"])
    )
    row = await session.scalar(
        select(LiveMapCredential)
        .where(
            LiveMapCredential.workspace_id == scope.workspace_id,
            LiveMapCredential.owner_id == scope.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError(detail="Workspace not found")
    if row.revision != value.expected_revision:
        raise ConflictError(detail="Live map settings changed")
    previous = _read_row(row, settings, scope.workspace_id)
    data = {
        name: getattr(previous, name)
        for name in ("openweather_key", "firms_key", "sec_contact_email")
    }
    for name in value.model_fields_set & set(data):
        item = getattr(value, name)
        data[name] = item.get_secret_value() if isinstance(item, SecretStr) else item
    existing = await session.scalars(
        select(LiveMapTerms)
        .where(LiveMapTerms.workspace_id == scope.workspace_id)
        .with_for_update()
    )
    term_rows = {r.provider_id: r for r in existing}
    for provider in revoked:
        if provider in term_rows:
            await session.delete(term_rows.pop(provider))
    for provider in accepted:
        term = catalog[provider]
        record = term_rows.get(provider)
        if record is None:
            record = LiveMapTerms(
                workspace_id=scope.workspace_id, owner_id=scope.user_id, provider_id=provider
            )
            session.add(record)
            term_rows[provider] = record
        # ``terms_use`` is non-None here: an accepted provider without it was rejected above.
        record.version, record.declared_use, record.acknowledged_at = (
            term["version"],
            cast(str, value.terms_use),
            datetime.now(UTC),
        )
        record.review_state, record.reviewer_user_id, record.reviewed_at, record.evidence_ref = (
            "pending",
            None,
            None,
            None,
        )
        if term["eligibility"] == "review" and is_operator and value.terms_review_confirmed:
            record.review_state, record.reviewer_user_id, record.reviewed_at = (
                "approved",
                1,
                datetime.now(UTC),
            )
            record.evidence_ref = (value.terms_evidence_ref or "").strip()
    row.revision += 1
    retained = {name: item for name, item in data.items() if item is not None}
    row.encrypted_payload = (
        _seal_secrets(
            settings.connector_credential_encryption_key.get_secret_value(),
            scope.workspace_id,
            row.revision,
            retained,
        )
        if retained
        else None
    )
    await session.flush()
    terms = {
        p: {
            "version": r.version,
            "declared_use": r.declared_use,
            "review_state": r.review_state,
            "reviewer_user_id": r.reviewer_user_id,
            "evidence_ref": r.evidence_ref,
        }
        for p, r in term_rows.items()
    }
    return LiveCredentials(revision=row.revision, terms=terms, **data)
