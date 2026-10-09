import pytest
from fastapi import HTTPException

from modules.chat import public as chat_public


async def _code(context):
    with pytest.raises(HTTPException) as caught:
        await chat_public.resolve_gadget_context(  # type: ignore[arg-type]
            None, context, scope=None, multi_workspace_enabled=False,
        )
    return caught.value.status_code, caught.value.detail["code"]


@pytest.mark.asyncio
async def test_empty_and_oversized_selection_is_invalid():
    assert await _code({"kind": "selection", "items": []}) == (422, "selection_invalid")
    assert await _code({"kind": "selection", "items": [{}] * 33}) == (422, "selection_invalid")


@pytest.mark.asyncio
async def test_malformed_items_are_invalid():
    assert await _code({"kind": "selection", "items": [{"bogus": 1}]}) == (422, "selection_invalid")


def test_selection_detail_matches_error_envelope():
    assert chat_public._selection_detail("selection_unavailable", "m") == {
        "code": "selection_unavailable", "message": "m", "details": {},
    }
