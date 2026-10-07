"""Unit tests for ingestion schemas, Telegram deliveries, cursors, and probe classification."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.ingestion.schemas import (
    IngestionRecord,
    Receipt,
    ReceiveBatch,
    RetryRunRequest,
    RunRead,
    StageRead,
    TelegramCursor,
    TelegramDeliveryProof,
    TelegramPageDelivery,
    TelegramRawDelivery,
    classify_telegram_probe,
)


def _make_telegram_update(update_id: int, message_text: str = "hello") -> tuple[dict, str]:
    """Helper to build a canonical update dict and its SHA-256 hex digest."""
    update = {"update_id": update_id, "message": {"text": message_text}}
    encoded = json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return update, sha256(encoded).hexdigest()


class TestIngestionRecord:
    """Test IngestionRecord validation, aware timestamps, and metadata bounds."""

    def test_valid_ingestion_record(self) -> None:
        now = datetime.now(UTC)
        rec = IngestionRecord(
            provider_id="item-123",
            content="Sample document body",
            observed_at=now,
            version="v1.0",
            metadata={"source": "api"},
            collected_at=now,
        )
        assert rec.provider_id == "item-123"
        assert rec.content == "Sample document body"
        assert rec.version == "v1.0"

    def test_observed_at_naive_rejected(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="observed_at must include a timezone"):
            IngestionRecord(
                provider_id="item-123",
                content="text",
                observed_at=naive,
            )

    def test_collected_at_naive_rejected(self) -> None:
        now = datetime.now(UTC)
        naive = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="collected_at must include a timezone"):
            IngestionRecord(
                provider_id="item-123",
                content="text",
                observed_at=now,
                collected_at=naive,
            )

    def test_record_metadata_64kib_limit(self) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValidationError, match="record metadata exceeds 64 KiB"):
            IngestionRecord(
                provider_id="item-123",
                content="text",
                observed_at=now,
                metadata={"large": "x" * 70_000},
            )

    def test_record_extra_forbidden(self) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValidationError):
            IngestionRecord(
                provider_id="item-123",
                content="text",
                observed_at=now,
                extra_key="not_allowed",  # type: ignore[call-arg]
            )


class TestReceiveBatchAndReceipt:
    """Test ReceiveBatch limits, reserved metadata rejection, and Receipt schema."""

    def test_valid_receive_batch(self) -> None:
        now = datetime.now(UTC)
        rec = IngestionRecord(
            provider_id="p1",
            content="content 1",
            observed_at=now,
        )
        batch = ReceiveBatch(
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            batch_key="batch-key-1",
            cursor_before="cur-0",
            cursor_after="cur-1",
            records=[rec],
        )
        assert batch.source_generation == 1
        assert len(batch.records) == 1

    @pytest.mark.parametrize("reserved_key", ["provider_record", "_owner_telegram_proof", "_native_telegram"])
    def test_reserved_metadata_keys_rejected(self, reserved_key: str) -> None:
        now = datetime.now(UTC)
        rec = IngestionRecord(
            provider_id="p1",
            content="content 1",
            observed_at=now,
            metadata={reserved_key: "untrusted_payload"},
        )
        with pytest.raises(
            ValidationError, match="provider_record metadata is reserved for trusted normalization"
        ):
            ReceiveBatch(
                source_id=uuid4(),
                source_generation=1,
                batch_key="batch-key",
                records=[rec],
            )

    def test_cursor_bytes_limit_enforced(self) -> None:
        now = datetime.now(UTC)
        rec = IngestionRecord(provider_id="p1", content="c", observed_at=now)
        # 2000 chars of '€' (3 bytes each = 6000 bytes > 4096 bytes, but 2000 chars <= 4096 max_length)
        oversized_cursor_bytes = "€" * 2000
        with pytest.raises(ValidationError, match="Cursor exceeds 4096 UTF-8 bytes"):
            ReceiveBatch(
                source_id=uuid4(),
                source_generation=1,
                batch_key="k",
                cursor_before=oversized_cursor_bytes,
                records=[rec],
            )

    def test_receipt_statuses(self) -> None:
        b_id, r_id = uuid4(), uuid4()
        for status in ["queued", "running", "succeeded", "needs_ocr", "failed"]:
            receipt = Receipt(batch_id=b_id, run_id=r_id, status=status)  # type: ignore[arg-type]
            assert receipt.status == status

        with pytest.raises(ValidationError):
            Receipt(batch_id=b_id, run_id=r_id, status="unknown")  # type: ignore[arg-type]

    def test_retry_run_request(self) -> None:
        req = RetryRunRequest(stage_key="normalize")
        assert req.stage_key == "normalize"
        with pytest.raises(ValidationError):
            RetryRunRequest(stage_key="")


class TestRunReadAndStageRead:
    """Test serialization of RunRead and StageRead."""

    def test_stage_read_and_run_read(self) -> None:
        now = datetime.now(UTC)
        stage = StageRead(
            stage_key="receive",
            status="succeeded",
            attempts=1,
            error_code=None,
            result_count=10,
            normalized_count=10,
            duplicate_count=0,
            skipped_count=0,
            failed_count=0,
            pending_count=0,
            updated_at=now,
        )
        assert stage.stage_key == "receive"
        assert stage.status == "succeeded"

        run_id = uuid4()
        src_id = uuid4()
        run = RunRead(
            run_id=run_id,
            source_id=src_id,
            status="succeeded",
            stages=[stage],
            error_code=None,
            created_at=now,
            updated_at=now,
        )
        assert run.run_id == run_id
        assert len(run.stages) == 1
        assert run.stages[0].stage_key == "receive"


class TestTelegramDeliveriesAndCursor:
    """Test TelegramDeliveryProof, TelegramRawDelivery, and TelegramCursor."""

    def test_telegram_delivery_proof_validation(self) -> None:
        proof = TelegramDeliveryProof(
            bot_id="1234567890",
            epoch=1,
            update_id=100,
            raw_update_sha256="a" * 64,
        )
        assert proof.bot_id == "1234567890"
        assert proof.epoch == 1

        # Non-numeric bot_id
        with pytest.raises(ValidationError):
            TelegramDeliveryProof(
                bot_id="abc",
                epoch=1,
                update_id=100,
                raw_update_sha256="a" * 64,
            )

        # Non-hex sha256
        with pytest.raises(ValidationError):
            TelegramDeliveryProof(
                bot_id="123",
                epoch=1,
                update_id=100,
                raw_update_sha256="not-a-hex",
            )

    def test_telegram_raw_delivery_digest_matching(self) -> None:
        update, digest = _make_telegram_update(50, "valid message")
        raw = TelegramRawDelivery(
            update_id=50,
            raw_update_sha256=digest,
            update=update,
        )
        assert raw.update_id == 50
        assert raw.raw_update_sha256 == digest

        # Mismatched digest raises
        with pytest.raises(ValidationError, match="Telegram update digest or size is invalid"):
            TelegramRawDelivery(
                update_id=50,
                raw_update_sha256="f" * 64,
                update=update,
            )

    def test_telegram_cursor_ledger_ordering_and_end(self) -> None:
        now = datetime.now(UTC)
        p1 = TelegramPageDelivery(update_id=10, raw_update_sha256="a" * 64)
        p2 = TelegramPageDelivery(update_id=20, raw_update_sha256="b" * 64)

        # Valid cursor: strictly increasing, ends at last_update_id=20
        cursor = TelegramCursor(
            kind="telegram_getupdates_v1",
            bot_id="9999",
            epoch=1,
            last_update_id=20,
            last_nonduplicate_received_at=now,
            last_page_deliveries=[p1, p2],
        )
        assert cursor.last_update_id == 20

        # Unordered ledger raises
        with pytest.raises(ValidationError, match="Telegram cursor ledger must be ordered"):
            TelegramCursor(
                kind="telegram_getupdates_v1",
                bot_id="9999",
                epoch=1,
                last_update_id=20,
                last_nonduplicate_received_at=now,
                last_page_deliveries=[p2, p1],
            )

        # Last entry doesn't match last_update_id raises
        with pytest.raises(ValidationError, match="Telegram cursor ledger must be ordered"):
            TelegramCursor(
                kind="telegram_getupdates_v1",
                bot_id="9999",
                epoch=1,
                last_update_id=30,  # ledger ends at 20
                last_nonduplicate_received_at=now,
                last_page_deliveries=[p1, p2],
            )


class TestClassifyTelegramProbe:
    """Test classify_telegram_probe state machine transitions and conflict detection."""

    def test_verified_bot_id_validation(self) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="Verified Telegram bot identity is invalid"):
            classify_telegram_probe(None, (), received_at=now, verified_bot_id="bot_invalid")

    def test_cursor_bot_id_mismatch_yields_conflict(self) -> None:
        now = datetime.now(UTC)
        p1 = TelegramPageDelivery(update_id=1, raw_update_sha256="a" * 64)
        cursor = TelegramCursor(
            kind="telegram_getupdates_v1",
            bot_id="111",
            epoch=1,
            last_update_id=1,
            last_nonduplicate_received_at=now,
            last_page_deliveries=[p1],
        )
        res = classify_telegram_probe(cursor, (), received_at=now, verified_bot_id="222")
        assert res.conflict_code == "telegram_stream_conflict"

    def test_empty_updates_preserves_cursor(self) -> None:
        now = datetime.now(UTC)
        p1 = TelegramPageDelivery(update_id=5, raw_update_sha256="a" * 64)
        cursor = TelegramCursor(
            kind="telegram_getupdates_v1",
            bot_id="123",
            epoch=2,
            last_update_id=5,
            last_nonduplicate_received_at=now,
            last_page_deliveries=[p1],
        )
        res = classify_telegram_probe(cursor, (), received_at=now, verified_bot_id="123")
        assert res.conflict_code is None
        assert res.epoch == 2
        assert res.delivery_proofs == ()
        assert res.cursor_after == cursor

    def test_initial_page_without_prior_cursor(self) -> None:
        now = datetime.now(UTC)
        u1, d1 = _make_telegram_update(10, "first")
        u2, d2 = _make_telegram_update(11, "second")
        raw1 = TelegramRawDelivery(update_id=10, raw_update_sha256=d1, update=u1)
        raw2 = TelegramRawDelivery(update_id=11, raw_update_sha256=d2, update=u2)

        res = classify_telegram_probe(
            None, (raw1, raw2), received_at=now, verified_bot_id="123"
        )
        assert res.conflict_code is None
        assert res.epoch == 1
        assert res.replay_update_ids == ()
        assert len(res.delivery_proofs) == 2
        assert res.cursor_after is not None
        assert res.cursor_after.epoch == 1
        assert res.cursor_after.last_update_id == 11
        assert len(res.cursor_after.last_page_deliveries) == 2

    def test_exact_duplicates_within_24h_marked_as_replay(self) -> None:
        now = datetime.now(UTC)
        u1, d1 = _make_telegram_update(10, "msg1")
        p1 = TelegramPageDelivery(update_id=10, raw_update_sha256=d1)
        cursor = TelegramCursor(
            kind="telegram_getupdates_v1",
            bot_id="123",
            epoch=1,
            last_update_id=10,
            last_nonduplicate_received_at=now - timedelta(hours=1),
            last_page_deliveries=[p1],
        )

        raw1 = TelegramRawDelivery(update_id=10, raw_update_sha256=d1, update=u1)
        res = classify_telegram_probe(
            cursor, (raw1,), received_at=now, verified_bot_id="123"
        )
        assert res.conflict_code is None
        assert res.epoch == 1
        assert res.replay_update_ids == (10,)
        assert res.cursor_after == cursor

    def test_in_order_new_updates_extend_ledger(self) -> None:
        now = datetime.now(UTC)
        _u1, d1 = _make_telegram_update(10, "msg1")
        p1 = TelegramPageDelivery(update_id=10, raw_update_sha256=d1)
        cursor = TelegramCursor(
            kind="telegram_getupdates_v1",
            bot_id="123",
            epoch=1,
            last_update_id=10,
            last_nonduplicate_received_at=now - timedelta(minutes=5),
            last_page_deliveries=[p1],
        )

        u2, d2 = _make_telegram_update(11, "msg2")
        raw2 = TelegramRawDelivery(update_id=11, raw_update_sha256=d2, update=u2)

        res = classify_telegram_probe(
            cursor, (raw2,), received_at=now, verified_bot_id="123"
        )
        assert res.conflict_code is None
        assert res.epoch == 1
        assert res.replay_update_ids == ()
        assert len(res.delivery_proofs) == 1
        assert res.delivery_proofs[0].update_id == 11
        assert res.cursor_after is not None
        assert res.cursor_after.last_update_id == 11
        assert len(res.cursor_after.last_page_deliveries) == 2

    def test_unordered_updates_yields_conflict(self) -> None:
        now = datetime.now(UTC)
        u1, d1 = _make_telegram_update(12, "higher first")
        u2, d2 = _make_telegram_update(10, "lower second")
        raw1 = TelegramRawDelivery(update_id=12, raw_update_sha256=d1, update=u1)
        raw2 = TelegramRawDelivery(update_id=10, raw_update_sha256=d2, update=u2)

        res = classify_telegram_probe(
            None, (raw1, raw2), received_at=now, verified_bot_id="123"
        )
        assert res.conflict_code == "telegram_stream_conflict"
