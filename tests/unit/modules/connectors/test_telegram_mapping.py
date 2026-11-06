"""Telegram update mapping: delivery-proof binding, channel authorization, media placeholders and bounds."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest

from modules.connectors.providers import telegram as tg
from modules.ingestion.public import TelegramDeliveryProof

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def _proof(update: dict[str, Any], **over: Any) -> TelegramDeliveryProof:
    raw = json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    fields: dict[str, Any] = {
        "bot_id": "123",
        "epoch": 2,
        "update_id": update["update_id"],
        "raw_update_sha256": sha256(raw).hexdigest(),
    }
    fields.update(over)
    return TelegramDeliveryProof(**fields)


def _post(**over: Any) -> dict[str, Any]:
    post: dict[str, Any] = {
        "message_id": 9,
        "date": 1_700_000_000,
        "text": "hello",
        "chat": {"id": -100, "type": "channel", "title": "News", "username": "newschannel"},
    }
    post.update(over)
    return post


def _map(update: dict[str, Any], allowed: frozenset[str] = frozenset({"-100"}), **proof_over: Any):
    return tg.map_telegram_update(
        update, allowed_chat_ids=allowed, proof=_proof(update, **proof_over), collected_at=NOW
    )


def test_channel_post_maps_text_identity_and_permalink() -> None:
    record = _map({"update_id": 5, "channel_post": _post()})
    assert record is not None
    assert record.provider_id == "telegram:-100:9"
    assert record.content == "hello"
    assert record.observed_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert record.metadata["canonical_url"] == "https://t.me/newschannel/9"
    assert record.metadata["title"] == "News"
    provenance = record.metadata["provider_record"]
    assert provenance["timestamp_basis"] == "provider_published"
    assert provenance["telegram"]["channel_id"] == "-100"
    assert record.version.startswith("telegram:2:5:")


def test_edited_post_uses_edit_time_as_modification_basis() -> None:
    update = {"update_id": 6, "edited_channel_post": _post(edit_date=1_700_000_500)}
    record = _map(update)
    assert record is not None
    assert record.observed_at == datetime.fromtimestamp(1_700_000_500, tz=UTC)
    assert record.metadata["provider_record"]["timestamp_basis"] == "provider_modified"
    assert record.metadata["provider_record"]["telegram"]["edited_received"] is True


def test_unauthorized_chat_or_non_channel_is_ignored() -> None:
    update = {"update_id": 5, "channel_post": _post()}
    assert _map(update, allowed=frozenset({"-999"})) is None
    group = {"update_id": 5, "channel_post": _post(chat={"id": -100, "type": "group"})}
    assert _map(group) is None
    assert _map({"update_id": 5, "message": _post()}) is None


def test_private_channel_has_no_permalink_and_uses_id_title() -> None:
    update = {"update_id": 5, "channel_post": _post(chat={"id": -100, "type": "channel"})}
    record = _map(update)
    assert record is not None
    assert "canonical_url" not in record.metadata
    assert record.metadata["title"] == "Telegram channel -100"


def test_media_only_post_gets_placeholder_text_and_descriptor() -> None:
    update = {
        "update_id": 7,
        "channel_post": _post(
            text=None,
            caption="cap",
            photo=[{"file_id": "a"}, {"file_id": "b"}],
            video_note={"file_id": "v"},
        ),
    }
    record = _map(update)
    assert record is not None
    assert "Telegram photo attached; media was not downloaded" in record.content
    media = record.metadata["provider_record"]["telegram"]["media"]
    assert media[0] == {"kind": "photo", "caption": "cap", "count": 2, "file_id": "b"}
    assert media[1]["kind"] == "other"


def test_empty_post_gets_explicit_placeholder() -> None:
    record = _map({"update_id": 8, "channel_post": _post(text=None)})
    assert record is not None and record.content == "[Telegram channel post has no text content]"


def test_long_text_is_truncated_and_flagged() -> None:
    record = _map({"update_id": 9, "channel_post": _post(text="x" * 5000)})
    assert record is not None
    assert len(record.content) == 4000
    assert record.metadata["provider_record"]["content_truncated"] is True


def test_proof_mismatch_is_rejected() -> None:
    update = {"update_id": 5, "channel_post": _post()}
    with pytest.raises(ValueError, match="telegram_delivery_proof_mismatch"):
        _map(update, raw_update_sha256="0" * 64)
    with pytest.raises(ValueError, match="telegram_delivery_proof_mismatch"):
        _map(update, update_id=6)
    with pytest.raises(ValueError, match="telegram_delivery_proof_mismatch"):
        tg._verify_update_proof({"update_id": True}, _proof({"update_id": 1}))


def test_naive_collection_time_is_rejected() -> None:
    update = {"update_id": 5, "channel_post": _post()}
    with pytest.raises(ValueError, match="timezone-aware"):
        tg.map_telegram_update(
            update,
            allowed_chat_ids=frozenset({"-100"}),
            proof=_proof(update),
            collected_at=datetime(2026, 1, 1),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"message_id": True},
        {"message_id": -1},
        {"date": "x"},
        {"date": -1},
        {"edit_date": -5},
        {"date": 10**30},
    ],
)
def test_invalid_message_identity_or_times_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="telegram_message_invalid"):
        _map({"update_id": 5, "channel_post": _post(**overrides)})


def test_bounded_int_text_and_channel_url_helpers() -> None:
    assert tg._bounded_int_text(5) == "5"
    for bad in (True, -1, 10**20, "5", None):
        assert tg._bounded_int_text(bad) is None
    assert tg._channel_post_url("abcde", 3) == "https://t.me/abcde/3"
    assert tg._channel_post_url("abc", 3) is None
    assert tg._channel_post_url(None, 3) is None


@pytest.mark.parametrize("token", ["", "has space", "ctl\x01char", "x" * 10_000, "del\x7f"])
def test_token_validation_rejects_unsafe_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="telegram_token_invalid"):
        tg._validate_token(token)


def test_token_validation_accepts_bot_token_shape() -> None:
    tg._validate_token("123456:ABC-def_ghi")
