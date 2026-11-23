"""Frozen request/result envelopes for translation batches.

Requests carry only resource references; content is always loaded by the resource owner.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.settings.public import TranslationSettingsRead

# github_project is intentionally not translatable: it renders timeline events, not documents.
ResourceType = Literal["news_story", "daily_brief", "document", "live_event"]
TranslationStatus = Literal["pending", "ready", "unchanged", "blocked", "failed"]


class LiveEventSource(BaseModel):
    """Owner-supplied snapshot of a provider-published live-map event (ADR exception, <=30 days)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=1000)


class TranslationItemRequest(BaseModel):
    """IDs/revisions only, except ``live_event`` which carries its bounded inline ``source``."""

    model_config = ConfigDict(extra="forbid")

    resource_type: ResourceType
    resource_id: UUID
    resource_revision: str = Field(min_length=1, max_length=128)
    source: LiveEventSource | None = None

    @model_validator(mode="after")
    def _source_only_live_event(self) -> "TranslationItemRequest":
        """Reject inline ``source`` on every type but ``live_event`` (content is owner-loaded)."""
        if self.source is not None and self.resource_type != "live_event":
            raise ValueError("source is only accepted for live_event")
        return self


class TranslationBatchRequest(BaseModel):
    """Up to 25 distinct resource references; the target comes from current workspace settings."""

    model_config = ConfigDict(extra="forbid")

    items: list[TranslationItemRequest] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def _distinct(self) -> "TranslationBatchRequest":
        """Reject duplicate (type, id) references."""
        refs = [(item.resource_type, item.resource_id) for item in self.items]
        if len(set(refs)) != len(refs):
            raise ValueError("Duplicate item references")
        return self

    @model_validator(mode="after")
    def _live_event_has_source(self) -> "TranslationBatchRequest":
        """Every ``live_event`` item must carry its inline source (nothing server-side to look up)."""
        if any(i.resource_type == "live_event" and i.source is None for i in self.items):
            raise ValueError("live_event items require source")
        return self


class TranslationItemStatusRead(BaseModel):
    """Per-item status without content."""

    resource_type: ResourceType
    resource_id: UUID
    status: TranslationStatus
    error_code: str | None = None


class TranslationBatchAccepted(BaseModel):
    """202 body. Disabled workspaces get ``batch_id=None`` and blocked items; rejected items are
    ``failed`` with ``error_code`` ``resource_unavailable``/``stale_request`` (no batch if all are)."""

    batch_id: UUID | None
    items: list[TranslationItemStatusRead]
    settings: TranslationSettingsRead


class TranslationPayload(BaseModel):
    """Protected translated fields; absent fields are not translated."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=2000)
    excerpt: str | None = Field(default=None, max_length=20000)
    content: str | None = Field(default=None, max_length=200000)


class TranslationItemRead(TranslationItemStatusRead):
    """Result item; ``translation`` is present only for ``ready`` and still-authorized items."""

    translation: TranslationPayload | None = None
    target_language: Literal["vi", "en"]
    original_revision: str


class TranslationBatchRead(BaseModel):
    """GET body, bound to the requesting actor and workspace."""

    batch_id: UUID
    target_language: Literal["vi", "en"]
    items: list[TranslationItemRead]


@dataclass(frozen=True, slots=True)
class TranslationInput:
    """Immutable, owner-module-supplied snapshot of the exact text to translate plus the revision, visibility hash and source identities that bind it."""

    workspace_id: UUID
    actor_user_id: int
    resource_type: ResourceType
    resource_id: UUID
    resource_revision: str  # news: translation_revision hash; brief: str(DailyBrief.revision)
    fields: dict[str, str]  # news {"title","excerpt"}; brief {"content"}
    visibility_hash: str  # sha256 hex over sorted dependency tuples (R12)
    source_ids: tuple[UUID, ...]
    local_only: bool
