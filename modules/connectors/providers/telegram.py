"""Bounded Telegram Bot API validation, update transport, and channel-post mapping."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import httpx

from modules.connectors.providers.feed_catalog import send_fence_trace
from modules.connectors.public import (
    ProviderRateLimited,
    TelegramScopeValidation,
    TelegramUpdatePage,
)
from modules.ingestion.schemas import IngestionRecord, TelegramDeliveryProof, TelegramRawDelivery

_METHODS = frozenset({"getMe", "getWebhookInfo", "getChat", "getChatMember", "getUpdates"})
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_TOKEN_BYTES = 512
_CHAT_ID = re.compile(r"^-?[1-9][0-9]{0,19}$")


class _TelegramAPIError(ValueError):
    """Expose a fixed provider failure code without retaining token-bearing URLs or bodies."""

    def __init__(self, code: str) -> None:
        """Keep only a safe operation code for upstream fixed-error translation."""
        super().__init__(code)
        self.code = code


def _validate_token(token: str) -> None:
    """Reject oversized, whitespace, or control-bearing tokens before URL construction."""
    encoded = token.encode("utf-8")
    if not encoded or len(encoded) > _MAX_TOKEN_BYTES or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token
    ):
        raise ValueError("telegram_token_invalid")


async def _telegram_call(
    client: httpx.AsyncClient, token: str, method: str, payload: dict[str, object] | None = None,
    *, max_response_bytes: int = _MAX_RESPONSE_BYTES,
    before_request: Callable[[], Awaitable[None]] | None = None,
) -> tuple[Any, int]:
    """Call one Bot API method under an absolute 30-second budget and return its full body size.

    Native fetch passes ``before_request`` (run immediately before the send, aborting by raising);
    setup-time scope validation has no native fence and omits it.
    """
    if method not in _METHODS:
        raise ValueError("telegram_method_unsupported")
    try:
        async with asyncio.timeout(30):
            if before_request is not None:
                await before_request()
            async with client.stream(
                "POST", f"https://api.telegram.org/bot{quote(token, safe='')}/{method}", json=payload or {},
                extensions={"trace": send_fence_trace(before_request)} if before_request is not None else {},
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise _TelegramAPIError("telegram_response_too_large")
        if response.status_code == 429:
            try:
                payload_json = json.loads(body)
                retry_after = payload_json["parameters"]["retry_after"]
                if isinstance(retry_after, bool) or not isinstance(retry_after, int):
                    raise ValueError  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
                if retry_after <= 0:
                    raise ValueError
            except KeyError:
                retry_after = 60
            except (ValueError, TypeError):
                raise ValueError("telegram_rate_deadline_invalid") from None
            try:
                deadline = datetime.now(UTC) + timedelta(seconds=retry_after)
            except OverflowError:
                raise ValueError("telegram_rate_deadline_invalid") from None
            raise ProviderRateLimited(next_eligible_at=deadline)
        if response.status_code in {401, 403}:
            raise _TelegramAPIError("telegram_credentials_rejected")
        if not 200 <= response.status_code < 300:
            raise _TelegramAPIError("telegram_provider_unavailable")
        data = json.loads(body)
        expected_result = list if method == "getUpdates" else dict
        if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("result"), expected_result):
            raise _TelegramAPIError("telegram_response_invalid")
        return data["result"], len(body)
    except ProviderRateLimited:
        raise
    except _TelegramAPIError:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        raise _TelegramAPIError("telegram_provider_unavailable") from None


async def validate_telegram_scope(
    token: str, chat_ids: tuple[str, ...]
) -> TelegramScopeValidation:
    """Verify bot identity, no webhook, channel types, and administrator rights for all scopes."""
    _validate_token(token)
    if not 1 <= len(chat_ids) <= 100 or len(set(chat_ids)) != len(chat_ids) or any(
        not _CHAT_ID.fullmatch(chat_id) for chat_id in chat_ids
    ):
        raise ValueError("telegram_scope_invalid")
    async with asyncio.timeout(60):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30), trust_env=False, follow_redirects=False, verify=True
        ) as client:
            bot, _ = await _telegram_call(client, token, "getMe")
            bot_id = bot.get("id")
            if (
                isinstance(bot_id, bool) or not isinstance(bot_id, int) or not 0 < bot_id < 10**20
                or bot.get("is_bot") is not True
            ):
                raise _TelegramAPIError("telegram_response_invalid")
            webhook, _ = await _telegram_call(client, token, "getWebhookInfo")
            if webhook.get("url") != "":
                raise _TelegramAPIError("telegram_webhook_configured")
            for chat_id in chat_ids:
                chat, _ = await _telegram_call(client, token, "getChat", {"chat_id": chat_id})
                if chat.get("type") != "channel" or str(chat.get("id")) != chat_id:
                    raise _TelegramAPIError("telegram_scope_not_channel")
                member, _ = await _telegram_call(
                    client, token, "getChatMember", {"chat_id": chat_id, "user_id": bot_id}
                )
                member_user = member.get("user")
                if (
                    member.get("status") not in {"administrator", "creator"}
                    or not isinstance(member_user, dict) or member_user.get("id") != bot_id
                ):
                    raise _TelegramAPIError("telegram_scope_not_admin")
            return TelegramScopeValidation(
                verified_bot_id=str(bot_id),
                verified_chat_ids=chat_ids,
                validated_at=datetime.now(UTC),
            )


async def fetch_telegram_updates(
    token: str, *, offset: int | None, before_request: Callable[[], Awaitable[None]],
    remaining_bytes: int = _MAX_RESPONSE_BYTES,
) -> TelegramUpdatePage:
    """Fetch one update page within its 10 MiB page and caller-supplied trigger byte budgets."""
    _validate_token(token)
    if isinstance(remaining_bytes, bool) or not isinstance(remaining_bytes, int) or remaining_bytes <= 0:
        raise ValueError("telegram_response_too_large")
    response_limit = min(_MAX_RESPONSE_BYTES, remaining_bytes)
    if offset is not None and (isinstance(offset, bool) or not 1 <= offset <= 2**63 - 1):
        raise ValueError("telegram_offset_invalid")
    payload: dict[str, object] = {
        "limit": 100,
        "timeout": 0,
        "allowed_updates": ["channel_post", "edited_channel_post"],
    }
    if offset is not None:
        payload["offset"] = offset
    async with asyncio.timeout(60):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30), trust_env=False, follow_redirects=False, verify=True
        ) as client:
            result, transport_bytes = await _telegram_call(
                client, token, "getUpdates", payload, max_response_bytes=response_limit,
                before_request=before_request,
            )
    if len(result) > 100:
        raise _TelegramAPIError("telegram_response_invalid")
    deliveries: list[TelegramRawDelivery] = []
    previous = -1
    for update in result:
        if not isinstance(update, dict):
            raise _TelegramAPIError("telegram_response_invalid")
        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or not 0 <= update_id <= 2**63 - 1:
            raise _TelegramAPIError("telegram_response_invalid")
        if update_id <= previous:
            raise _TelegramAPIError("telegram_stream_conflict")
        previous = update_id
        raw = json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        deliveries.append(
            TelegramRawDelivery(
                update_id=update_id,
                raw_update_sha256=sha256(raw).hexdigest(),
                update=update,
            )
        )
    return TelegramUpdatePage(
        deliveries=tuple(deliveries), collected_at=datetime.now(UTC), transport_bytes=transport_bytes
    )


def map_telegram_update(
    update: dict[str, Any], *, allowed_chat_ids: frozenset[str],
    proof: TelegramDeliveryProof, collected_at: datetime,
) -> IngestionRecord | None:
    """Map only proven authorized channel posts/edits; media stays descriptive placeholders."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    update_id = update.get("update_id")
    if (
        isinstance(update_id, bool) or not isinstance(update_id, int)
        or update_id != proof.update_id or not 0 <= update_id <= 2**63 - 1
    ):
        raise ValueError("telegram_delivery_proof_mismatch")
    raw = json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if sha256(raw).hexdigest() != proof.raw_update_sha256:
        raise ValueError("telegram_delivery_proof_mismatch")
    edited = "edited_channel_post" in update
    message = update.get("edited_channel_post" if edited else "channel_post")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "channel":
        return None
    chat_id = str(chat.get("id", ""))
    if chat_id not in allowed_chat_ids:
        return None
    message_id = message.get("message_id")
    date = message.get("date")
    edited_date = message.get("edit_date")
    if (
        isinstance(message_id, bool) or not isinstance(message_id, int) or not 0 <= message_id < 10**20
        or isinstance(date, bool) or not isinstance(date, int) or date < 0
        or (edited_date is not None and (isinstance(edited_date, bool) or not isinstance(edited_date, int) or edited_date < 0))
    ):
        raise ValueError("telegram_message_invalid")
    try:
        published_at = datetime.fromtimestamp(date, UTC)
        edited_at = datetime.fromtimestamp(edited_date, UTC) if edited_date is not None else None
    except (OverflowError, OSError, ValueError):
        raise ValueError("telegram_message_invalid") from None
    observed_at = edited_at if edited and edited_at is not None else published_at
    text = message.get("text") or message.get("caption") or ""
    if not isinstance(text, str):
        text = ""
    media: list[dict[str, object]] = []
    media_truncated = False
    media_kinds = ("photo", "video", "audio", "voice", "document", "animation", "sticker", "video_note")
    for kind in media_kinds:
        value = message.get(kind)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        if not values:
            continue
        count = len(values)
        media_truncated = media_truncated or count > 100
        item = values[-1] if values else None
        file_id = item.get("file_id") if isinstance(item, dict) else None
        media_truncated = media_truncated or (isinstance(file_id, str) and len(file_id) > 512)
        media.append({
            "kind": kind if kind != "video_note" else "other",
            "caption": (caption[:4096] if isinstance(caption := message.get("caption"), str) else None),
            "count": min(max(count, 1), 100),
            "file_id": file_id[:512] if isinstance(file_id, str) else None,
        })
        text += f"\n[Telegram {kind} attached; media was not downloaded]"
    if len(media) > 20:
        media = media[:20]
    if not text:
        text = "[Telegram channel post has no text content]"
    thread_id = message.get("message_thread_id")
    reply = message.get("reply_to_message")
    reply_id = reply.get("message_id") if isinstance(reply, dict) else None
    username = chat.get("username") if isinstance(chat.get("username"), str) else None
    channel_label = chat.get("title") if isinstance(chat.get("title"), str) else None
    canonical = (
        f"https://t.me/{username}/{message_id}"
        if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
        else None
    )
    title = channel_label or (f"Telegram channel {chat_id}")
    provider_version = f"telegram:{proof.epoch}:{proof.update_id}:{observed_at.isoformat()}"
    metadata_truncated = (
        len(text) > 4000 or media_truncated or (channel_label is not None and len(channel_label) > 255)
        or (username is not None and len(username) > 64)
        or (isinstance(thread_id, int) and len(str(thread_id)) > 20)
        or (isinstance(reply_id, int) and len(str(reply_id)) > 20)
    )
    metadata: dict[str, Any] = {
        "provider_record": {
            "provider": "telegram",
            "identity": f"telegram:{chat_id}:{message_id}",
            "provider_version": provider_version,
            "timestamp_basis": "provider_modified" if edited and edited_at else "provider_published",
            "coverage": "pending_updates_only",
            "content_truncated": metadata_truncated,
            "provider_modified_at": edited_at if edited else None,
            "license_label": None,
            "telegram": {
                "bot_id": proof.bot_id,
                "channel_id": chat_id,
                "message_id": str(message_id),
                "thread_id": str(thread_id) if isinstance(thread_id, int) and not isinstance(thread_id, bool) and 0 <= thread_id < 10**20 else None,
                "reply_to_message_id": str(reply_id) if isinstance(reply_id, int) and not isinstance(reply_id, bool) and 0 <= reply_id < 10**20 else None,
                "channel_label": channel_label[:255] if channel_label else None,
                "channel_username": username[:64] if username else None,
                "epoch": proof.epoch,
                "update_id": proof.update_id,
                "edited_received": edited,
                "published_at": published_at,
                "edited_at": edited_at,
                "media": media,
            },
        }
    }
    from modules.knowledge.documents.public import ProviderRecordMetadata

    metadata["provider_record"] = ProviderRecordMetadata.model_validate(
        metadata["provider_record"]
    ).model_dump(mode="json", exclude_none=True)
    metadata["title"] = title
    if canonical is not None:
        metadata["canonical_url"] = canonical
    return IngestionRecord(
        provider_id=f"telegram:{chat_id}:{message_id}",
        content=text[:4000],
        observed_at=observed_at,
        collected_at=collected_at,
        version=provider_version,
        metadata=metadata,
    )
