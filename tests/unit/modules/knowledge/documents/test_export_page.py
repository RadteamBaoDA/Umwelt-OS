"""Characterization tests for ``export_page`` (bounded, fenced owner export of documents/revisions)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modules.knowledge.documents import public
from tests.unit.modules._documents_patch import patch_documents
from tests.unit.modules.knowledge.documents._scope import SCOPE_KW

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SRC = uuid4()


def _header_row(index: int, current: int | None = 1) -> dict:  # type: ignore[type-arg]
    created = T0 + timedelta(minutes=index)
    return {
        "document_id": uuid4(),
        "source_id": SRC,
        "document_created_at": created,
        "document_updated_at": created,
        "document_current_version": 1,
        "document_title": f"Doc {index}",
        "document_content_type": "text/plain",
        "document_mime_type": None,
        "document_canonical_url": "https://example.com/a?q=1#f"
        if index != 1
        else "https://user:pw@example.com/a",
        "source_status": "active",
        "source_generation": 2,
        "current_version_number": current,
        "accepted_generation": None,
    }


def _version_row(index: int) -> dict:  # type: ignore[type-arg]
    created = T0 + timedelta(minutes=index)
    return {
        "document_id": uuid4(),
        "source_id": SRC,
        "document_created_at": created,
        "document_updated_at": created,
        "document_current_version": 1,
        "source_status": "active",
        "source_generation": 2,
        "version_id": uuid4(),
        "version_number": 1,
        "version_content": f"body {index}",
        "version_content_hash": "a" * 64,
        "version_observed_at": created,
        "version_created_at": created,
        "provider_id": None,
        "provider_version": None,
        "normalization_version": None,
        "accepted_source_generation": None,
        "provenance_observed_at": None,
        "received_at": None,
        "collected_at": None,
        "selection_observed_at": None,
        "provenance_title": None,
        "provenance_canonical_url": None,
        "provenance_published_at": None,
        "provenance_content_type": None,
        "interaction_read_at": None,
        "interaction_bookmarked_at": None,
        "interaction_dismissed_at": None,
    }


class _Stream:
    def __init__(self, rows: list[dict]) -> None:  # type: ignore[type-arg]
        self._rows = rows
        self.closed = False

    def mappings(self):  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            for row in self._rows:
                yield row

        return gen()

    async def close(self) -> None:
        self.closed = True


def _session(rows: list[dict], *, count: int, stream: bool = False):  # type: ignore[no-untyped-def,type-arg]
    session = MagicMock()
    session.scalar = AsyncMock(return_value=count)
    result = SimpleNamespace(all=lambda: [SimpleNamespace(_mapping=row) for row in rows])
    session.execute = AsyncMock(return_value=result)
    session.opened = _Stream(rows)
    session.stream = AsyncMock(return_value=session.opened)
    return session


async def _page(session, **over):  # type: ignore[no-untyped-def]
    kwargs = {"owner_id": 1, "record_kind": "documents", "limit": 2, **SCOPE_KW, **over}
    return await public.export_page(session, **kwargs)


@pytest.mark.asyncio
async def test_document_headers_page_has_cursor_fences_and_sanitized_url() -> None:
    rows = [_header_row(i) for i in range(3)]
    session = _session(rows, count=3)
    page = await _page(session)
    assert [item.title for item in page.items] == ["Doc 0", "Doc 1"]
    assert len(page.fences) == 2 and page.snapshot_count == 3
    assert page.items[0].canonical_url == "https://example.com/a"
    assert page.items[1].canonical_url is None  # credentials are never exported
    assert page.next_cursor is not None
    assert page.payload_bytes > 2 and page.available is True
    statement = session.execute.await_args.args[0]
    assert "LIMIT" in str(statement.compile()) and statement.compile().params["param_1"] == 3
    second = await _page(_session(rows[2:], count=3), cursor=page.next_cursor)
    assert [item.title for item in second.items] == ["Doc 2"] and second.next_cursor is None


@pytest.mark.asyncio
async def test_exact_limit_without_extra_row_has_no_cursor() -> None:
    page = await _page(_session([_header_row(0), _header_row(1)], count=2))
    assert len(page.items) == 2 and page.next_cursor is None


@pytest.mark.asyncio
async def test_missing_current_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="no current immutable version"):
        await _page(_session([_header_row(0, current=None)], count=1))


@pytest.mark.asyncio
async def test_invalid_kind_or_limit_is_rejected_before_any_query() -> None:
    session = _session([], count=0)
    for over in ({"record_kind": "other"}, {"limit": 0}, {"limit": 101}):
        with pytest.raises(ValueError, match="kind or page limit is invalid"):
            await _page(session, **over)
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_actor_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="actor does not match"):
        await _page(_session([], count=0), owner_id=99)


@pytest.mark.asyncio
async def test_first_record_over_byte_budget_raises_and_later_record_ends_page() -> None:
    rows = [_header_row(0), _header_row(1)]
    with (
        patch_documents("EXPORT_PAGE_MAX_BYTES", 10),
        pytest.raises(ValueError, match="exceeds the page byte budget"),
    ):
        await _page(_session(rows, count=2))
    probe = await _page(_session(rows[:1], count=1))
    one_record = probe.payload_bytes
    with patch_documents("EXPORT_PAGE_MAX_BYTES", one_record + 5):
        page = await _page(_session(rows, count=2))
    assert len(page.items) == 1 and page.next_cursor is not None


@pytest.mark.asyncio
async def test_versions_page_streams_rows_and_closes_stream() -> None:
    rows = [_version_row(i) for i in range(3)]
    session = _session(rows, count=3)
    page = await _page(session, record_kind="versions")
    assert [item.content for item in page.items] == ["body 0", "body 1"]
    assert (
        page.fences[0].version_id == page.items[0].id
        and page.fences[0].version_content_digest == "a" * 64
    )
    assert page.items[0].provenance is None and page.items[0].is_current_version is True
    assert page.next_cursor is not None and session.opened.closed is True
    statement = session.stream.await_args.args[0]
    assert statement.get_execution_options()["yield_per"] == 10


@pytest.mark.asyncio
async def test_versions_first_record_over_budget_raises_and_still_closes_stream() -> None:
    session = _session([_version_row(0)], count=1)
    with (
        patch_documents("EXPORT_PAGE_MAX_BYTES", 10),
        pytest.raises(ValueError, match="version exceeds the page byte budget"),
    ):
        await _page(session, record_kind="versions")
    assert session.opened.closed is True


# --- validate_export_fences ---------------------------------------------------------------


def _fence(**over):  # type: ignore[no-untyped-def]
    from modules.knowledge.documents.schemas import DocumentExportFence

    base = {
        "document_id": uuid4(),
        "document_created_at": T0,
        "document_updated_at": T0,
        "document_current_version": 1,
        "source_id": SRC,
        "source_status": "active",
        "current_source_generation": 2,
    }
    base.update(over)
    return DocumentExportFence(**base)


def _live_row(**over):  # type: ignore[no-untyped-def]
    base = {
        "created_at": T0,
        "updated_at": T0,
        "current_version": 1,
        "source_id": SRC,
        "status": "active",
        "generation": 2,
    }
    base.update(over)
    return SimpleNamespace(**base)


async def _validate(fences, *, rows, count=1, expected=1, kind="documents", eligible=True):  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    session = MagicMock()
    session.scalar = AsyncMock(return_value=count)
    session.execute = AsyncMock(
        side_effect=[SimpleNamespace(one_or_none=lambda row=row: row) for row in rows]
    )
    with patch.object(
        public.sources,
        "filter_export_eligible_sources",
        AsyncMock(return_value=[SRC] if eligible else []),
    ):
        return await public.validate_export_fences(
            session,
            owner_id=1,
            record_kind=kind,
            snapshot_at=T0,
            expected_snapshot_count=expected,
            fences=fences,
            **SCOPE_KW,
        )


@pytest.mark.asyncio
async def test_fences_valid_when_everything_matches() -> None:
    result = await _validate([_fence()], rows=[_live_row()])
    assert (result.valid, result.reason, result.observed_snapshot_count) == (True, "valid", 1)


@pytest.mark.asyncio
async def test_fences_report_count_drift_before_row_checks() -> None:
    result = await _validate([_fence()], rows=[], count=5, expected=1)
    assert (result.valid, result.reason, result.observed_snapshot_count) == (
        False,
        "snapshot_count_changed",
        5,
    )


@pytest.mark.asyncio
async def test_fences_report_source_generation_changes() -> None:
    mixed = [_fence(), _fence(current_source_generation=3)]
    assert (await _validate(mixed, rows=[])).reason == "source_generation_changed"
    assert (
        await _validate([_fence()], rows=[], eligible=False)
    ).reason == "source_generation_changed"
    assert (
        await _validate([_fence()], rows=[_live_row(generation=9)])
    ).reason == "source_generation_changed"


@pytest.mark.asyncio
async def test_fences_report_changed_or_deleted_record() -> None:
    assert (await _validate([_fence()], rows=[None])).reason == "record_changed"
    assert (
        await _validate([_fence()], rows=[_live_row(current_version=4)])
    ).reason == "record_changed"


@pytest.mark.asyncio
async def test_version_fences_check_revision_identity() -> None:
    fence = _fence(
        version_id=uuid4(), version_number=1, version_created_at=T0, version_content_digest="a" * 64
    )
    good = SimpleNamespace(version_number=1, created_at=T0, content_hash="a" * 64)
    ok = await _validate([fence], rows=[_live_row(), good], kind="versions")
    assert ok.valid is True
    bad = SimpleNamespace(version_number=1, created_at=T0, content_hash="b" * 64)
    assert (
        await _validate([fence], rows=[_live_row(), bad], kind="versions")
    ).reason == "record_changed"
    assert (
        await _validate([fence], rows=[_live_row(), None], kind="versions")
    ).reason == "evidence_unavailable"
    with pytest.raises(ValueError, match="missing its immutable revision identity"):
        await _validate([_fence()], rows=[_live_row()], kind="versions")


@pytest.mark.asyncio
async def test_header_fence_with_version_identity_is_rejected() -> None:
    fence = _fence(
        version_id=uuid4(), version_number=1, version_created_at=T0, version_content_digest="a" * 64
    )
    with pytest.raises(ValueError, match="cannot include a version identity"):
        await _validate([fence], rows=[_live_row()])


@pytest.mark.asyncio
async def test_fence_input_validation() -> None:
    with pytest.raises(ValueError, match="revalidation input is invalid"):
        await _validate([], rows=[], kind="other")
    with pytest.raises(ValueError, match="limited to 100 records"):
        await _validate([_fence()] * 101, rows=[])
