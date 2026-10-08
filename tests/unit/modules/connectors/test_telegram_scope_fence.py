"""validate_telegram_scope forwards the owner fence to every Bot API call."""

import pytest

from modules.connectors.providers import telegram


@pytest.mark.asyncio
async def test_before_request_forwarded_to_every_call(monkeypatch):
    seen: list[tuple[str, object]] = []

    async def fake_call(client, token, method, payload=None, *, before_request=None, **_):
        seen.append((method, before_request))
        if method == "getMe":
            return {"id": 7, "is_bot": True}, 1
        if method == "getWebhookInfo":
            return {"url": ""}, 1
        if method == "getChat":
            return {"type": "channel", "id": int(payload["chat_id"])}, 1
        return {"status": "administrator", "user": {"id": 7}}, 1

    async def fence() -> None:
        return None

    monkeypatch.setattr(telegram, "_telegram_call", fake_call)
    result = await telegram.validate_telegram_scope(
        "123456:" + "A" * 35, ("-100123",), before_request=fence,
    )
    assert result.verified_chat_ids == ("-100123",)
    assert [m for m, _ in seen] == ["getMe", "getWebhookInfo", "getChat", "getChatMember"]
    assert all(f is fence for _, f in seen)
