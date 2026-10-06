"""Validated DTOs for notification emission and owner reads."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NotificationEmit(BaseModel):
    """Input other modules pass to ``public.emit``; ``dedupe_key`` makes emission idempotent."""

    model_config = ConfigDict(extra="forbid")

    dedupe_key: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=2000)
    params: dict[str, str | int] = Field(default_factory=dict, max_length=16)
    link: str | None = Field(default=None, max_length=300)

    @field_validator("link")
    @classmethod
    def link_is_in_app_path(cls, value: str | None) -> str | None:
        """Allow only relative in-app paths: must start with a single '/' (no '//', backslash or scheme)."""
        if value is not None and (not value.startswith("/") or value.startswith("//") or "\\" in value):
            raise ValueError("link must be a relative in-app path")
        return value


@dataclass(frozen=True)
class NotificationEvidence:
    """Private exact Document identity attached only to a copied notification title."""

    document_id: UUID
    document_version_id: UUID


class NotificationRead(BaseModel):
    """Public notification projection."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    title: str | None
    body: str | None
    params: dict[str, Any]
    link: str | None
    read_at: datetime | None
    created_at: datetime


class NotificationPage(BaseModel):
    """Newest-first notifications plus the owner's total unread count."""

    items: list[NotificationRead]
    unread_count: int


class NotificationPatch(BaseModel):
    """Mark one notification read (true) or unread (false)."""

    model_config = ConfigDict(extra="forbid")

    read: bool
