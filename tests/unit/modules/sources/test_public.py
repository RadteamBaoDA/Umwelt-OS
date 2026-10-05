"""Unit tests for public sources functions: locking, lifecycle status, generation, and filters."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.tools.schemas import ToolDestination
from modules.sources import public
from modules.sources.models import Source
from modules.sources.schemas import SourcePatch


def _make_source(
    *,
    source_id: UUID | None = None,
    source_type: str = "rss",
    name: str = "Test Source",
    provider: str | None = None,
    status: str = "active",
    generation: int = 1,
    local_only: bool = False,
    configuration: dict | None = None,
) -> Source:
    """Helper to build a Source ORM model for testing."""
    now = datetime.now(UTC)
    return Source(
        id=source_id or uuid4(),
        type=source_type,
        name=name,
        provider=provider,
        status=status,
        generation=generation,
        local_only=local_only,
        configuration=configuration or {},
        created_at=now,
        updated_at=now,
    )


class TestLockSourceFunctions:
    """Test lock_source, lock_source_for_document, and get_source_fence."""

    @pytest.mark.asyncio
    async def test_lock_source_found(self) -> None:
        src = _make_source(generation=2, status="active", local_only=True)
        session = AsyncMock()
        session.scalar.return_value = src

        fence = await public.lock_source(session, src.id)
        assert fence is not None
        assert fence.id == src.id
        assert fence.status == "active"
        assert fence.generation == 2
        assert fence.local_only is True

    @pytest.mark.asyncio
    async def test_lock_source_not_found(self) -> None:
        session = AsyncMock()
        session.scalar.return_value = None

        fence = await public.lock_source(session, uuid4())
        assert fence is None

    @pytest.mark.asyncio
    async def test_get_source_fence_found(self) -> None:
        src = _make_source(generation=3, status="paused", local_only=False)
        session = AsyncMock()
        session.get.return_value = src

        fence = await public.get_source_fence(session, src.id)
        assert fence is not None
        assert fence.status == "paused"
        assert fence.generation == 3

    @pytest.mark.asyncio
    async def test_get_source_fence_not_found(self) -> None:
        session = AsyncMock()
        session.get.return_value = None

        fence = await public.get_source_fence(session, uuid4())
        assert fence is None

    @pytest.mark.asyncio
    async def test_lock_source_for_document_active(self) -> None:
        src = _make_source(status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        # Should complete without error
        await public.lock_source_for_document(session, src.id)

    @pytest.mark.asyncio
    async def test_lock_source_for_document_not_found_raises(self) -> None:
        session = AsyncMock()
        session.scalar.return_value = None

        with pytest.raises(LookupError, match="Source not found"):
            await public.lock_source_for_document(session, uuid4())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["paused", "archived"])
    async def test_lock_source_for_document_inactive_raises(self, status: str) -> None:
        src = _make_source(status=status)
        session = AsyncMock()
        session.scalar.return_value = src

        with pytest.raises(ValueError, match="Cannot add documents to an inactive source"):
            await public.lock_source_for_document(session, src.id)


class TestSourceConnectorAndConfiguration:
    """Test get_connector_source and set_connector_configuration generation fencing."""

    @pytest.mark.asyncio
    async def test_get_connector_source(self) -> None:
        src = _make_source(
            configuration={"url": "https://example.com", "schedule_interval_minutes": 30}
        )
        session = AsyncMock()
        session.get.return_value = src

        cs = await public.get_connector_source(session, src.id)
        assert cs is not None
        assert cs.id == src.id
        assert cs.configuration["url"] == "https://example.com"
        assert cs.configuration["schedule_interval_minutes"] == 30

        # Verify configuration is defensively copied
        src.configuration["url"] = "https://mutated.com"
        assert cs.configuration["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_get_connector_source_missing(self) -> None:
        session = AsyncMock()
        session.get.return_value = None

        assert await public.get_connector_source(session, uuid4()) is None

    @pytest.mark.asyncio
    async def test_set_connector_configuration_success(self) -> None:
        src = _make_source(generation=1, status="active", configuration={})
        session = AsyncMock()
        session.scalar.return_value = src

        new_config = {"feed_url": "https://news.org/feed.xml"}
        updated = await public.set_connector_configuration(
            session, src.id, expected_generation=1, configuration=new_config
        )
        assert updated is not None
        assert updated.generation == 2
        assert src.generation == 2
        assert src.configuration == new_config
        session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_connector_configuration_generation_mismatch(self) -> None:
        src = _make_source(generation=2, status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        updated = await public.set_connector_configuration(
            session, src.id, expected_generation=1, configuration={"url": "abc"}
        )
        assert updated is None
        assert src.generation == 2

    @pytest.mark.asyncio
    async def test_set_connector_configuration_paused_source(self) -> None:
        src = _make_source(generation=1, status="paused")
        session = AsyncMock()
        session.scalar.return_value = src

        # Fails without allow_paused=True
        updated = await public.set_connector_configuration(
            session, src.id, expected_generation=1, configuration={"a": 1}, allow_paused=False
        )
        assert updated is None

        # Succeeds with allow_paused=True
        updated_allowed = await public.set_connector_configuration(
            session, src.id, expected_generation=1, configuration={"a": 1}, allow_paused=True
        )
        assert updated_allowed is not None
        assert updated_allowed.generation == 2

    @pytest.mark.asyncio
    async def test_set_connector_configuration_archived_always_fails(self) -> None:
        src = _make_source(generation=1, status="archived")
        session = AsyncMock()
        session.scalar.return_value = src

        updated = await public.set_connector_configuration(
            session, src.id, expected_generation=1, configuration={"a": 1}, allow_paused=True
        )
        assert updated is None


class TestSourceSyncAndProcessingState:
    """Test record_collection_started, record_collection_result, record_processing_result."""

    @pytest.mark.asyncio
    async def test_record_collection_started_success(self) -> None:
        now = datetime.now(UTC)
        src = _make_source(generation=1, status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        result = await public.record_collection_started(session, src.id, 1, now)
        assert result is True
        assert src.last_sync_at == now
        assert src.collected_at == now

    @pytest.mark.asyncio
    async def test_record_collection_started_generation_or_status_mismatch(self) -> None:
        now = datetime.now(UTC)
        src = _make_source(generation=2, status="paused")
        session = AsyncMock()
        session.scalar.return_value = src

        # Status is paused
        assert await public.record_collection_started(session, src.id, 2, now) is False
        # Generation is 2, expected 1
        src.status = "active"
        assert await public.record_collection_started(session, src.id, 1, now) is False

    @pytest.mark.asyncio
    async def test_record_collection_result_success(self) -> None:
        now = datetime.now(UTC)
        src = _make_source(generation=1, status="active")
        src.last_error_code = "prev_err"
        src.collection_error_code = "prev_err"
        session = AsyncMock()
        session.scalar.return_value = src

        ok = await public.record_collection_result(session, src.id, 1, now, error_code=None)
        assert ok is True
        assert src.last_success_at == now
        assert src.last_error_code is None
        assert src.collection_error_code is None

    @pytest.mark.asyncio
    async def test_record_collection_result_no_changes(self) -> None:
        now = datetime.now(UTC)
        src = _make_source(generation=1, status="active")
        src.last_error_at = now
        session = AsyncMock()
        session.scalar.return_value = src

        ok = await public.record_collection_result(
            session, src.id, 1, now, error_code=None, no_changes=True
        )
        assert ok is True
        assert src.last_sync_at == now
        assert src.collected_at == now
        assert src.last_error_at is None

    @pytest.mark.asyncio
    async def test_record_collection_result_error(self) -> None:
        now = datetime.now(UTC)
        src = _make_source(generation=1, status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        ok = await public.record_collection_result(
            session, src.id, 1, now, error_code="rate_limit_exceeded"
        )
        assert ok is True
        assert src.collection_error_code == "rate_limit_exceeded"
        assert src.last_error_code == "rate_limit_exceeded"
        assert src.last_error_at == now

    @pytest.mark.asyncio
    async def test_record_processing_result(self) -> None:
        now = datetime.now(UTC)
        src = _make_source(generation=1, status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        # Success
        assert (
            await public.record_processing_result(session, src.id, 1, now, error_code=None) is True
        )
        assert src.last_success_at == now
        assert src.processing_error_code is None

        # Failure
        assert (
            await public.record_processing_result(
                session, src.id, 1, now, error_code="parse_failure"
            )
            is True
        )
        assert src.processing_error_code == "parse_failure"
        assert src.last_error_code == "parse_failure"
        assert src.last_error_at == now


class TestSourceLifecycleMutations:
    """Test update_source, pause_source_for_connector, and archive_source."""

    @pytest.mark.asyncio
    async def test_update_source_name_only(self) -> None:
        src = _make_source(generation=1, name="Old Name")
        session = AsyncMock()
        session.scalar.return_value = src

        with patch("modules.sources.public.commit_with_replay", new_callable=AsyncMock):
            updated = await public.update_source(
                session, src, SourcePatch(name="New Name")
            )
        assert updated is not None
        assert updated.name == "New Name"
        assert updated.generation == 1  # Name only does not increment generation

    @pytest.mark.asyncio
    async def test_update_source_status_increments_generation(self) -> None:
        src = _make_source(generation=1, status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        with (
            patch("modules.sources.public._fence_connector_source", new_callable=AsyncMock),
            patch("modules.sources.public.commit_with_replay", new_callable=AsyncMock),
        ):
            updated = await public.update_source(session, src, SourcePatch(status="paused"))
        assert updated is not None
        assert updated.status == "paused"
        assert updated.generation == 2
        assert updated.retired_at is not None

    @pytest.mark.asyncio
    async def test_archived_source_cannot_be_reactivated(self) -> None:
        src = _make_source(status="archived", generation=3)
        session = AsyncMock()
        session.scalar.return_value = src

        with pytest.raises(ValueError, match="Archived sources cannot be reactivated"):
            await public.update_source(session, src, SourcePatch(status="active"))

    @pytest.mark.asyncio
    async def test_pause_source_for_connector(self) -> None:
        src = _make_source(generation=2, status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        with patch("modules.sources.public._fence_connector_source", new_callable=AsyncMock):
            cs = await public.pause_source_for_connector(session, src.id)
        assert cs is not None
        assert cs.status == "paused"
        assert cs.generation == 3
        assert src.status == "paused"
        assert src.generation == 3

    @pytest.mark.asyncio
    async def test_archive_source(self) -> None:
        src = _make_source(generation=1, status="active")
        session = AsyncMock()
        session.scalar.return_value = src

        with (
            patch("modules.sources.public._fence_connector_source", new_callable=AsyncMock),
            patch("modules.sources.public.commit_with_replay", new_callable=AsyncMock),
        ):
            archived = await public.archive_source(session, src.id)
        assert archived is not None
        assert archived.status == "archived"
        assert archived.generation == 2
        assert archived.retired_at is not None


class TestSourceFiltersAndProjections:
    """Test get_tool_source, list_tool_sources, and gadget source projections."""

    @pytest.mark.asyncio
    async def test_get_gadget_sources_bounds_and_ordering(self) -> None:
        from collections import namedtuple

        Row = namedtuple(
            "Row", ["id", "name", "type", "provider", "status", "generation", "local_only"]
        )
        session = AsyncMock()
        id1, id2 = uuid4(), uuid4()

        # Reject more than 32 IDs
        with pytest.raises(ValueError, match="At most 32 distinct source IDs"):
            await public.get_gadget_sources(session, tuple(uuid4() for _ in range(33)))

        # Reject duplicate IDs
        with pytest.raises(ValueError, match="At most 32 distinct source IDs"):
            await public.get_gadget_sources(session, (id1, id1))

        # Empty request returns empty
        assert await public.get_gadget_sources(session, ()) == ()

        # Ordered result
        row1 = Row(id1, "Source 1", "rss", None, "active", 1, False)
        row2 = Row(id2, "Source 2", "web", None, "active", 2, True)

        mock_result = MagicMock()
        mock_result.__iter__.return_value = [row2, row1]  # Returned out of order by DB
        session.execute.return_value = mock_result

        # Caller requested (id1, id2): order must match request
        results = await public.get_gadget_sources(session, (id1, id2))
        assert len(results) == 2
        assert results[0].id == id1
        assert results[0].name == "Source 1"
        assert results[1].id == id2
        assert results[1].name == "Source 2"

    @pytest.mark.asyncio
    async def test_list_active_gadget_sources_limits(self) -> None:
        session = AsyncMock()
        with pytest.raises(ValueError, match="Active source projection limit"):
            await public.list_active_gadget_sources(session, limit=0)
        with pytest.raises(ValueError, match="Active source projection limit"):
            await public.list_active_gadget_sources(session, limit=33)

    @pytest.mark.asyncio
    async def test_list_gadget_sources_limits(self) -> None:
        session = AsyncMock()
        with pytest.raises(ValueError, match="Source selection page limit"):
            await public.list_gadget_sources(session, limit=0)
        with pytest.raises(ValueError, match="Source selection page limit"):
            await public.list_gadget_sources(session, limit=101)

    @pytest.mark.asyncio
    async def test_list_tool_sources_limits(self) -> None:
        session = AsyncMock()
        with pytest.raises(ValueError, match="Source tool page size is outside"):
            await public.list_tool_sources(session, limit=0, cursor=None, source_ids=frozenset())
        with pytest.raises(ValueError, match="Source tool page size is outside"):
            await public.list_tool_sources(session, limit=101, cursor=None, source_ids=frozenset())

    @pytest.mark.asyncio
    async def test_list_tool_sources_empty_ids_without_owner_all(self) -> None:
        session = AsyncMock()
        page = await public.list_tool_sources(
            session, limit=10, cursor=None, source_ids=frozenset(), owner_all=False
        )
        assert page.items == ()
        assert page.next_cursor is None

    @pytest.mark.asyncio
    async def test_get_tool_source_destination_and_scope_filtering(self) -> None:
        from collections import namedtuple

        ToolRow = namedtuple(
            "ToolRow", ["id", "name", "type", "status", "generation", "local_only", "created_at"]
        )
        session = AsyncMock()
        source_id = uuid4()
        now = datetime.now(UTC)

        # 1. Not in scope and owner_all=False -> None
        res = await public.get_tool_source(
            session, source_id, source_ids=frozenset(), owner_all=False
        )
        assert res is None

        # 2. Local-only row requested from REMOTE destination -> None
        row_tuple = ToolRow(source_id, "Local Only", "manual", "active", 1, True, now)
        result_mock = MagicMock()
        result_mock.one_or_none.return_value = row_tuple
        session.execute.return_value = result_mock

        res_remote = await public.get_tool_source(
            session,
            source_id,
            source_ids=frozenset([source_id]),
            owner_all=False,
            destination=ToolDestination.REMOTE,
        )
        assert res_remote is None

        # 3. Local destination accepts local_only row
        res_local = await public.get_tool_source(
            session,
            source_id,
            source_ids=frozenset([source_id]),
            owner_all=False,
            destination=ToolDestination.LOCAL,
        )
        assert res_local is not None
        assert res_local.id == source_id
        assert res_local.local_only is True
