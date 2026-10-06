"""Validated public DTOs for resumable first-run onboarding."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


OnboardingStep = Literal[
    "ai_privacy", "capability", "sources", "sample_or_import", "indexing", "complete",
]
OnboardingDataChoice = Literal["sample", "personal_import"]


class OnboardingStateRead(BaseModel):
    """Expose only revisioned step progress and explicit completion time."""

    configuration_revision: int = Field(ge=1)
    current_step: OnboardingStep
    data_choice: OnboardingDataChoice | None = None
    completed_at: datetime | None


class OnboardingStateUpdate(BaseModel):
    """Validate an optimistic step update; secrets and readiness claims are not accepted."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    current_step: OnboardingStep
    data_choice: OnboardingDataChoice | None = None
