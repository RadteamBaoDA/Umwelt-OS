"""Unit tests for sources schemas, types, validation, and connector snapshots."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.sources.schemas import (
    ConnectorSource,
    GadgetSourceSelection,
    GadgetSourceSelectionPage,
    OperationRead,
    SourceCreate,
    SourceFence,
    SourceList,
    SourcePatch,
    SourceRead,
    SourceStatus,
    SourceType,
)


class TestSourceTypes:
    """Test allowed SourceType and SourceStatus literal definitions."""

    def test_valid_source_types(self) -> None:
        valid_types: list[SourceType] = [
            "rss",
            "web",
            "file",
            "github",
            "calendar",
            "email",
            "api",
            "mcp",
            "manual",
            "other",
        ]
        for st in valid_types:
            source = SourceCreate(type=st, name=f"Test {st}")
            assert source.type == st

    def test_invalid_source_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceCreate(type="invalid_type", name="Test")  # type: ignore[arg-type]

    def test_valid_source_statuses(self) -> None:
        valid_statuses: list[SourceStatus] = ["active", "paused", "archived"]
        for status in valid_statuses:
            patch = SourcePatch(status=status)
            assert patch.status == status

    def test_invalid_source_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourcePatch(status="deleted")  # type: ignore[arg-type]


class TestSourceCreate:
    """Test validation of SourceCreate schema including registered provider rules."""

    def test_create_without_provider(self) -> None:
        sc = SourceCreate(type="manual", name="Manual Source")
        assert sc.type == "manual"
        assert sc.name == "Manual Source"
        assert sc.provider is None

    @pytest.mark.parametrize(
        ("provider", "expected_type"),
        [
            ("youtube", "rss"),
            ("arxiv", "rss"),
            ("huggingface", "api"),
            ("github_releases", "api"),
            ("telegram", "api"),
        ],
    )
    def test_registered_providers_match_expected_types(
        self, provider: str, expected_type: SourceType
    ) -> None:
        sc = SourceCreate(type=expected_type, name=f"{provider} feed", provider=provider)
        assert sc.provider == provider
        assert sc.type == expected_type

    @pytest.mark.parametrize(
        ("provider", "wrong_type"),
        [
            ("youtube", "api"),
            ("arxiv", "web"),
            ("huggingface", "rss"),
            ("github_releases", "rss"),
            ("telegram", "rss"),
        ],
    )
    def test_registered_providers_reject_mismatched_types(
        self, provider: str, wrong_type: SourceType
    ) -> None:
        with pytest.raises(
            ValidationError, match="Source type does not match the registered provider"
        ):
            SourceCreate(type=wrong_type, name=f"{provider} mismatch", provider=provider)

    def test_unregistered_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Provider is not registered"):
            SourceCreate(type="api", name="Custom Provider", provider="custom_provider")

    def test_name_boundary_conditions(self) -> None:
        # Min length: 1 char
        sc_min = SourceCreate(type="rss", name="a")
        assert sc_min.name == "a"

        # Max length: 200 chars
        name_200 = "x" * 200
        sc_max = SourceCreate(type="rss", name=name_200)
        assert sc_max.name == name_200

        # Empty name rejected
        with pytest.raises(ValidationError):
            SourceCreate(type="rss", name="")

        # Over 200 chars rejected
        with pytest.raises(ValidationError):
            SourceCreate(type="rss", name="x" * 201)

    def test_provider_length_boundary(self) -> None:
        # Registered provider must be registered, but Field max_length is 120
        with pytest.raises(ValidationError):
            SourceCreate(type="api", name="Test", provider="p" * 121)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            SourceCreate(type="rss", name="Test", extra_key="value")  # type: ignore[call-arg]


class TestSourcePatch:
    """Test validation of SourcePatch schema."""

    def test_empty_patch_valid(self) -> None:
        patch = SourcePatch()
        assert patch.name is None
        assert patch.status is None
        assert patch.model_fields_set == set()

    def test_patch_name_only(self) -> None:
        patch = SourcePatch(name="Updated Name")
        assert patch.name == "Updated Name"
        assert patch.status is None
        assert patch.model_fields_set == {"name"}

    def test_patch_status_only(self) -> None:
        patch = SourcePatch(status="paused")
        assert patch.name is None
        assert patch.status == "paused"
        assert patch.model_fields_set == {"status"}

    def test_patch_name_boundaries(self) -> None:
        assert SourcePatch(name="a").name == "a"
        assert SourcePatch(name="b" * 200).name == "b" * 200
        with pytest.raises(ValidationError):
            SourcePatch(name="")
        with pytest.raises(ValidationError):
            SourcePatch(name="b" * 201)

    def test_patch_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            SourcePatch(name="Test", unknown="val")  # type: ignore[call-arg]


class TestSourceRead:
    """Test serialization and attribute binding of SourceRead."""

    def test_source_read_instantiation_from_dict(self) -> None:
        now = datetime.now(UTC)
        source_id = uuid4()
        data = {
            "id": source_id,
            "type": "rss",
            "name": "Tech News",
            "provider": None,
            "status": "active",
            "local_only": False,
            "last_sync_at": now,
            "last_success_at": now,
            "last_error_at": None,
            "last_error_code": None,
            "collected_at": now,
            "indexed_at": now,
            "collection_error_code": None,
            "processing_error_code": None,
            "generation": 1,
            "retired_at": None,
            "created_at": now,
            "updated_at": now,
        }
        read = SourceRead.model_validate(data)
        assert read.id == source_id
        assert read.type == "rss"
        assert read.generation == 1
        assert read.local_only is False

    def test_source_read_from_attributes(self) -> None:
        now = datetime.now(UTC)
        source_id = uuid4()

        class DummyModel:
            id = source_id
            type = "web"
            name = "Documentation"
            provider = None
            status = "active"
            local_only = True
            last_sync_at = None
            last_success_at = None
            last_error_at = now
            last_error_code = "timeout"
            collected_at = None
            indexed_at = None
            collection_error_code = "timeout"
            processing_error_code = None
            generation = 3
            retired_at = None
            created_at = now
            updated_at = now

        read = SourceRead.model_validate(DummyModel(), from_attributes=True)
        assert read.id == source_id
        assert read.last_error_code == "timeout"
        assert read.collection_error_code == "timeout"
        assert read.generation == 3
        assert read.local_only is True


class TestConnectorSource:
    """Test ConnectorSource snapshot schema including configuration, URLs, and intervals."""

    def test_connector_source_with_url_and_interval(self) -> None:
        source_id = uuid4()
        config = {
            "feed_url": "https://example.com/rss.xml",
            "schedule_interval_minutes": 60,
            "timezone": "UTC",
        }
        cs = ConnectorSource(
            workspace_id=uuid4(),
            local_only=False,
            id=source_id,
            type="rss",
            status="active",
            generation=2,
            configuration=config,
            provider="youtube",
        )
        assert cs.id == source_id
        assert cs.type == "rss"
        assert cs.configuration["feed_url"] == "https://example.com/rss.xml"
        assert cs.configuration["schedule_interval_minutes"] == 60
        assert cs.provider == "youtube"

    def test_connector_source_immutability(self) -> None:
        cs = ConnectorSource(
            workspace_id=uuid4(),
            local_only=False,
            id=uuid4(),
            type="rss",
            status="active",
            generation=1,
            configuration={"url": "https://test.com"},
        )
        with pytest.raises(ValidationError):
            cs.status = "paused"  # type: ignore[misc]

    def test_connector_source_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ConnectorSource(
                workspace_id=uuid4(),
                local_only=False,
                id=uuid4(),
                type="rss",
                status="active",
                generation=1,
                configuration={},
                unexpected_field=123,  # type: ignore[call-arg]
            )


class TestSourceFenceAndProjections:
    """Test SourceFence, GadgetSourceSelection, and pagination models."""

    def test_source_fence_fields_and_immutability(self) -> None:
        source_id = uuid4()
        fence = SourceFence(
            workspace_id=uuid4(),
            id=source_id,
            status="active",
            generation=4,
            local_only=False,
        )
        assert fence.id == source_id
        assert fence.generation == 4
        with pytest.raises(ValidationError):
            fence.generation = 5  # type: ignore[misc]
        with pytest.raises(ValidationError):
            SourceFence(
                workspace_id=uuid4(),
                id=source_id,
                status="active",
                generation=4,
                local_only=False,
                extra="disallowed",  # type: ignore[call-arg]
            )

    def test_gadget_source_selection_page(self) -> None:
        source_id = uuid4()
        item = GadgetSourceSelection(
            id=source_id,
            name="News Gadget",
            type="rss",
            provider="arxiv",
            status="active",
            generation=1,
            local_only=False,
        )
        page = GadgetSourceSelectionPage(items=(item,), next_cursor="cursor_abc")
        assert len(page.items) == 1
        assert page.items[0].provider == "arxiv"
        assert page.next_cursor == "cursor_abc"

    def test_source_list_and_operation_read(self) -> None:
        now = datetime.now(UTC)
        op_id = uuid4()
        src_id = uuid4()
        op = OperationRead(
            workspace_id=uuid4(),
            operation_id=op_id,
            source_id=src_id,
            status="queued",
            error_code=None,
            documents_status="queued",
            pending_child_count=0,
            failed_child_count=0,
            pending_owner_codes=[],
            created_at=now,
            updated_at=now,
        )
        assert op.status == "queued"
        assert op.error_code is None
        assert op.documents_status == "queued"

        # Invalid operation status
        with pytest.raises(ValidationError):
            OperationRead(
                workspace_id=uuid4(),
                operation_id=op_id,
                source_id=src_id,
                status="in_progress",  # type: ignore[arg-type]
                error_code=None,
                documents_status="queued",
                pending_child_count=0,
                failed_child_count=0,
                pending_owner_codes=[],
                created_at=now,
                updated_at=now,
            )

        # SourceList page
        s_list = SourceList(items=[], next_cursor=None)
        assert s_list.items == []
        assert s_list.next_cursor is None
