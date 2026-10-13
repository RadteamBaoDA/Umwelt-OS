"""Page reads reuse the prelocked Source fences instead of repeating per-row lock queries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.knowledge.documents import public as documents
from modules.notifications import public
from modules.sources import public as sources

_FP = "a" * 64


def _row(version_id: Any, document_id: Any) -> Any:
    definition, rule = uuid4(), uuid4()
    return SimpleNamespace(
        id=uuid4(), kind="dashboard_highlight", title="t", body="b", link="/x", read_at=None,
        created_at=datetime.now(UTC), document_id=document_id, document_version_id=version_id,
        copied_evidence_revoked=False,
        dedupe_key=f"highlight:{definition}:1:{_FP}:{rule}:{version_id}",
        params={"definition_id": str(definition), "definition_revision": 1, "severity": "info"},
    )


@pytest.mark.asyncio
async def test_page_reads_do_no_per_row_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    doc, src, v_ok, v_gone, v_stale = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    locks = {"n": 0}

    async def version_fences(_s: Any, ids: list[Any], **_k: Any) -> dict[Any, Any]:
        fence = SimpleNamespace(document_id=doc, source_id=src, current_source_generation=2)
        return {v_ok: fence, v_stale: SimpleNamespace(document_id=doc, source_id=src, current_source_generation=1)}

    async def lock(_s: Any, source_id: Any, **_k: Any) -> Any:
        locks["n"] += 1
        return SimpleNamespace(id=source_id, generation=2)

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("per-row query repeated")

    monkeypatch.setattr(documents, "review_version_fences", version_fences)
    monkeypatch.setattr(sources, "lock_retained_evidence_source", lock)
    rows = [_row(v_ok, doc), _row(v_gone, doc), _row(v_stale, None)]
    locked = await public._lock_notification_read_sources(
        object(), rows, scope=object(), multi_workspace_enabled=False, access_fence=object(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(documents, "review_version_fences", boom)
    monkeypatch.setattr(documents, "review_version_locator", boom)
    monkeypatch.setattr(sources, "lock_retained_evidence_source", boom)
    reads = [
        await public._visible_read(
            object(), r, scope=object(), multi_workspace_enabled=False, access_fence=object(),  # type: ignore[arg-type]
            locked=locked,
        ) for r in rows
    ]
    assert locks["n"] == 1  # one lock for the shared Source
    assert [r.title for r in reads] == ["t", None, None]
