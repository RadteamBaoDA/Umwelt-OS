"""Unit tests for settings module schemas, validation, default preferences, and endpoint policies.

Covers:
- Settings schemas: OwnerPreferencesRead and OwnerPreferencesUpdate.
- Validation rules: IANA timezone resolution, theme enum ('light', 'dark', 'system'), and locale enum ('en-us', 'vi-vi').
- Default owner preferences: uninitialized state returns system/en-us/Asia/Ho_Chi_Minh with revision 1 and persisted=False.
- Optimistic concurrency in save_owner_preferences: revision mismatch raises HTTP 409, while matching revision increments revision.
- Endpoint validation: policy checks against allowed hosts, port validation, credential rejection, and scheme normalization.
- Privacy mapping: destination-bound consent checks for remote reasoning, embeddings, and web search.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from core.config import Settings
from modules.settings.models import OwnerPreferencesRecord
from modules.settings.public import (
    _privacy,
    read_owner_preferences,
    save_owner_preferences,
    validate_endpoint,
)
from modules.settings.schemas import (
    OwnerPreferencesRead,
    OwnerPreferencesUpdate,
)


class TestOwnerPreferencesSchemas:
    """Tests for OwnerPreferences schema constraints and validation."""

    def test_valid_preferences_update(self) -> None:
        """OwnerPreferencesUpdate parses valid payload with valid IANA timezone."""
        update = OwnerPreferencesUpdate(
            expected_revision=1,
            theme="dark",
            locale="vi-vi",
            timezone="Asia/Ho_Chi_Minh",
        )
        assert update.expected_revision == 1
        assert update.theme == "dark"
        assert update.locale == "vi-vi"
        assert update.timezone == "Asia/Ho_Chi_Minh"

    def test_invalid_timezone_raises_validation_error(self) -> None:
        """OwnerPreferencesUpdate rejects timezones not recognized in IANA zone database."""
        with pytest.raises(ValidationError) as exc_info:
            OwnerPreferencesUpdate(
                expected_revision=1,
                theme="light",
                locale="en-us",
                timezone="Mars/Olympus_Mons",
            )
        assert "Timezone must be a valid IANA timezone" in str(exc_info.value)

    def test_invalid_theme_raises_validation_error(self) -> None:
        """OwnerPreferencesUpdate rejects unsupported theme strings."""
        with pytest.raises(ValidationError):
            OwnerPreferencesUpdate(
                expected_revision=1,
                theme="neon",  # Only light, dark, system allowed
                locale="en-us",
                timezone="UTC",
            )

    def test_invalid_locale_raises_validation_error(self) -> None:
        """OwnerPreferencesUpdate rejects unsupported locale strings."""
        with pytest.raises(ValidationError):
            OwnerPreferencesUpdate(
                expected_revision=1,
                theme="system",
                locale="fr-fr",  # Only en-us, vi-vi allowed
                timezone="UTC",
            )

    def test_expected_revision_ge_1(self) -> None:
        """OwnerPreferencesUpdate requires expected_revision >= 1."""
        with pytest.raises(ValidationError):
            OwnerPreferencesUpdate(
                expected_revision=0,
                theme="system",
                locale="en-us",
                timezone="UTC",
            )

    def test_extra_fields_forbidden(self) -> None:
        """OwnerPreferencesUpdate forbids unexpected extra fields."""
        with pytest.raises(ValidationError):
            OwnerPreferencesUpdate(
                expected_revision=1,
                theme="system",
                locale="en-us",
                timezone="UTC",
                custom_accent="blue",  # type: ignore[call-arg]
            )


class TestReadOwnerPreferences:
    """Tests for read_owner_preferences defaults and database projections."""

    @pytest.mark.asyncio
    async def test_read_owner_preferences_default_when_no_record(self) -> None:
        """read_owner_preferences returns documented defaults (system, en-us, Asia/Ho_Chi_Minh) when no row exists."""
        session = AsyncMock()
        session.scalar.return_value = None  # No database row exists

        prefs = await read_owner_preferences(session)
        assert isinstance(prefs, OwnerPreferencesRead)
        assert prefs.configuration_revision == 1
        assert prefs.persisted is False
        assert prefs.theme == "system"
        assert prefs.locale == "en-us"
        assert prefs.timezone == "Asia/Ho_Chi_Minh"

    @pytest.mark.asyncio
    async def test_read_owner_preferences_persisted_record(self) -> None:
        """read_owner_preferences returns persisted database record values with persisted=True."""
        session = AsyncMock()
        record = OwnerPreferencesRecord(
            owner_id=1,
            configuration_revision=4,
            theme="dark",
            locale="vi-vi",
            timezone="Asia/Tokyo",
        )
        session.scalar.return_value = record

        prefs = await read_owner_preferences(session)
        assert isinstance(prefs, OwnerPreferencesRead)
        assert prefs.configuration_revision == 4
        assert prefs.persisted is True
        assert prefs.theme == "dark"
        assert prefs.locale == "vi-vi"
        assert prefs.timezone == "Asia/Tokyo"


class TestSaveOwnerPreferences:
    """Tests for optimistic locking and saving preferences."""

    @pytest.mark.asyncio
    async def test_save_owner_preferences_conflict_raises_409(self) -> None:
        """save_owner_preferences raises 409 when row revision differs from expected_revision."""
        session = AsyncMock()
        session.execute.return_value = None

        record = OwnerPreferencesRecord(
            owner_id=1,
            configuration_revision=2,  # Current is 2
            theme="light",
            locale="en-us",
            timezone="UTC",
        )
        session.scalar.return_value = record

        update = OwnerPreferencesUpdate(
            expected_revision=1,  # Stale expected revision 1!
            theme="dark",
            locale="en-us",
            timezone="UTC",
        )

        with pytest.raises(HTTPException) as exc_info:
            await save_owner_preferences(session, update)
        assert exc_info.value.status_code == 409
        assert "Preferences changed; reload before saving" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_save_owner_preferences_success_increments_revision(self) -> None:
        """save_owner_preferences applies update and increments configuration_revision."""
        session = AsyncMock()
        session.execute.return_value = None

        record = OwnerPreferencesRecord(
            owner_id=1,
            configuration_revision=1,
            theme="light",
            locale="en-us",
            timezone="UTC",
        )
        session.scalar.return_value = record

        update = OwnerPreferencesUpdate(
            expected_revision=1,
            theme="dark",
            locale="vi-vi",
            timezone="Asia/Ho_Chi_Minh",
        )

        result = await save_owner_preferences(session, update)
        assert result.configuration_revision == 2
        assert result.persisted is True
        assert result.theme == "dark"
        assert result.locale == "vi-vi"
        assert result.timezone == "Asia/Ho_Chi_Minh"
        assert record.configuration_revision == 2
        session.flush.assert_called_once()


class TestEndpointAndPrivacyValidation:
    """Tests for validate_endpoint and _privacy destination binding."""

    def _sample_settings(self, allowed_hosts: tuple[str, ...] = ("api.openai.com", "gateway.local:8080")) -> Settings:
        """Create a mock settings instance with allowed hosts."""
        settings = MagicMock(spec=Settings)
        settings.ai_allowed_endpoint_hosts = set(allowed_hosts)
        return settings

    def test_validate_endpoint_valid(self) -> None:
        """validate_endpoint normalizes host and strips default port 443 for https."""
        settings = self._sample_settings(allowed_hosts=("api.openai.com",))
        url = "https://api.openai.com:443/v1/"
        result = validate_endpoint(url, settings)
        assert result == "https://api.openai.com/v1"

    def test_validate_endpoint_disallowed_host_raises_422(self) -> None:
        """validate_endpoint raises 422 if endpoint host is not allowed by deployment policy."""
        settings = self._sample_settings(allowed_hosts=("api.openai.com",))
        with pytest.raises(HTTPException) as exc_info:
            validate_endpoint("https://evil-site.com/v1", settings)
        assert exc_info.value.status_code == 422
        assert "Endpoint address is not allowed by deployment policy" in exc_info.value.detail

    def test_validate_endpoint_rejects_credentials(self) -> None:
        """validate_endpoint raises 422 if endpoint contains embedded credentials."""
        settings = self._sample_settings()
        with pytest.raises(HTTPException) as exc_info:
            validate_endpoint("https://user:pass@api.openai.com/v1", settings)
        assert exc_info.value.status_code == 422
        assert "Invalid gateway endpoint" in exc_info.value.detail

    def test_validate_endpoint_none_returns_none(self) -> None:
        """validate_endpoint returns None when input is None or empty."""
        settings = self._sample_settings()
        assert validate_endpoint(None, settings) is None
        assert validate_endpoint("", settings) is None

    def test_privacy_destinations_bound_to_current_destination(self) -> None:
        """_privacy scopes grants only when current destination matches granted destinations."""
        dest = "omniroute:dest1"
        raw_grants = {
            "allow_remote_reasoning": True,
            "reasoning_destinations": [dest],
            "allow_remote_embeddings": True,
            "embedding_destinations": ["other_dest"],  # Mismatch!
        }
        privacy = _privacy(raw_grants, destination=dest, web_destination=None)
        assert privacy.allow_remote_reasoning is True
        assert privacy.reasoning_destinations == [dest]
        assert privacy.allow_remote_embeddings is False
        assert privacy.embedding_destinations == []
        assert privacy.allow_remote_web_search is False

    def test_privacy_web_search_without_omniroute_destination_does_not_raise(self) -> None:
        """Web consent on with no OmniRoute destination must not build [None]."""
        raw = {"allow_remote_web_search": True, "web_search_destinations": ["web-search:x"]}
        privacy = _privacy(raw, None, "web-search:x")
        assert privacy.allow_remote_web_search is True
        assert privacy.web_search_destinations == ["web-search:x"]

    def test_privacy_reports_web_destination_not_omniroute_destination(self) -> None:
        """web_search_destinations carries the web destination, not the OmniRoute one."""
        raw = {"allow_remote_web_search": True, "web_search_destinations": ["web-search:x"]}
        privacy = _privacy(raw, "omniroute:abc", "web-search:x")
        assert privacy.web_search_destinations == ["web-search:x"]
