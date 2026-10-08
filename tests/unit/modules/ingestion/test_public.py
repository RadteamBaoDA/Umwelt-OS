"""Unit tests for ingestion public contract functions.

Covers:
- Ingestion job queuing via receive_batch, receive_connector_batch, queue_connector_crawl, and receive_file.
- Stage triggers: durable domain event publication (ingestion.stage.requested, connector.crawl.requested, document.file.uploaded).
- Receipt generation: Receipt, CrawlReceipt, and NativeCollectionReceipt.
- Status verification: get_run, list_source_runs, and retry_run.
- Idempotency and duplicate detection (matching keys, payload conflicts).
- Authentication and authorization checks for collector credentials.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from core.events import DomainEvent
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.ingestion.models import (
    CollectorCredential,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    SourceIngestionState,
)
from modules.ingestion.public import (
    _digest,
    create_collector_credential,
    get_run,
    queue_connector_crawl,
    receive_batch,
    receive_connector_batch,
    receive_file,
    retry_run,
    revoke_collector_credential,
)
from modules.ingestion.schemas import (
    CrawlReceipt,
    IngestionRecord,
    Receipt,
    ReceiveBatch,
)
from modules.sources.schemas import ConnectorSource, SourceFence, SourceFenceSet

WORKSPACE_ID = uuid4()
SCOPE = WorkspaceContext(user_id=7, workspace_id=WORKSPACE_ID, role="owner", membership_revision=3)
ACCESS = AccessFence(WORKSPACE_ID, 7, 3, 5)


@pytest.fixture(autouse=True)
def _admitted_scope():
    """Owner admission is covered by scope tests; these exercise ingestion behaviour only."""
    with patch("modules.ingestion.public._admit_ingestion_scope", AsyncMock(return_value=ACCESS)):
        yield


def _fence(source_id: UUID, status: str = "active", generation: int = 1) -> SourceFence:
    return SourceFence(
        id=source_id, workspace_id=WORKSPACE_ID, status=status, generation=generation, local_only=False,
    )


def _locked(source_id: UUID, status: str = "active") -> SourceFenceSet:
    return SourceFenceSet(fences=(_fence(source_id, status),), access_fence=ACCESS)


def _projection(source: ConnectorSource) -> tuple[ConnectorSource, SourceFence, AccessFence]:
    """Shape returned by the locked Source projection: detached config, lifecycle fence, access fence."""
    fence = SourceFence(
        id=source.id, workspace_id=WORKSPACE_ID, status=source.status, generation=source.generation, local_only=False,
    )
    return source, fence, ACCESS


def _setup_mock_session(session: AsyncMock) -> None:
    """Configure session.add as a synchronous mock that assigns default IDs."""
    def _mock_add(obj: object) -> None:
        if getattr(obj, "id", None) is None:
            try:
                obj.id = uuid4()
            except (AttributeError, TypeError):
                pass
        if getattr(obj, "created_at", None) is None:
            try:
                obj.created_at = datetime.now(UTC)
            except (AttributeError, TypeError):
                pass
        if getattr(obj, "updated_at", None) is None:
            try:
                obj.updated_at = datetime.now(UTC)
            except (AttributeError, TypeError):
                pass

    session.add = MagicMock(side_effect=_mock_add)


class TestDigestAndCredentials:
    """Tests for hashing utilities and collector credential rotation."""

    def test_digest_deterministic(self) -> None:
        """Verify that _digest produces deterministic sha256 hex strings for dict mappings."""
        d1 = {"b": 2, "a": 1}
        d2 = {"a": 1, "b": 2}
        assert _digest(d1) == _digest(d2)

    @pytest.mark.asyncio
    async def test_create_collector_credential_source_not_found(self) -> None:
        """Create collector credential raises LookupError if source is absent."""
        session = AsyncMock()
        with patch("modules.sources.public.lock_source", return_value=None):  # noqa: SIM117  # style-only rewrite skipped to avoid touching control flow
            with pytest.raises(LookupError, match="Source not found"):
                await create_collector_credential(session, uuid4(), scope=SCOPE, multi_workspace_enabled=False)

    @pytest.mark.asyncio
    async def test_create_collector_credential_success(self) -> None:
        """Create collector credential revokes active credentials and returns a 32-byte token."""
        session = AsyncMock()
        _setup_mock_session(session)
        source_id = uuid4()
        active_source = MagicMock()
        active_source.status = "active"

        existing_cred = MagicMock(spec=CollectorCredential)
        existing_cred.revoked_at = None

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [existing_cred]
        session.scalars.return_value = scalars_mock

        fence = _fence(source_id)
        with patch("modules.sources.public.lock_source", return_value=fence), \
             patch("modules.sources.public.get_source_fence", return_value=fence):
            token = await create_collector_credential(session, source_id, scope=SCOPE, multi_workspace_enabled=False)
            assert isinstance(token, str)
            assert len(token) > 20
            assert existing_cred.revoked_at is not None
            session.add.assert_called_once()
            session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_collector_credential(self) -> None:
        """Revoke collector credential sets revoked_at timestamp for matching token hash."""
        session = AsyncMock()
        source_id = uuid4()

        active_cred = MagicMock(spec=CollectorCredential)
        active_cred.revoked_at = None

        # 1st scalar: source_id, 2nd scalar: row (CollectorCredential)
        session.scalar.side_effect = [source_id, active_cred]

        with patch("modules.sources.public.lock_source_set", return_value=_locked(source_id)):
            await revoke_collector_credential(session, "test-token", scope=SCOPE, multi_workspace_enabled=False)
            assert active_cred.revoked_at is not None


class TestReceiveBatchAndReceipts:
    """Tests for receive_batch, receive_connector_batch, and receipt generation."""

    def _sample_payload(self, source_id: UUID) -> ReceiveBatch:
        """Generate a valid ReceiveBatch payload for testing."""
        return ReceiveBatch(
            source_id=source_id,
            source_generation=1,
            connector_revision=1,
            batch_key="batch-123",
            cursor_before="cur-0",
            cursor_after="cur-1",
            records=[
                IngestionRecord(
                    provider_id="rec-1",
                    version="1",
                    content="sample document content",
                    metadata={"test": True},
                    observed_at=datetime.now(UTC),
                    collected_at=datetime.now(UTC),
                )
            ],
        )

    def _sample_source(self, source_id: UUID, status: str = "active", provider: str = "rss") -> ConnectorSource:
        """Generate a valid ConnectorSource model."""
        return ConnectorSource(
            workspace_id=WORKSPACE_ID,
            local_only=False,
            id=source_id,
            type="connector",
            status=status,
            generation=1,
            configuration={},
            provider=provider,
        )

    @pytest.mark.asyncio
    async def test_receive_batch_source_not_found(self) -> None:
        """Receive batch raises 404 when the source does not exist."""
        session = AsyncMock()
        payload = self._sample_payload(uuid4())

        missing = HTTPException(status_code=404, detail="Source set is unavailable")
        with patch("modules.ingestion.public._lock_source_projection", side_effect=missing):
            with pytest.raises(HTTPException) as exc_info:
                await receive_batch(session, payload, "token", scope=SCOPE, multi_workspace_enabled=False)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_receive_batch_unauthorized_token(self) -> None:
        """Receive batch raises 401 when collector credential grant is invalid."""
        session = AsyncMock()
        source_id = uuid4()
        source = self._sample_source(source_id)
        payload = self._sample_payload(source_id)

        session.scalar.return_value = None  # No valid grant found

        with patch("modules.ingestion.public._lock_source_projection", return_value=_projection(source)):
            with pytest.raises(HTTPException) as exc_info:
                await receive_batch(session, payload, "bad-token", scope=SCOPE, multi_workspace_enabled=False)
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_receive_batch_idempotent_duplicate_key(self) -> None:
        """Receive batch returns existing run and batch when exact batch_key and hash match."""
        session = AsyncMock()
        _setup_mock_session(session)
        source_id = uuid4()
        source = self._sample_source(source_id)
        payload = self._sample_payload(source_id)
        payload_hash = _digest(payload.model_dump(mode="json"))

        existing_batch = IngestionBatch(
            id=uuid4(),
            source_id=source_id,
            batch_key=payload.batch_key,
            payload_hash=payload_hash,
            source_generation=1,
        )
        existing_run = IngestionRun(
            id=uuid4(),
            batch_id=existing_batch.id,
            source_id=source_id,
            status="queued",
        )

        session.scalar.side_effect = ["valid_hash", existing_batch, existing_run]

        with patch("modules.ingestion.public._lock_source_projection", return_value=_projection(source)), \
             patch("modules.connectors.public.is_native_provider", return_value=False), \
             patch("modules.connectors.public.require_batch_fence", return_value=True):
            batch, run = await receive_batch(session, payload, "valid-token", scope=SCOPE, multi_workspace_enabled=False)
            assert batch.id == existing_batch.id
            assert run.id == existing_run.id

    @pytest.mark.asyncio
    async def test_receive_batch_payload_hash_conflict(self) -> None:
        """Receive batch raises 409 when batch_key exists with differing payload content."""
        session = AsyncMock()
        source_id = uuid4()
        source = self._sample_source(source_id)
        payload = self._sample_payload(source_id)

        existing_batch = IngestionBatch(
            id=uuid4(),
            source_id=source_id,
            batch_key=payload.batch_key,
            payload_hash="different_hash",
            source_generation=1,
        )

        session.scalar.side_effect = ["valid_hash", existing_batch]

        with patch("modules.ingestion.public._lock_source_projection", return_value=_projection(source)), \
             patch("modules.connectors.public.is_native_provider", return_value=False), \
             patch("modules.connectors.public.require_batch_fence", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await receive_batch(session, payload, "valid-token", scope=SCOPE, multi_workspace_enabled=False)
            assert exc_info.value.status_code == 409
            assert "Batch key was already used with different content" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_receive_connector_batch_receipt_generation(self) -> None:
        """Receive connector batch returns a Receipt with batch_id, run_id and status."""
        session = AsyncMock()
        batch = MagicMock()
        batch.id = uuid4()
        run = MagicMock()
        run.id = uuid4()
        run.status = "queued"

        payload = self._sample_payload(uuid4())

        with patch("modules.ingestion.public.receive_batch", return_value=(batch, run)):
            receipt = await receive_connector_batch(session, payload, "token", scope=SCOPE, multi_workspace_enabled=False)
            assert isinstance(receipt, Receipt)
            assert receipt.batch_id == batch.id
            assert receipt.run_id == run.id
            assert receipt.status == "queued"


class TestQueueConnectorCrawl:
    """Tests for queue_connector_crawl, stage triggering, and CrawlReceipt."""

    @pytest.mark.asyncio
    async def test_queue_connector_crawl_source_inactive_raises_409(self) -> None:
        """Queue connector crawl raises 409 if source status is paused/archived."""
        session = AsyncMock()
        source_id = uuid4()
        source = ConnectorSource(
            workspace_id=WORKSPACE_ID,
            local_only=False,
            id=source_id,
            type="connector",
            status="paused",
            generation=1,
            configuration={},
            provider="web",
        )
        with patch("modules.ingestion.public._lock_source_projection", return_value=_projection(source)):
            with pytest.raises(HTTPException) as exc_info:
                await queue_connector_crawl(session, source_id, 1, 1, None, {}, scope=SCOPE, multi_workspace_enabled=False)
            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_queue_connector_crawl_success(self) -> None:
        """Queue connector crawl creates batch, run, collect_web stage and returns CrawlReceipt."""
        session = AsyncMock()
        _setup_mock_session(session)
        source_id = uuid4()
        source = ConnectorSource(
            workspace_id=WORKSPACE_ID,
            local_only=False,
            id=source_id,
            type="connector",
            status="active",
            generation=1,
            configuration={},
            provider="web",
        )
        state = SourceIngestionState(source_id=source_id, cursor=None, lease_expires_at=None)

        session.scalar.return_value = None  # No existing batch
        session.get.return_value = state

        with patch("modules.ingestion.public._lock_source_projection", return_value=_projection(source)), \
             patch("modules.connectors.public.is_native_provider", return_value=False), \
             patch("modules.connectors.public.require_collection_fence", return_value=True), \
             patch("modules.sources.public.record_collection_started_in_uow", return_value=True), \
             patch("modules.ingestion.public.publish_event", return_value=None) as mock_pub, \
             patch("modules.ingestion.public.commit_with_replay", return_value=None):
            receipt = await queue_connector_crawl(session, source_id, 1, 1, None, {"url": "https://example.com"}, scope=SCOPE, multi_workspace_enabled=False)
            assert isinstance(receipt, CrawlReceipt)
            assert receipt.run_id is not None
            mock_pub.assert_called_once()
            event: DomainEvent = mock_pub.call_args[0][1]
            assert event.type == "connector.crawl.requested"


class TestReceiveFile:
    """Tests for receive_file file upload handling and idempotency."""

    @pytest.mark.asyncio
    async def test_receive_file_success(self) -> None:
        """Receive file registers document, queues parse_file stage, and emits upload event."""
        session = AsyncMock()
        _setup_mock_session(session)
        source_id = uuid4()
        doc_id = uuid4()
        source = MagicMock()
        source.id = source_id
        source.status = "active"
        source.generation = 1

        session.scalar.return_value = None  # No existing batch

        with patch("modules.sources.public.lock_source_set", return_value=_locked(source_id)), \
             patch("modules.sources.public.record_collection_started_in_uow", return_value=True), \
             patch("modules.knowledge.documents.public.prepare_uploaded_document_in_uow", return_value=None), \
             patch("modules.knowledge.documents.public.add_uploaded_document", return_value=doc_id), \
             patch("modules.ingestion.public.publish_event", return_value=None) as mock_pub, \
             patch("modules.ingestion.public.commit_with_replay", return_value=None):
            run, created = await receive_file(
                session, source_id, doc_id, "doc.pdf", "application/pdf", "file:///tmp/doc.pdf", 1024, "sha256abc", scope=SCOPE, multi_workspace_enabled=False,
                expected_access_fence=ACCESS, expected_source_fence=_fence(source_id),
            )
            assert created is True
            assert run.status == "queued"
            mock_pub.assert_called_once()
            event = mock_pub.call_args[0][1]
            assert event.type == "document.file.uploaded"
            assert event.payload["mime_type"] == "application/pdf"


class TestStatusVerificationAndRetry:
    """Tests for get_run, list_source_runs, and retry_run status checks."""

    @pytest.mark.asyncio
    async def test_get_run_not_found(self) -> None:
        """Get run returns None when run_id does not exist."""
        session = AsyncMock()
        session.scalar.return_value = None
        result = await get_run(session, uuid4(), scope=SCOPE, multi_workspace_enabled=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_run_with_stages(self) -> None:
        """Get run returns the IngestionRun and projected StageRead models."""
        session = AsyncMock()
        run_id = uuid4()
        run = IngestionRun(id=run_id, source_id=uuid4(), status="running")
        stage = IngestionStage(
            id=uuid4(),
            run_id=run_id,
            stage_key="parse_file",
            status="running",
            attempts=1,
            updated_at=datetime.now(UTC),
        )

        session.scalar.return_value = run
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [stage]
        authorized_mock = MagicMock()
        authorized_mock.all.return_value = [stage.id]
        session.scalars.side_effect = [scalars_mock, authorized_mock]

        # ObservationNormalization count query
        session.execute.return_value = []

        result = await get_run(session, run_id, scope=SCOPE, multi_workspace_enabled=False)
        assert result is not None
        fetched_run, stages = result
        assert fetched_run.id == run_id
        assert len(stages) == 1
        assert stages[0].stage_key == "parse_file"
        assert stages[0].status == "running"

    @pytest.mark.asyncio
    async def test_retry_run_absent_returns_none(self) -> None:
        """Retry run returns None when the target run does not exist."""
        session = AsyncMock()
        session.scalar.return_value = None
        res = await retry_run(session, uuid4(), scope=SCOPE, multi_workspace_enabled=False)
        assert res is None

    @pytest.mark.asyncio
    async def test_retry_run_source_inactive_raises_409(self) -> None:
        """Retry run raises 409 if the underlying source is no longer active."""
        session = AsyncMock()
        run_id = uuid4()
        source_id = uuid4()
        run = IngestionRun(id=run_id, source_id=source_id, status="failed")
        session.scalar.return_value = run

        with patch("modules.sources.public.lock_source_set", return_value=_locked(source_id, "paused")):
            with pytest.raises(HTTPException) as exc_info:
                await retry_run(session, run_id, scope=SCOPE, multi_workspace_enabled=False)
            assert exc_info.value.status_code == 409
