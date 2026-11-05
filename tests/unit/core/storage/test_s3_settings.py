"""``STORAGE_BACKEND=s3`` settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import Settings


def test_local_is_the_default_and_needs_no_s3_settings() -> None:
    settings = Settings()
    assert settings.storage_backend == "local" and settings.s3_bucket == ""
    assert (
        settings.storage_presigned_downloads is False
        and settings.storage_presigned_ttl_seconds == 60
    )


def test_s3_requires_a_bucket() -> None:
    with pytest.raises(ValidationError, match="S3_BUCKET"):
        Settings(storage_backend="s3")


def test_s3_credentials_must_be_set_together_and_stay_masked() -> None:
    with pytest.raises(ValidationError, match="set together"):
        Settings(storage_backend="s3", s3_bucket="b", s3_access_key_id="only-id")
    settings = Settings(
        storage_backend="s3", s3_bucket="b", s3_access_key_id="id", s3_secret_access_key="topsecret"
    )
    assert "topsecret" not in repr(settings) and "topsecret" not in str(
        settings.s3_secret_access_key
    )


def test_presigned_ttl_is_capped_at_sixty_seconds() -> None:
    with pytest.raises(ValidationError):
        Settings(storage_presigned_ttl_seconds=61)
