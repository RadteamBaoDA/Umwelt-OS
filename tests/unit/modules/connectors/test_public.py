"""Unit tests for connectors public contracts, discovery, registry, and status checks.

Covers:
- Connector registry (registry.configuration, registry.validate, registry.health, registry.sync, registry.normalize)
- Public discovery & provider catalog (list_catalog, get_catalog_entry, capability filters and availability states)
- Query contracts (ConnectorConfig validations, get_connector_configuration, get_current_provider_scope, overlap_floor)
- Status checks and access guards (collection_allowed, agent_browser_target_in_scope, validate_public_url)
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.connectors.catalog import (
    CatalogEntry,
    get_catalog_entry,
    list_catalog,
)
from modules.connectors.models import ConnectorProvisioning
from modules.connectors.public import (
    DEFAULT_OVERLAP,
    DEFAULT_TIMEZONE,
    NATIVE_PROVIDERS,
    AgentBrowserScope,
    CollectionFence,
    ConnectorConfig,
    ConnectorConfigurationSnapshot,
    ProviderScopeSnapshot,
    agent_browser_target_in_scope,
    collection_allowed,
    default_schedule_interval_minutes,
    get_connector_configuration,
    get_current_provider_scope,
    is_native_provider,
    overlap_floor,
    validate_public_url,
)
from modules.connectors.registry import (
    configuration,
    health,
    normalize,
    sync,
    validate,
)
from modules.sources.schemas import ConnectorSource, SourceFence


class TestConnectorRegistry:
    """Tests for connector registry validation, configuration defaults, and record normalization."""

    def test_is_native_provider(self) -> None:
        """Verify native provider detection against the approved NATIVE_PROVIDERS set."""
        for provider in ("youtube", "arxiv", "huggingface", "github_releases", "github", "telegram"):
            assert is_native_provider(provider) is True

        assert is_native_provider("rss") is False
        assert is_native_provider("web") is False
        assert is_native_provider("custom_api") is False
        assert is_native_provider(None) is False

    def test_registry_configuration_defaults(self) -> None:
        """Native providers default to 30s timeout; generic providers default to 60s."""
        # Generic RSS source
        generic_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            status="active",
            generation=1,
            configuration={"feed_url": "https://example.com/feed.xml"},
        )
        generic_cfg = configuration(generic_source)
        assert generic_cfg.timeout_seconds == 60

        # Native YouTube source
        native_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            provider="youtube",
            status="active",
            generation=1,
            configuration={
                "youtube_channel_id": "UC1234567890123456789012",
                "history_mode": "returned_snapshot",
            },
        )
        native_cfg = configuration(native_source)
        assert native_cfg.timeout_seconds == 30

        # Custom timeout preserved
        custom_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            provider="youtube",
            status="active",
            generation=1,
            configuration={
                "youtube_channel_id": "UC1234567890123456789012",
                "history_mode": "returned_snapshot",
                "timeout_seconds": 15,
            },
        )
        assert configuration(custom_source).timeout_seconds == 15

    def test_registry_validate_inactive_source_rejected(self) -> None:
        """Paused or archived sources cannot be validated for active collection."""
        source = ConnectorSource(
            id=uuid4(),
            type="rss",
            status="paused",
            generation=1,
            configuration={"feed_url": "https://example.com/feed.xml"},
        )
        with pytest.raises(ValueError, match="Source is not active"):
            validate(source)

    def test_registry_validate_unsupported_type_rejected(self) -> None:
        """Sources with unsupported types raise ValueError."""
        source = ConnectorSource(
            id=uuid4(),
            type="unsupported_type",
            status="active",
            generation=1,
            configuration={},
        )
        with pytest.raises(ValueError, match="no packaged connector"):
            validate(source)

    def test_registry_validate_native_providers_success(self) -> None:
        """Valid configurations for registered native providers pass validation."""
        # YouTube
        yt_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            provider="youtube",
            status="active",
            generation=1,
            configuration={
                "youtube_channel_id": "UC1234567890123456789012",
                "history_mode": "returned_snapshot",
            },
        )
        yt_res = validate(yt_source)
        assert yt_res["provider"] == "youtube"
        assert yt_res["configuration"]["youtube_channel_id"] == "UC1234567890123456789012"

        # ArXiv
        arxiv_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            provider="arxiv",
            status="active",
            generation=1,
            configuration={
                "arxiv_category": "cs.AI",
                "history_mode": "returned_snapshot",
            },
        )
        arxiv_res = validate(arxiv_source)
        assert arxiv_res["provider"] == "arxiv"
        assert arxiv_res["configuration"]["arxiv_category"] == "cs.AI"

        # Telegram
        tg_source = ConnectorSource(
            id=uuid4(),
            type="api",
            provider="telegram",
            status="active",
            generation=1,
            configuration={
                "telegram_chat_ids": ["-1001234567890"],
                "history_mode": "pending_updates",
            },
        )
        tg_res = validate(tg_source)
        assert tg_res["provider"] == "telegram"

    def test_registry_validate_native_timeout_limit(self) -> None:
        """Native providers cannot exceed 30 seconds request timeout."""
        source = ConnectorSource(
            id=uuid4(),
            type="rss",
            provider="youtube",
            status="active",
            generation=1,
            configuration={
                "youtube_channel_id": "UC1234567890123456789012",
                "history_mode": "returned_snapshot",
                "timeout_seconds": 31,
            },
        )
        with pytest.raises(ValueError, match="timeout cannot exceed 30 seconds"):
            validate(source)

    def test_registry_validate_generic_api_port_restriction(self) -> None:
        """REST connectors require items_path and reject non-standard HTTP(S) ports."""
        # Missing items_path
        source_missing_items = ConnectorSource(
            id=uuid4(),
            type="api",
            status="active",
            generation=1,
            configuration={"url": "https://api.example.com/items"},
        )
        with pytest.raises(ValueError, match="requires items_path"):
            validate(source_missing_items)

        # Non-standard port
        source_bad_port = ConnectorSource(
            id=uuid4(),
            type="api",
            status="active",
            generation=1,
            configuration={"url": "https://api.example.com:8443/items", "items_path": "data"},
        )
        with pytest.raises(ValueError, match="default HTTP\\(S\\) port"):
            validate(source_bad_port)

    def test_registry_health_reporting(self) -> None:
        """health() correctly differentiates ready, misconfigured, and inactive sources."""
        # Ready generic RSS
        ready_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            status="active",
            generation=1,
            configuration={"feed_url": "https://example.com/rss.xml"},
        )
        assert health(ready_source) == {"status": "ready", "connector": "rss"}

        # Misconfigured active source (missing feed_url)
        bad_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            status="active",
            generation=1,
            configuration={},
        )
        assert health(bad_source) == {"status": "misconfigured", "connector": "rss"}

        # Inactive source
        paused_source = ConnectorSource(
            id=uuid4(),
            type="rss",
            status="paused",
            generation=1,
            configuration={"feed_url": "https://example.com/rss.xml"},
        )
        assert health(paused_source) == {"status": "paused", "connector": "rss"}

    def test_registry_sync_payload(self) -> None:
        """sync() constructs validated connector payloads with cursor state."""
        source = ConnectorSource(
            id=uuid4(),
            type="rss",
            status="active",
            generation=1,
            configuration={"feed_url": "https://example.com/rss.xml"},
        )
        payload = sync(source, cursor="2026-10-01T00:00:00Z")
        assert payload["type"] == "rss"
        assert payload["url"] == "https://example.com/rss.xml"
        assert "cursor_before" in payload
        assert "catch_up_since" in payload

    def test_registry_normalize_bounds_and_timestamps(self) -> None:
        """normalize() caps content length, provider_id length, and ensures UTC ISO observed_at."""
        raw_record = {
            "provider_id": "x" * 600,
            "content": "a" * 1_200_000,
            "version": "v" * 300,
            "observed_at": "2026-10-05T12:00:00+07:00",
            "metadata": {"key": "val"},
        }
        normalized = normalize(raw_record)
        assert len(normalized["provider_id"]) == 512
        assert len(normalized["content"]) == 1_000_000
        assert len(normalized["version"]) == 255
        assert normalized["metadata"] == {"key": "val"}
        # Verify UTC conversion: 12:00 +07:00 is 05:00 UTC
        assert normalized["observed_at"] == "2026-10-05T05:00:00+00:00"


class TestPublicDiscoveryAndCatalog:
    """Tests for public provider catalog listing, lookups, and capability filters."""

    def test_list_catalog_returns_all_entries(self) -> None:
        """list_catalog returns an immutable tuple of declared CatalogEntry records."""
        entries = list_catalog()
        assert isinstance(entries, tuple)
        assert len(entries) >= 15
        assert all(isinstance(e, CatalogEntry) for e in entries)

        # Check key providers are present
        provider_ids = {e.provider_id for e in entries}
        expected = {"rss", "web", "rest", "github", "telegram", "mcp", "youtube", "arxiv", "huggingface", "github_releases"}
        assert expected.issubset(provider_ids)

    def test_get_catalog_entry_lookup(self) -> None:
        """get_catalog_entry returns exact CatalogEntry or None when missing."""
        entry = get_catalog_entry("github")
        assert entry is not None
        assert entry.provider_id == "github"
        assert entry.label == "GitHub"
        assert entry.auth_methods == ("oauth2",)
        assert "github_owner" in entry.scope_fields
        assert entry.availability == "requires_credentials"

        assert get_catalog_entry("non_existent_provider_id") is None

    def test_catalog_capability_filters(self) -> None:
        """Capability properties reflect provider support for history, edit, and deletion."""
        rss = get_catalog_entry("rss")
        assert rss is not None
        assert rss.supports_history is True
        assert rss.supports_edit is False
        assert rss.supports_delete is False

        mcp = get_catalog_entry("mcp")
        assert mcp is not None
        assert mcp.supports_history is False
        assert mcp.supports_edit is True
        assert mcp.supports_delete is False

        telegram = get_catalog_entry("telegram")
        assert telegram is not None
        assert telegram.supports_history is True
        assert telegram.supports_edit is True
        assert telegram.supports_delete is False

    def test_catalog_availability_states(self) -> None:
        """Catalog entries declare valid availability categories."""
        valid_availabilities = {
            "implemented", "requires_credentials", "unsupported_operation",
            "planned", "unavailable", "available",
        }
        for entry in list_catalog():
            assert entry.availability in valid_availabilities


class TestQueryContracts:
    """Tests for Pydantic configuration validation and query snapshots."""

    def test_connector_config_timezone_validation(self) -> None:
        """Timezones must be recognized by the IANA database."""
        # Valid timezones
        cfg1 = ConnectorConfig(timezone="Asia/Ho_Chi_Minh")
        assert cfg1.timezone == "Asia/Ho_Chi_Minh"
        cfg2 = ConnectorConfig(timezone="UTC")
        assert cfg2.timezone == "UTC"

        # Invalid timezone
        with pytest.raises(ValidationError, match="valid IANA timezone"):
            ConnectorConfig(timezone="Fake/Timezone_Invalid")

    def test_connector_config_arxiv_category_validation(self) -> None:
        """arXiv category must not contain path separators +, ,, /."""
        valid = ConnectorConfig(arxiv_category="math.PR")
        assert valid.arxiv_category == "math.PR"

        with pytest.raises(ValidationError):
            ConnectorConfig(arxiv_category="cs.AI+cs.LG")

        with pytest.raises(ValidationError):
            ConnectorConfig(arxiv_category="cs.AI/cs.LG")

    def test_connector_config_github_repository_validation(self) -> None:
        """GitHub repositories cannot be '.', '..', or end in '.git'."""
        valid = ConnectorConfig(github_repository="Umwelt-OS")
        assert valid.github_repository == "Umwelt-OS"

        with pytest.raises(ValidationError, match="without a path suffix"):
            ConnectorConfig(github_repository="repo.git")

        with pytest.raises(ValidationError, match="without a path suffix"):
            ConnectorConfig(github_repository="..")

    def test_connector_config_telegram_chat_ids_validation(self) -> None:
        """Telegram chat IDs must be unique signed decimal strings."""
        valid = ConnectorConfig(telegram_chat_ids=("-1001234567890", "987654321"))
        assert valid.telegram_chat_ids == ("-1001234567890", "987654321")

        # Duplicate chat IDs
        with pytest.raises(ValidationError, match="unique signed decimal strings"):
            ConnectorConfig(telegram_chat_ids=("-1001234567890", "-1001234567890"))

        # Non-decimal characters
        with pytest.raises(ValidationError, match="unique signed decimal strings"):
            ConnectorConfig(telegram_chat_ids=("@channel_username",))

    @pytest.mark.asyncio
    async def test_get_connector_configuration_contract(self) -> None:
        """get_connector_configuration returns a redact-safe ConnectorConfigurationSnapshot."""
        source_id = uuid4()
        source_fence = SourceFence(id=source_id, generation=1, status="active", local_only=False)
        source = ConnectorSource(
            id=source_id,
            type="rss",
            status="active",
            generation=1,
            configuration={"feed_url": "https://example.com/rss.xml", "timezone": "UTC"},
        )
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=2,
            desired_enabled=True,
            state="active",
            desired_configuration={"auth_method": "none"},
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning.lock_connector", return_value=(source_fence, row, {})), \
             patch("modules.sources.public.get_connector_source", return_value=source):
            snapshot = await get_connector_configuration(session, source_id)

        assert snapshot is not None
        assert isinstance(snapshot, ConnectorConfigurationSnapshot)
        assert snapshot.source_id == source_id
        assert snapshot.expected_revision == 2
        assert snapshot.activation_state == "active"
        assert snapshot.desired_enabled is True
        assert snapshot.auth_method == "none"

    @pytest.mark.asyncio
    async def test_get_current_provider_scope(self) -> None:
        """get_current_provider_scope hashes non-secret scope fields deterministically."""
        source_id = uuid4()
        source = ConnectorSource(
            id=source_id,
            type="rss",
            status="active",
            generation=1,
            configuration={"feed_url": "https://example.com/feed.xml"},
        )
        session = AsyncMock()

        with patch("modules.sources.public.get_connector_source", return_value=source):
            scope = await get_current_provider_scope(session, source_id, expected_source_generation=1)

        assert scope is not None
        assert isinstance(scope, ProviderScopeSnapshot)
        assert scope.source_id == source_id
        assert scope.provider_id == "rss"
        assert len(scope.discriminator) == 64  # SHA-256 hex digest

    def test_default_schedule_interval_minutes(self) -> None:
        """default_schedule_interval_minutes returns correct defaults by source type."""
        assert default_schedule_interval_minutes("rss") == 15
        assert default_schedule_interval_minutes("web") == 30
        assert default_schedule_interval_minutes("api") == 30

    def test_overlap_floor(self) -> None:
        """overlap_floor returns a 1-day UTC subtracted instant for valid ISO timestamps."""
        assert overlap_floor(None) is None
        assert overlap_floor("invalid-date-string") is None

        cursor = "2026-10-05T12:00:00Z"
        floor = overlap_floor(cursor)
        assert floor is not None
        expected = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
        assert floor == expected


class TestStatusChecksAndGuards:
    """Tests for collection authorization, browser scope bounds, and URL guards."""

    @pytest.mark.asyncio
    async def test_collection_allowed_delegates_to_provisioning(self) -> None:
        """collection_allowed constructs ConnectorSource and delegates to require_collection_fence."""
        source_id = uuid4()
        fence = CollectionFence(source_generation=1, connector_revision=2)
        session = AsyncMock()

        with patch("modules.connectors.provisioning.require_collection_fence", new_callable=AsyncMock) as mock_fence:
            mock_fence.return_value = True
            allowed = await collection_allowed(
                session,
                source_id=source_id,
                source_status="active",
                source_generation=1,
                fence=fence,
            )

        assert allowed is True
        mock_fence.assert_awaited_once()

    def test_agent_browser_target_in_scope_allowed(self) -> None:
        """Valid target URL matching granted origin and path prefix returns True."""
        scope = AgentBrowserScope(
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            grant_revision=1,
            scope_hash="hash",
            origin="https://example.com",
            path_prefix="/docs",
            local_only=False,
            enabled=True,
        )

        assert agent_browser_target_in_scope(scope, "https://example.com/docs") is True
        assert agent_browser_target_in_scope(scope, "https://example.com/docs/api") is True
        assert agent_browser_target_in_scope(scope, "https://example.com/docs/sub/page") is True

    def test_agent_browser_target_in_scope_rejected_reasons(self) -> None:
        """agent_browser_target_in_scope rejects targets violating security invariants."""
        scope = AgentBrowserScope(
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            grant_revision=1,
            scope_hash="hash",
            origin="https://example.com",
            path_prefix="/docs",
            local_only=False,
            enabled=True,
        )

        # Insecure scheme HTTP
        assert agent_browser_target_in_scope(scope, "http://example.com/docs") is False
        # Mismatched origin
        assert agent_browser_target_in_scope(scope, "https://other.com/docs") is False
        # Path traversal
        assert agent_browser_target_in_scope(scope, "https://example.com/docs/../private") is False
        # Query parameters
        assert agent_browser_target_in_scope(scope, "https://example.com/docs?query=1") is False
        # Fragments
        assert agent_browser_target_in_scope(scope, "https://example.com/docs#anchor") is False
        # Credentials in URL
        assert agent_browser_target_in_scope(scope, "https://user:pass@example.com/docs") is False
        # Encoded separators
        assert agent_browser_target_in_scope(scope, "https://example.com/docs%2fsub") is False
        assert agent_browser_target_in_scope(scope, "https://example.com/docs%5csub") is False
        assert agent_browser_target_in_scope(scope, "https://example.com/docs%25sub") is False
        # Backslash in path
        assert agent_browser_target_in_scope(scope, "https://example.com/docs\\admin") is False
        # Outside path prefix
        assert agent_browser_target_in_scope(scope, "https://example.com/other") is False

    @pytest.mark.asyncio
    async def test_validate_public_url_valid_and_invalid(self) -> None:
        """validate_public_url enforces credential-free HTTP(S) and public IP resolution."""
        # Non-HTTP(S) scheme
        with pytest.raises(ValueError, match="Only credential-free HTTP\\(S\\) URLs are allowed"):
            await validate_public_url("ftp://example.com/file")

        # Credentials embedded
        with pytest.raises(ValueError, match="Only credential-free HTTP\\(S\\) URLs are allowed"):
            await validate_public_url("https://user:pass@example.com/feed")

        # Resolves to private IP
        with patch("modules.connectors.public.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            with pytest.raises(ValueError, match="URL resolves to a non-public address"):
                await validate_public_url("https://localhost/feed")

        with patch("modules.connectors.public.getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.1", 443))]):
            with pytest.raises(ValueError, match="URL resolves to a non-public address"):
                await validate_public_url("https://router.local/feed")

        # Resolves to public IP
        with patch("modules.connectors.public.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
            res = await validate_public_url("https://example.com/feed.xml")
            assert res == "https://example.com/feed.xml"
