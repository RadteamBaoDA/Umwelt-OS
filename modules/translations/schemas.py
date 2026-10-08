"""Frozen request/result envelopes for translation batches.

Requests carry only resource references; content is always loaded by the resource owner.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.settings.schemas import TranslationSettingsRead

ResourceType = Literal["news_story", "daily_brief"]
TranslationStatus = Literal["pending", "ready", "unchanged", "blocked", "failed"]


class TranslationItemRequest(BaseModel):
    """Only IDs/revisions are accepted; content is loaded by resource owners."""
    model_config = ConfigDict(extra="forbid")

    resource_type: ResourceType
    resource_id: UUID
    resource_revision: str = Field(min_length=1, max_length=128)


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


class TranslationItemStatusRead(BaseModel):
    """Per-item status without content."""
    resource_type: ResourceType
    resource_id: UUID
    status: TranslationStatus


class TranslationBatchAccepted(BaseModel):
    """202 body. Disabled workspaces get ``batch_id=None``, blocked items and no enqueue."""
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
    error_code: str | None = None


class TranslationBatchRead(BaseModel):
    """GET body, bound to the requesting actor and workspace."""
    batch_id: UUID
    target_language: Literal["vi", "en"]
    items: list[TranslationItemRead]
