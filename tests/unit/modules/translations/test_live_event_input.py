"""live_event translation: inline-source schema bounds, owner-only verdicts/reader, size gate."""

import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from core.workspaces.schemas import WorkspaceContext
from modules.translations import live_events, public, service
from modules.translations.schemas import (
    TranslationBatchRequest,
    TranslationInput,
    TranslationItemRequest,
)

WS = uuid4()
OWNER = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=2, workspace_id=WS, role="member", membership_revision=1)
REV = live_events.live_event_revision("Hello", "World")


def _item(rtype="live_event", source=None, revision=REV, rid=None):
    out = {
        "resource_type": rtype,
        "resource_id": str(rid or uuid4()),
        "resource_revision": revision,
    }
    if source is not None:
        out["source"] = source
    return out


SRC = {"title": "Hello", "summary": "World"}


def test_revision_hash_vector():
    assert REV == hashlib.sha256(b"Hello\nWorld").hexdigest()
    assert live_events.live_event_revision("Hello", None) == hashlib.sha256(b"Hello\n").hexdigest()


def test_schema_bounds_and_forbid():
    TranslationItemRequest.model_validate(_item(source=SRC))
    for bad in (
        {"title": "", "summary": None},
        {"title": "x" * 501, "summary": None},
        {"title": "t", "summary": "x" * 1001},
        {"title": "t", "summary": None, "extra": "no"},
    ):
        with pytest.raises(ValidationError):
            TranslationItemRequest.model_validate(_item(source=bad))
    TranslationItemRequest.model_validate(_item(source={"title": "x" * 500, "summary": "y" * 1000}))


def test_source_rejected_on_other_types_and_required_in_batch():
    with pytest.raises(ValidationError):
        TranslationItemRequest.model_validate(_item("news_story", source=SRC))
    # item alone does not require source (worker rebuilds items without it) ...
    TranslationItemRequest.model_validate(_item())
    # ... but a batch does
    with pytest.raises(ValidationError):
        TranslationBatchRequest.model_validate({"items": [_item()]})
    TranslationBatchRequest.model_validate({"items": [_item(source=SRC)]})


def _req(source=SRC, revision=REV):
    return TranslationItemRequest.model_validate(_item(source=source, revision=revision))


def test_verdict_member_mismatch_owner():
    item = _req()
    assert live_events.request_verdict(MEMBER, item) == "resource_unavailable"
    assert live_events.request_verdict(OWNER, _req(revision="other")) == "stale_request"
    got = live_events.request_verdict(OWNER, item)
    assert not isinstance(got, str) and got.resource_revision == REV
    assert got.content_hash == service.content_hash(
        live_events.live_event_input(OWNER, item.resource_id, item.source, REV)
    )
    no_source = TranslationItemRequest.model_validate(_item())
    assert live_events.request_verdict(OWNER, no_source) == "resource_unavailable"


def _session(row):
    return MagicMock(scalar=AsyncMock(return_value=row))


async def _read(session, scope, rid):
    return await live_events.read_live_event_translation_input(
        session, scope=scope, multi_workspace_enabled=False, event_id=rid
    )


async def test_reader_member_none_and_owner_newest_snapshot():
    rid = uuid4()
    member_session = _session({"title": "Hello", "summary": "World"})
    assert await _read(member_session, MEMBER, rid) is None
    member_session.scalar.assert_not_called()
    got = await _read(_session({"title": "Hello", "summary": "World"}), OWNER, rid)
    assert got is not None
    assert got.resource_type == "live_event" and got.resource_revision == REV
    assert got.fields == {"title": "Hello", "excerpt": "World"} and got.local_only is False
    assert got.source_ids == () and got.workspace_id == WS and got.actor_user_id == 1
    assert await _read(_session(None), OWNER, rid) is None


def _input(title, excerpt):
    return TranslationInput(
        WS, 1, "live_event", uuid4(), "r", {"title": title, "excerpt": excerpt}, "v" * 64, (), False
    )


async def _translate(src, gw=None):
    cfg = SimpleNamespace(
        privacy=SimpleNamespace(allow_remote_reasoning=True),
        defaults=SimpleNamespace(resolve=lambda _r: object()),
        system_prompts={},
        endpoint_destination_id=None,
    )
    return await service.translate_input(
        gw or MagicMock(),
        cfg,
        SimpleNamespace(reasoning_allowed=True),
        src,
        "vi",
        AsyncMock(),
        deadline=time.monotonic() + 30,
    )


async def test_translate_input_live_event_too_large_and_already_target():
    with pytest.raises(service.TranslationBlocked) as exc:
        await _translate(_input("t" * (service.MAX_TITLE + 1), "x"))
    assert exc.value.args[0] == "input_too_large" or "input_too_large" in str(exc.value)
    vi = "Công ty cho biết doanh thu tăng trong quý này và triển vọng rất tốt cho nhà đầu tư."
    assert await _translate(_input("Doanh thu tăng", vi)) == {
        "title": "Doanh thu tăng",
        "excerpt": vi,
    }


async def test_authorize_items_live_event_owner_vs_member():
    request = TranslationBatchRequest.model_validate({"items": [_item(source=SRC)]})
    for scope, ok in ((OWNER, True), (MEMBER, False)):
        out = await public._authorize_items(
            MagicMock(), scope, request, multi_workspace_enabled=False
        )
        assert (not isinstance(out[0], str)) is ok


def test_hash_derived_uuid_accepted():
    rid = UUID(bytes=hashlib.sha256(b"live_event:k").digest()[:16])  # arbitrary version nibble
    assert TranslationItemRequest.model_validate(_item(source=SRC, rid=rid)).resource_id == rid
