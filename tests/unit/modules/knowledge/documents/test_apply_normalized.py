"""Characterization tests pinning ``_apply_normalized_document`` (validation-first revisions).

The function is a long multi-phase routine (provenance validation, tombstone/duplicate
short-circuits, provider ranking, revision write, post-write identity proof). These tests pin
its observable contract - results, raised errors and the order of session writes - so the
helper split keeps behaviour identical.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modules.knowledge.documents import public
from modules.knowledge.documents.models import (
    Document,
    DocumentVersion,
    NormalizedDocumentIdentity,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.public import NormalizedDocumentValidationRejected
from tests.unit.modules._documents_patch import patch_documents

WS = uuid4()
SRC = uuid4()
T0 = datetime(2026, 1, 1, tzinfo=UTC)
HASH_LOW = "0" * 64
HASH_HIGH = "f" * 64


def _payload(**over):  # type: ignore[no-untyped-def]
    base = {
        "source_id": SRC, "provider_id": "ext-1", "provider_version": "1", "accepted_record_hash": HASH_HIGH,
        "normalization_version": 1, "observed_at": T0, "received_at": None, "collected_at": None,
        "title": "T", "canonical_url": None, "published_at": None, "content_type": "text/plain",
        "content": "hello", "provenance": {}, "telegram_order": None, "expected_source_generation": 1,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _doc(current_version=0):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        id=uuid4(), workspace_id=WS, source_id=SRC, external_id="ext-1", current_version=current_version,
        content_hash="old", title="old", canonical_url=None, published_at=None, content_type=None,
        observed_at=T0, language=None, extraction_status="ready",
    )


def _ident(document):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        id=uuid4(), workspace_id=WS, source_id=SRC, external_id="ext-1",
        document_id=document.id, tombstoned_at=None,
    )


def _session(scalars):  # type: ignore[no-untyped-def]
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=list(scalars))
    session.flush = AsyncMock()
    session.added = []
    session.add = MagicMock(side_effect=session.added.append)
    return session


def _source(provider="rss"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(provider=provider)


async def _apply(session, payload, source, document, identity, hooks=()):  # type: ignore[no-untyped-def]
    # Hooks are process-global (the search module registers one on import); isolate them.
    with patch_documents("add_content_chunks", AsyncMock(return_value=2)), \
            patch_documents("content_hash_async", AsyncMock(return_value="digest")), \
            patch_documents("_superseded_version_hooks", list(hooks)):
        return await public._apply_normalized_document(session, payload, source, document, identity, WS)


@pytest.mark.asyncio
async def test_tombstoned_identity_short_circuits_without_session_use() -> None:
    document = _doc()
    identity = _ident(document)
    identity.tombstoned_at = T0
    session = _session([])
    result, doc, ident = await _apply(session, _payload(), _source(), document, identity)
    assert result.disposition == "tombstoned"
    assert (doc, ident) == (document, identity)
    session.scalar.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_non_normalized_existing_document_is_rejected() -> None:
    with pytest.raises(NormalizedDocumentValidationRejected, match="non-normalized document"):
        await _apply(_session([]), _payload(), _source(), _doc(), None)


@pytest.mark.asyncio
async def test_telegram_without_provenance_is_rejected_before_any_query() -> None:
    session = _session([])
    with pytest.raises(NormalizedDocumentValidationRejected, match="requires immutable delivery provenance"):
        await _apply(session, _payload(), _source("telegram"), None, None)
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_provider_provenance_is_rejected() -> None:
    with pytest.raises(NormalizedDocumentValidationRejected, match="Incoming provider provenance rejected"):
        await _apply(_session([]), _payload(provenance={"provider_record": {"bad": 1}}), _source(), None, None)


@pytest.mark.asyncio
async def test_duplicate_returns_existing_revision_without_writes() -> None:
    document = _doc(current_version=3)
    identity = _ident(document)
    version = SimpleNamespace(id=uuid4(), version_number=3)
    prior = SimpleNamespace(document_version_id=version.id, provider_id="ext-1")
    session = _session([prior, version, 4])
    result, _, _ = await _apply(session, _payload(), _source(), document, identity)
    assert (result.disposition, result.version_number, result.created_version) == ("duplicate", 3, False)
    assert result.selected_current is True
    assert result.chunk_count == 4
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_with_missing_revision_is_a_consistency_error() -> None:
    document = _doc(current_version=3)
    prior = SimpleNamespace(document_version_id=uuid4(), provider_id="ext-1")
    with pytest.raises(RuntimeError, match="missing revision"):
        await _apply(_session([prior, None]), _payload(), _source(), document, _ident(document))


@pytest.mark.asyncio
async def test_owner_authored_current_revision_conflicts() -> None:
    document = _doc(current_version=2)
    with pytest.raises(NormalizedDocumentValidationRejected, match="owner-authored current revision"):
        await _apply(_session([None, None]), _payload(), _source(), document, _ident(document))


@pytest.mark.asyncio
async def test_new_document_writes_identity_document_version_provenance_in_order() -> None:
    session = _session([0, None])  # max version number, previous current-version id
    result, document, identity = await _apply(session, _payload(), _source(), None, None)
    assert [type(obj) for obj in session.added] == [
        NormalizedDocumentIdentity, Document, DocumentVersion, NormalizedVersionProvenance,
    ]
    assert (result.disposition, result.version_number, result.chunk_count) == ("normalized", 1, 2)
    assert result.created_version is True and result.selected_current is True
    assert document.current_version == 1 and document.content_hash == "digest"
    assert identity.document_id == document.id
    assert session.flush.await_count == 3  # document insert, version insert, post-write flush


@pytest.mark.asyncio
async def test_measurement_provider_never_selects_current_here() -> None:
    session = _session([0])
    result, document, _ = await _apply(session, _payload(), _source("fred"), None, None)
    assert result.selected_current is False
    assert document.current_version == 0


@pytest.mark.asyncio
async def test_generic_lower_rank_is_stored_but_not_selected() -> None:
    document = _doc(current_version=1)
    current = SimpleNamespace(selection_observed_at=T0 + timedelta(days=1), accepted_record_hash=HASH_LOW)
    session = _session([None, current, 1])
    result, document, _ = await _apply(session, _payload(), _source(), document, _ident(document))
    assert (result.version_number, result.selected_current) == (2, False)
    assert document.current_version == 1


@pytest.mark.asyncio
async def test_generic_higher_rank_selects_and_runs_superseded_hooks() -> None:
    document = _doc(current_version=1)
    current = SimpleNamespace(selection_observed_at=T0, accepted_record_hash=HASH_LOW)
    previous_version_id = uuid4()
    session = _session([None, current, 1, previous_version_id])
    hook = AsyncMock()
    result, document, _ = await _apply(session, _payload(), _source(), document, _ident(document), hooks=[hook])
    assert (result.version_number, result.selected_current) == (2, True)
    assert document.current_version == 2 and document.title == "T"
    hook.assert_awaited_once_with(session, previous_version_id)


@pytest.mark.asyncio
async def test_chunk_write_value_error_becomes_runtime_error() -> None:
    session = _session([0, None])
    with patch_documents("add_content_chunks", AsyncMock(side_effect=ValueError("x"))), \
            patch_documents("content_hash_async", AsyncMock(return_value="digest")), \
            pytest.raises(RuntimeError, match="normalized_preparation_chunk_write_conflict"):
        await public._apply_normalized_document(session, _payload(), _source(), None, None, WS)


@pytest.mark.asyncio
async def test_post_write_identity_proof_detects_foreign_workspace() -> None:
    document = _doc(current_version=1)
    document.workspace_id = uuid4()
    current = SimpleNamespace(selection_observed_at=T0, accepted_record_hash=HASH_LOW)
    session = _session([None, current, 1, uuid4()])
    with pytest.raises(RuntimeError, match="normalized_preparation_insert_changed"):
        await _apply(session, _payload(), _source(), document, _ident(document))


def _telegram_metadata(bot="1", epoch=1, update=5):  # type: ignore[no-untyped-def]
    return SimpleNamespace(provider="telegram", telegram=SimpleNamespace(bot_id=bot, epoch=epoch, update_id=update))


class _FakeProviderRecord:
    """Stand-in for ``ProviderRecordMetadata`` driven by the raw dict in provenance."""

    @staticmethod
    def model_validate(raw):  # type: ignore[no-untyped-def]
        if raw is None or raw.get("bad"):
            raise ValueError("bad")
        return SimpleNamespace(
            provider=raw["provider"],
            telegram=None if raw.get("no_telegram") else SimpleNamespace(
                bot_id=raw.get("bot", "1"), epoch=raw.get("epoch", 1), update_id=raw.get("update", 5)),
        )


def _tg_payload(update=9, hash_=HASH_HIGH, bot="1", order=True):  # type: ignore[no-untyped-def]
    return _payload(
        accepted_record_hash=hash_,
        provenance={"provider_record": {"provider": "telegram", "bot": bot, "epoch": 1, "update": update}},
        telegram_order=SimpleNamespace(epoch=1, update_id=update) if order else None,
    )


def _tg_current(update=5, hash_=HASH_LOW, bot="1"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        selection_observed_at=T0, accepted_record_hash=hash_,
        provenance_json={"provider_record": {"provider": "telegram", "bot": bot, "epoch": 1, "update": update}},
    )


async def _apply_tg(payload, current):  # type: ignore[no-untyped-def]
    document = _doc(current_version=1)
    session = _session([None, current, 1, uuid4()])
    with patch_documents("ProviderRecordMetadata", _FakeProviderRecord):
        return await _apply(session, payload, _source("telegram"), document, _ident(document))


@pytest.mark.asyncio
async def test_telegram_newer_delivery_is_selected() -> None:
    result, _, _ = await _apply_tg(_tg_payload(update=9), _tg_current(update=5))
    assert result.selected_current is True


@pytest.mark.asyncio
async def test_telegram_older_delivery_is_stored_unselected() -> None:
    result, _, _ = await _apply_tg(_tg_payload(update=3), _tg_current(update=5))
    assert result.selected_current is False


@pytest.mark.asyncio
async def test_telegram_missing_order_proof_is_rejected() -> None:
    with pytest.raises(NormalizedDocumentValidationRejected, match="ordering proof is missing"):
        await _apply_tg(_tg_payload(order=False), _tg_current())


@pytest.mark.asyncio
async def test_telegram_bot_mismatch_is_rejected() -> None:
    with pytest.raises(NormalizedDocumentValidationRejected, match="incompatible bot binding"):
        await _apply_tg(_tg_payload(), _tg_current(bot="2"))


@pytest.mark.asyncio
async def test_telegram_equal_rank_distinguishes_conflicting_content() -> None:
    with pytest.raises(NormalizedDocumentValidationRejected, match="conflicting immutable content"):
        await _apply_tg(_tg_payload(update=5, hash_=HASH_HIGH), _tg_current(update=5, hash_=HASH_LOW))
    with pytest.raises(NormalizedDocumentValidationRejected, match="different normalization version"):
        await _apply_tg(_tg_payload(update=5, hash_=HASH_HIGH), _tg_current(update=5, hash_=HASH_HIGH))
