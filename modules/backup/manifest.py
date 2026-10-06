"""Versioned, credential-free manifest carried inside an encrypted backup archive."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BackupFile(BaseModel):
    """One archived regular file, addressed only by a safe archive-relative name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackupComponent(BaseModel):
    """Component version and archive members produced by one supported snapshot path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    status: Literal["complete", "not_configured"]
    directory: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    files: tuple[BackupFile, ...]


class ProtectedKeyReference(BaseModel):
    """Refer to a separately protected secret by purpose and digest, never plaintext."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_:-]*$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackupManifest(BaseModel):
    """Describe exact archived components and checksums without exposing host paths or keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    created_at: datetime
    consistency: Literal["quiesced"]
    consistency_method: str = Field(min_length=1, max_length=160)
    components: tuple[BackupComponent, ...]
    required_key_references: tuple[ProtectedKeyReference, ...]
