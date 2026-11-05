from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DomainEvent(BaseModel):
    """Base contract for immutable, versioned domain event payloads."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    type: str
    version: int
    occurred_at: datetime
    producer: str
    payload: dict[str, Any]
