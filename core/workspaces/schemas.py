"""Detached identity context; callers still require resource-specific authorization."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """Membership snapshot for one actor; revalidate revisions before later publication.

    This grants workspace identity only. In particular, member role is not a document,
    Brief, source, Chat or Memory read permission, and never confers instance authority.
    No bearer/session is carried: account and membership checks are not logout proof.
    """

    user_id: int
    workspace_id: UUID
    role: Literal["owner", "member"]
    membership_revision: int

    def __post_init__(self) -> None:
        """Reject malformed detached identities; construction itself grants no authority."""
        if type(self.user_id) is not int or self.user_id <= 0:
            raise ValueError("A positive actor ID is required")
        if not isinstance(self.workspace_id, UUID) or self.role not in {"owner", "member"}:
            raise ValueError("A valid workspace and membership role are required")
        if type(self.membership_revision) is not int or self.membership_revision <= 0:
            raise ValueError("A positive membership revision is required")


@dataclass(frozen=True, slots=True)
class InternalJobScope:
    """Durable worker subject snapshot, never authorization supplied by queued JSON alone.

    Owner modules compare the subject to their durable job/receipt before admission. Source
    identity/generation restricts this subject and must be checked by the Source owner, including
    exact retained tombstones for cleanup. Worker scopes do not impersonate browser sessions.
    """

    workspace_id: UUID
    actor_user_id: int
    membership_revision: int
    source_id: UUID | None = None
    source_generation: int | None = None

    def __post_init__(self) -> None:
        """Require positive actor/revision and paired valid source identity/generation."""
        if not isinstance(self.workspace_id, UUID):
            raise ValueError("A valid workspace ID is required")
        if type(self.actor_user_id) is not int or self.actor_user_id <= 0:
            raise ValueError("A positive actor ID is required")
        if type(self.membership_revision) is not int or self.membership_revision <= 0:
            raise ValueError("A positive membership revision is required")
        if (self.source_id is None) != (self.source_generation is None):
            raise ValueError("Source identity and generation must be supplied together")
        if self.source_id is not None and (
            not isinstance(self.source_id, UUID)
            or type(self.source_generation) is not int or self.source_generation <= 0
        ):
            raise ValueError("A valid source ID and positive generation are required")


Scope: TypeAlias = WorkspaceContext | InternalJobScope


@dataclass(frozen=True, slots=True)
class AccessFence:
    """Detached membership/configuration revisions for later locked authorization checks."""

    workspace_id: UUID
    user_id: int
    membership_revision: int
    configuration_revision: int

    def __post_init__(self) -> None:
        """Reject malformed revision snapshots; a valid snapshot is still not permission."""
        if not isinstance(self.workspace_id, UUID):
            raise ValueError("A valid workspace ID is required")
        if any(type(value) is not int or value <= 0 for value in (
            self.user_id, self.membership_revision, self.configuration_revision,
        )):
            raise ValueError("Positive actor and fence revisions are required")


class WorkspaceRead(BaseModel):
    """Membership-visible workspace metadata, without credentials or domain contents."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    name: str
    owner_user_id: int
    is_default: bool
    role: Literal["owner", "member"]
    configuration_revision: int


class WorkspaceList(BaseModel):
    """Only the authenticated actor's membership-visible metadata."""

    items: list[WorkspaceRead]


class WorkspaceEdit(BaseModel):
    """Owner rename guarded by the visible workspace configuration revision."""

    name: str = Field(min_length=1, max_length=160)
    expected_revision: int | None = Field(default=None, ge=1)


class InvitationCreate(BaseModel):
    """Normalized target email is bound by the owner service, with required CAS."""

    email: str = Field(min_length=3, max_length=320)
    expected_revision: int | None = Field(default=None, ge=1)


class InvitationCreated(BaseModel):
    """One-time secret URL; never used as a management-read projection."""

    invitation_id: UUID
    invitation_url: str
    expires_at: datetime


class InvitationRead(BaseModel):
    """Owner-only invitation state excluding token secrets and storage digests."""

    id: UUID
    email: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    accepted_by_user_id: int | None
    revoked_at: datetime | None


class InvitationList(BaseModel):
    """Bounded owner-only invitation page, without bearer credentials."""

    items: list[InvitationRead]
    next_cursor: UUID | None = None


class MemberRead(BaseModel):
    """Owner-only member identity and current authorization revision."""

    user_id: int
    email: str | None
    role: Literal["owner", "member"]
    membership_revision: int


class MemberList(BaseModel):
    """Bounded owner-only membership page."""

    items: list[MemberRead]
    next_cursor: int | None = None


class InvitationAccept(BaseModel):
    """Bearer proof and optional real password; secret repr prevents accidental logging."""

    token: SecretStr = Field(min_length=40, max_length=256)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)
    google_enrollment: bool = False


class InvitationAccepted(BaseModel):
    """Atomic membership result; acceptance intentionally issues no authentication session."""

    membership: MemberRead
    default_workspace: WorkspaceRead | None = None


@dataclass(frozen=True, slots=True)
class InvitationTarget:
    """Internal detached lookup for auth/OIDC preparation; contains no invitation secret."""

    invitation_id: UUID
    workspace_id: UUID
    owner_user_id: int
    email: str
