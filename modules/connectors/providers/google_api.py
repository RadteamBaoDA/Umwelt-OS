"""Bounded read-only Gmail, Calendar and Drive collectors for Google APIs."""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import quote, urlencode

from modules.connectors.providers import rest
from modules.connectors.quota import QuotaDeferred
from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata

MAX_PAGES = 10
MAX_RECORDS = 500
MAX_REQUESTS_PER_RECEIPT = 25
MAX_GMAIL_DETAILS_PER_RECEIPT = MAX_REQUESTS_PER_RECEIPT - 2
MAX_BATCH_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_CURSOR_BYTES = 16_384
GMAIL_ORIGIN = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_ORIGIN = "https://www.googleapis.com/calendar/v3/calendars"
DRIVE_ORIGIN = "https://www.googleapis.com/drive/v3"
_TEXT_MIMES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})
_GOOGLE_DOC = "application/vnd.google-apps.document"


class GoogleProviderError(ValueError):
    """A stable provider schema or scope error that contains no credential material."""


class _ReceiptPause(Exception):
    """Stop at a resumable receipt boundary before exceeding the durable request budget."""

    def __init__(self, deferred: QuotaDeferred | None = None) -> None:
        super().__init__("collection_receipt_request_bound")
        self.deferred = deferred


@dataclass(frozen=True)
class GoogleCollectionResult:
    """Provider records and an opaque cursor to commit with their durable receipt."""

    records: tuple[IngestionRecord, ...]
    cursor_after: str
    coverage: Literal["returned_snapshot", "truncated"] = "returned_snapshot"


def _provider_record(
    *, provider: str, identity: str, version: str | None,
    timestamp_basis: Literal["provider_modified", "provider_published", "collection"],
    provider_modified_at: datetime | None,
    source_fields: dict[str, object], content_truncated: bool = False,
) -> dict[str, object]:
    """Build the typed provider provenance envelope consumed by durable native ingestion.

    The mapper supplies the timestamp basis separately from the provider version because Gmail's
    history ID is a version cursor, not a modification timestamp. Provider-specific fields are
    checked against the central typed allowlist before becoming generic record metadata.
    """
    envelope = ProviderRecordMetadata(
        provider=provider,
        identity=identity,
        provider_version=version,
        timestamp_basis=timestamp_basis,
        coverage="returned_snapshot",
        content_truncated=content_truncated,
        provider_modified_at=provider_modified_at,
        source_fields=source_fields,
    )
    return envelope.model_dump(mode="json", exclude_none=True)


@dataclass
class _RequestBudget:
    """Count every bounded Google API response in one atomic collection attempt."""

    access_token: str
    before_request: rest.BeforeSend
    transport_bytes: int = 0
    requests: int = 0

    async def get(self, url: str, *, accept: str = "application/json") -> bytes:
        """Fetch one Google page within the request cap using the shared credential/send fence."""
        if self.requests >= MAX_REQUESTS_PER_RECEIPT:
            raise _ReceiptPause
        try:
            fetched = await rest.fetch_bounded(
                url,
                headers={"Authorization": f"Bearer {self.access_token}", "Accept": accept},
                before_send=self.before_request,
                max_bytes=MAX_RESPONSE_BYTES,
            )
        except QuotaDeferred as exc:
            raise _ReceiptPause(exc) from exc
        self.requests += 1
        body = fetched.body or b""
        self.transport_bytes += len(body)
        if self.transport_bytes > MAX_TOTAL_BYTES:
            raise rest.CollectionIncomplete
        return body


def _json(body: bytes) -> Any:
    """Decode JSON while preserving decimal numeric literals for provider metadata."""
    try:
        return json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GoogleProviderError("response_not_json") from exc


def _next_page(page: dict[str, Any]) -> str | None:
    """Return a valid provider page token, rejecting malformed tokens before cursor advancement."""
    if "nextPageToken" not in page:
        return None
    value = page["nextPageToken"]
    if not isinstance(value, str) or not value:
        raise GoogleProviderError("page_token_invalid")
    return value


def _text(value: object, limit: int = 1_000_000) -> str:
    """Return bounded UTF-8 provider text without accepting non-string schema values."""
    if not isinstance(value, str):
        raise GoogleProviderError("text_field_invalid")
    return value[:limit]


def _utc(value: object, *, fallback: datetime | None = None) -> datetime:
    """Parse an aware RFC3339 timestamp or use an explicitly supplied collection-time fallback."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise GoogleProviderError("timestamp_invalid") from exc
        if parsed.tzinfo is None:
            raise GoogleProviderError("timestamp_invalid")
        return parsed.astimezone(UTC)
    if fallback is not None:
        return fallback.astimezone(UTC)
    raise GoogleProviderError("timestamp_missing")


def _cursor_json(value: dict[str, Any]) -> str:
    """Serialize a provider continuation cursor under the public byte bound."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise rest.CollectionIncomplete
    return encoded


def _read_cursor(raw: str | None, provider: str) -> dict[str, Any] | None:
    """Read this provider's opaque v1 or v2 cursor without accepting another provider's state.

    V1 remains valid for pre-upgrade Gmail, Calendar, and Drive cursors; v2 adds resumable snapshot
    phases and scope fingerprints. Callers validate their phase-specific fields after decoding.
    """
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise GoogleProviderError("cursor_invalid")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise GoogleProviderError("cursor_invalid") from exc
    version = value.get("v") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or isinstance(version, bool) or version not in {1, 2}
            or value.get("provider") != provider):
        raise GoogleProviderError("cursor_invalid")
    return value


def _append_record(records: list[IngestionRecord], record: IngestionRecord, current_bytes: int) -> int:
    """Append within record and ingestion-batch caps or fail before a cursor can advance."""
    record_bytes = len(record.model_dump_json().encode("utf-8"))
    if len(records) >= MAX_RECORDS or current_bytes + record_bytes > MAX_BATCH_BYTES:
        raise rest.CollectionIncomplete
    records.append(record)
    return current_bytes + record_bytes


def _gmail_message_record(message: object, collected_at: datetime) -> IngestionRecord:
    """Map a Gmail message without attachments and attach validated identity/version provenance.

    The Gmail history ID is retained as a version, while ``internalDate`` remains the published
    timestamp basis. Provider fields are allowlisted before the record can reach durable ingestion.
    """
    if not isinstance(message, dict):
        raise GoogleProviderError("gmail_message_invalid")
    identifier = message.get("id")
    thread_id = message.get("threadId")
    if (not isinstance(identifier, str) or not identifier or len(identifier) > 512
            or not isinstance(thread_id, str) or not thread_id or len(thread_id) > 512):
        raise GoogleProviderError("gmail_message_identity_invalid")
    raw_payload = message.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    raw_headers = payload.get("headers")
    headers: list[Any] = raw_headers if isinstance(raw_headers, list) else []
    fields = {
        item.get("name", "").lower(): item.get("value", "")
        for item in headers if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    text_parts: list[str] = []
    pending = [payload]
    visited_parts = 0

    while pending:
        part = pending.pop()
        visited_parts += 1
        if visited_parts > 1000:
            raise rest.CollectionIncomplete
        if not isinstance(part, dict):
            continue
        raw_body = part.get("body")
        body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
        data = body.get("data")
        if (part.get("mimeType") == "text/plain" and isinstance(data, str)
                and not body.get("attachmentId") and not part.get("filename")):
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GoogleProviderError("gmail_body_invalid") from exc
            text_parts.append(decoded)
        children = part.get("parts", [])
        if isinstance(children, list):
            pending.extend(reversed(children))
    body_text = "\n".join(text_parts)
    if len(body_text) > 1_000_000:
        raise rest.CollectionIncomplete
    raw_snippet = message.get("snippet")
    snippet: str = raw_snippet if isinstance(raw_snippet, str) else ""
    content: str = body_text or snippet
    raw_internal_date = message.get("internalDate")
    if (isinstance(raw_internal_date, bool) or not isinstance(raw_internal_date, (str, int))
            or not str(raw_internal_date).isdigit()):
        raise GoogleProviderError("gmail_timestamp_invalid")
    try:
        observed_at = datetime.fromtimestamp(int(raw_internal_date) / 1000, tz=UTC)
    except (ValueError, OSError, OverflowError) as exc:
        raise GoogleProviderError("gmail_timestamp_invalid") from exc
    history_id = message.get("historyId")
    if not isinstance(history_id, str) or not history_id.isdigit():
        raise GoogleProviderError("gmail_history_id_invalid")
    subject = _text(fields.get("subject", ""), 1000)
    final_content = subject + ("\n\n" if subject and content else "") + content
    if len(final_content) > 1_000_000:
        raise rest.CollectionIncomplete
    return IngestionRecord(
        provider_id=identifier,
        content=final_content,
        observed_at=observed_at,
        collected_at=collected_at.astimezone(UTC),
        version=history_id,
        metadata={
            "thread_id": thread_id,
            "history_id": history_id,
            "label_ids": [value for value in message.get("labelIds", []) if isinstance(value, str)][:100],
            "subject": subject,
            "from": _text(fields.get("from", ""), 1000),
            "to": _text(fields.get("to", ""), 2000),
            "date_header": _text(fields.get("date", ""), 255),
            "provider_record": _provider_record(
                provider="google_mail", identity=identifier, version=history_id,
                timestamp_basis="provider_published", provider_modified_at=None,
                source_fields={
                    "thread_id": thread_id,
                    "history_id": history_id,
                    "subject": subject,
                    "from": _text(fields.get("from", ""), 1000),
                    "to": _text(fields.get("to", ""), 2000),
                    "date_header": _text(fields.get("date", ""), 255),
                },
            ),
        },
    )


async def collect_gmail(
    access_token: str,
    label_ids: tuple[str, ...],
    *,
    before_request: rest.BeforeSend,
    cursor: str | None = None,
    collected_at: datetime | None = None,
) -> GoogleCollectionResult:
    """Collect Gmail in receipt-sized chunks, preserving snapshot and history continuation.

    The profile history ID and fixed 30-day query bound are saved before listing. Cursors retain
    the current page token and completed-message offset, so quota deferral or the per-receipt cap
    never causes already accepted messages to be fetched again. The caller must commit each
    returned cursor only with the matching durable ingestion receipt.
    """
    if (not label_ids or any(not isinstance(label, str) or not label or len(label) > 256 or "/" in label
                             for label in label_ids) or len(set(label_ids)) != len(label_ids)):
        raise GoogleProviderError("gmail_label_scope_invalid")
    state = _read_cursor(cursor, "gmail")
    collected = collected_at or datetime.now(UTC)
    if collected.tzinfo is None:
        raise GoogleProviderError("collection_time_invalid")
    budget = _RequestBudget(access_token, before_request)
    records: list[IngestionRecord] = []
    batch_bytes = 0
    selected_labels = sorted(label_ids)
    original_cursor = cursor
    if state is None:
        try:
            profile = _json(await budget.get(f"{GMAIL_ORIGIN}/profile"))
        except _ReceiptPause as pause:
            if pause.deferred is not None:
                raise pause.deferred
            raise GoogleProviderError("gmail_cursor_no_progress") from pause
        history_id = profile.get("historyId") if isinstance(profile, dict) else None
        if not isinstance(history_id, str) or not history_id.isdigit():
            raise GoogleProviderError("gmail_profile_invalid")
        state = {
            "v": 2, "provider": "gmail", "labels": selected_labels, "phase": "snapshot",
            "history_id": history_id,
            "snapshot_after": int((collected - timedelta(days=30)).timestamp()),
            "page_token": None, "offset": 0,
        }
    elif "phase" not in state:
        # Existing v1 cursors are already past the snapshot and continue from their history ID.
        state = {
            "v": 2, "provider": "gmail", "labels": selected_labels, "phase": "history",
            "history_id": state.get("history_id"), "history_page_token": None, "history_offset": 0,
        }
    elif state.get("labels") != selected_labels:
        raise GoogleProviderError("gmail_cursor_scope_changed")

    history_id = state.get("history_id")
    if not isinstance(history_id, str) or not history_id.isdigit():
        raise GoogleProviderError("gmail_cursor_invalid")
    phase = state.get("phase")
    if phase == "snapshot":
        snapshot_after = state.get("snapshot_after")
        page_token = state.get("page_token")
        offset = state.get("offset", 0)
        if (isinstance(snapshot_after, bool) or not isinstance(snapshot_after, int)
                or page_token is not None and (not isinstance(page_token, str) or not page_token)
                or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise GoogleProviderError("gmail_cursor_invalid")
        params: dict[str, Any] = {
            "labelIds": selected_labels, "q": f"after:{snapshot_after}", "maxResults": MAX_GMAIL_DETAILS_PER_RECEIPT,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            page = _json(await budget.get(f"{GMAIL_ORIGIN}/messages?{urlencode(params, doseq=True)}"))
            if not isinstance(page, dict) or not isinstance(page.get("messages", []), list):
                raise GoogleProviderError("gmail_messages_invalid")
            ids = [item["id"] for item in page.get("messages", []) if isinstance(item, dict)
                   and isinstance(item.get("id"), str)]
            if offset > len(ids):
                raise GoogleProviderError("gmail_cursor_offset_invalid")
            for index in range(offset, len(ids)):
                identifier = ids[index]
                message = _json(await budget.get(
                    f"{GMAIL_ORIGIN}/messages/{quote(identifier, safe='')}?format=full"))
                if not isinstance(message, dict):
                    raise GoogleProviderError("gmail_message_invalid")
                labels = message.get("labelIds", [])
                if not isinstance(labels, list):
                    raise GoogleProviderError("gmail_labels_invalid")
                if set(selected_labels).intersection(value for value in labels if isinstance(value, str)):
                    batch_bytes = _append_record(records, _gmail_message_record(message, collected), batch_bytes)
                state["offset"] = index + 1
            next_token = _next_page(page)
            if next_token:
                state.update({"page_token": next_token, "offset": 0})
            else:
                state = {
                    "v": 2, "provider": "gmail", "labels": selected_labels, "phase": "history",
                    "history_id": history_id, "history_page_token": None, "history_offset": 0,
                }
        except _ReceiptPause as pause:
            result_cursor = _cursor_json(state)
            if result_cursor == original_cursor and not records:
                if pause.deferred is not None:
                    raise pause.deferred
                raise GoogleProviderError("gmail_cursor_no_progress") from pause
            return GoogleCollectionResult(tuple(records), result_cursor, "truncated")
        result_cursor = _cursor_json(state)
        return GoogleCollectionResult(tuple(records), result_cursor,
                                      "truncated" if state.get("phase") == "snapshot" else "returned_snapshot")

    if phase != "history":
        raise GoogleProviderError("gmail_cursor_invalid")
    page_token = state.get("history_page_token")
    offset = state.get("history_offset", 0)
    if (page_token is not None and (not isinstance(page_token, str) or not page_token)
            or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise GoogleProviderError("gmail_cursor_invalid")
    params = {"startHistoryId": history_id, "historyTypes": ["messageAdded", "labelAdded"], "maxResults": 100}
    if page_token:
        params["pageToken"] = page_token
    try:
        page = _json(await budget.get(f"{GMAIL_ORIGIN}/history?{urlencode(params, doseq=True)}"))
        if not isinstance(page, dict):
            raise GoogleProviderError("gmail_page_invalid")
        message_ids: list[str] = []
        seen: set[str] = set()
        history_rows = page.get("history", [])
        if not isinstance(history_rows, list):
            raise GoogleProviderError("gmail_history_invalid")
        for history_row in history_rows:
            if not isinstance(history_row, dict):
                continue
            for key in ("messagesAdded", "labelsAdded"):
                items = history_row.get(key, [])
                for item in items if isinstance(items, list) else []:
                    message = item.get("message") if isinstance(item, dict) else None
                    identifier = message.get("id") if isinstance(message, dict) else None
                    if isinstance(identifier, str) and identifier not in seen:
                        seen.add(identifier)
                        message_ids.append(identifier)
        if offset > len(message_ids):
            raise GoogleProviderError("gmail_cursor_offset_invalid")
        for index in range(offset, min(len(message_ids), offset + MAX_GMAIL_DETAILS_PER_RECEIPT)):
            identifier = message_ids[index]
            message = _json(await budget.get(
                f"{GMAIL_ORIGIN}/messages/{quote(identifier, safe='')}?format=full"))
            if not isinstance(message, dict):
                raise GoogleProviderError("gmail_message_invalid")
            labels = message.get("labelIds", [])
            if not isinstance(labels, list):
                raise GoogleProviderError("gmail_labels_invalid")
            if set(selected_labels).intersection(value for value in labels if isinstance(value, str)):
                batch_bytes = _append_record(records, _gmail_message_record(message, collected), batch_bytes)
            state["history_offset"] = index + 1
        next_token = _next_page(page)
        if state.get("history_offset", 0) < len(message_ids):
            # Replay this bounded history page and skip completed IDs by offset on the next receipt.
            state["history_page_token"] = page_token
        elif next_token:
            state.update({"history_page_token": next_token, "history_offset": 0})
        else:
            final_history = page.get("historyId")
            if isinstance(final_history, str) and final_history.isdigit():
                state["history_id"] = final_history
            state.update({"history_page_token": None, "history_offset": 0})
    except _ReceiptPause as pause:
        result_cursor = _cursor_json(state)
        if result_cursor == original_cursor and not records:
            if pause.deferred is not None:
                raise pause.deferred
            raise GoogleProviderError("gmail_cursor_no_progress") from pause
        return GoogleCollectionResult(tuple(records), result_cursor, "truncated")
    result_cursor = _cursor_json(state)
    complete_page = state.get("history_offset", 0) == 0 and state.get("history_page_token") is None
    return GoogleCollectionResult(tuple(records), result_cursor,
                                  "returned_snapshot" if complete_page else "truncated")


def _calendar_event_record(calendar_id: str, event: object, collected_at: datetime) -> IngestionRecord:
    """Map an event with cancellation/recurrence metadata and typed provider identity/version.

    The event start remains the observed event time; the provider ``updated`` timestamp, when
    present, supplies the modification basis and version used by ingestion provenance checks.
    """
    if not isinstance(event, dict):
        raise GoogleProviderError("calendar_event_invalid")
    event_id = event.get("id")
    status = event.get("status", "confirmed")
    provider_id = f"{calendar_id}/{event_id}" if isinstance(event_id, str) else ""
    if (not isinstance(event_id, str) or not event_id or len(provider_id) > 512
            or not isinstance(status, str) or status not in {"confirmed", "tentative", "cancelled"}):
        raise GoogleProviderError("calendar_event_identity_invalid")
    raw_start = event.get("start")
    raw_end = event.get("end")
    start: dict[str, Any] = raw_start if isinstance(raw_start, dict) else {}
    end: dict[str, Any] = raw_end if isinstance(raw_end, dict) else {}
    raw_start_value = start.get("dateTime") or start.get("date")
    raw_updated = event.get("updated") or event.get("created")
    if isinstance(raw_start_value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_start_value):
        try:
            observed_at = datetime.combine(date.fromisoformat(raw_start_value), datetime.min.time(), tzinfo=UTC)
        except ValueError as exc:
            raise GoogleProviderError("calendar_event_time_invalid") from exc
    else:
        observed_at = _utc(raw_start_value or raw_updated, fallback=collected_at if status == "cancelled" else None)
    title = _text(event.get("summary", ""), 1000)
    description = _text(event.get("description", ""), 10000)
    content = title + ("\n\n" if title and description else "") + description
    recurrence = event.get("recurrence", [])
    if not isinstance(recurrence, list) or any(not isinstance(item, str) for item in recurrence):
        raise GoogleProviderError("calendar_recurrence_invalid")
    updated = event.get("updated")
    return IngestionRecord(
        provider_id=provider_id,
        content=content,
        observed_at=observed_at,
        collected_at=collected_at.astimezone(UTC),
        version=_text(updated, 255) if isinstance(updated, str) else None,
        metadata={
            "calendar_id": calendar_id,
            "event_id": event_id,
            "status": status,
            "start": _text(raw_start_value, 512) if isinstance(raw_start_value, str) else None,
            "end": _text(end.get("dateTime") or end.get("date"), 512)
            if isinstance(end.get("dateTime") or end.get("date"), str) else None,
            "start_timezone": _text(start["timeZone"], 128) if isinstance(start.get("timeZone"), str) else None,
            "end_timezone": _text(end["timeZone"], 128) if isinstance(end.get("timeZone"), str) else None,
            "recurrence": [_text(item, 1000) for item in recurrence[:32]],
            "recurring_event_id": event.get("recurringEventId"),
            "original_start_time": event.get("originalStartTime"),
            "html_link": _text(event["htmlLink"], 2048) if isinstance(event.get("htmlLink"), str) else None,
            "provider_record": _provider_record(
                provider="google_calendar", identity=provider_id,
                version=updated if isinstance(updated, str) else None,
                timestamp_basis="provider_modified" if isinstance(updated, str) else "collection",
                provider_modified_at=_utc(updated) if isinstance(updated, str) else None,
                content_truncated=(len(event.get("summary", "")) > 1000
                                   if isinstance(event.get("summary", ""), str) else False)
                or (len(event.get("description", "")) > 10000
                    if isinstance(event.get("description", ""), str) else False),
                source_fields={
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                    "status": status,
                    "recurrence": [_text(item, 255) for item in recurrence[:32]],
                    **({"start": _text(raw_start_value, 64)} if isinstance(raw_start_value, str) else {}),
                    **({"end": _text(end.get("dateTime") or end.get("date"), 64)}
                       if isinstance(end.get("dateTime") or end.get("date"), str) else {}),
                    **({"start_timezone": _text(start["timeZone"], 128)}
                       if isinstance(start.get("timeZone"), str) else {}),
                    **({"end_timezone": _text(end["timeZone"], 128)}
                       if isinstance(end.get("timeZone"), str) else {}),
                    **({"html_link": _text(event["htmlLink"], 2048)}
                       if isinstance(event.get("htmlLink"), str) else {}),
                },
            ),
        },
    )


async def collect_calendar(
    access_token: str,
    calendar_ids: tuple[str, ...],
    *,
    before_request: rest.BeforeSend,
    cursor: str | None = None,
    collected_at: datetime | None = None,
) -> GoogleCollectionResult:
    """Collect calendar pages in receipt-sized chunks, preserving each calendar's sync token.

    Initial bounds are fixed in the cursor and page through the selected calendars in sorted
    order. Each API page is limited to 20 events, so 25 calls cannot exceed the 500-record receipt
    cap. Continuation state advances only with the caller's durable receipt.
    """
    if (not calendar_ids or any(not isinstance(value, str) or not value or len(value) > 512 for value in calendar_ids)
            or len(set(calendar_ids)) != len(calendar_ids)):
        raise GoogleProviderError("calendar_scope_invalid")
    selected_ids = sorted(calendar_ids)
    scope_hash = hashlib.sha256("\0".join(selected_ids).encode("utf-8")).hexdigest()
    collected = collected_at or datetime.now(UTC)
    if collected.tzinfo is None:
        raise GoogleProviderError("collection_time_invalid")
    budget = _RequestBudget(access_token, before_request)
    state = _read_cursor(cursor, "calendar")
    if state is None:
        state = {
            "v": 2, "provider": "calendar", "scope_hash": scope_hash,
            "time_min": (collected - timedelta(days=30)).isoformat(),
            "time_max": (collected + timedelta(days=90)).isoformat(),
            "tokens": [None] * len(selected_ids), "calendar_index": 0, "page_token": None,
        }
    elif "scope_hash" not in state:
        # V1 stored tokens by ID and continues compatible tokens; newly selected calendars bootstrap.
        old_tokens = state.get("calendars", {})
        if not isinstance(old_tokens, dict) or set(old_tokens) - set(selected_ids):
            raise GoogleProviderError("calendar_cursor_invalid")
        if any(not isinstance(token, str) or not token for token in old_tokens.values()):
            raise GoogleProviderError("calendar_cursor_invalid")
        state = {
            "v": 2, "provider": "calendar", "scope_hash": scope_hash,
            "time_min": (collected - timedelta(days=30)).isoformat(),
            "time_max": (collected + timedelta(days=90)).isoformat(),
            "tokens": [old_tokens.get(calendar_id) for calendar_id in selected_ids],
            "calendar_index": 0, "page_token": None,
        }
    if state.get("scope_hash") != scope_hash:
        raise GoogleProviderError("calendar_cursor_scope_changed")
    tokens = state.get("tokens")
    calendar_index = state.get("calendar_index", 0)
    page_token = state.get("page_token")
    time_min = state.get("time_min")
    time_max = state.get("time_max")
    if (not isinstance(tokens, list) or len(tokens) != len(selected_ids)
            or any(token is not None and (not isinstance(token, str) or not token) for token in tokens)
            or isinstance(calendar_index, bool) or not isinstance(calendar_index, int)
            or not 0 <= calendar_index < len(selected_ids)
            or page_token is not None and (not isinstance(page_token, str) or not page_token)
            or not isinstance(time_min, str) or not isinstance(time_max, str)):
        raise GoogleProviderError("calendar_cursor_invalid")
    original_cursor = cursor
    records: list[IngestionRecord] = []
    batch_bytes = 0
    try:
        while calendar_index < len(selected_ids):
            calendar_id = selected_ids[calendar_index]
            base = f"{CALENDAR_ORIGIN}/{quote(calendar_id, safe='')}/events"
            token = tokens[calendar_index]
            params: dict[str, Any] = {"maxResults": 20, "showDeleted": "true"}
            if token:
                params["syncToken"] = token
            else:
                params.update({"timeMin": time_min, "timeMax": time_max, "singleEvents": "false"})
            if page_token:
                params["pageToken"] = page_token
            page = _json(await budget.get(f"{base}?{urlencode(params)}"))
            if not isinstance(page, dict) or not isinstance(page.get("items", []), list):
                raise GoogleProviderError("calendar_page_invalid")
            items = page.get("items", [])
            if len(items) > 20:
                raise GoogleProviderError("calendar_page_too_large")
            for event in items:
                batch_bytes = _append_record(
                    records, _calendar_event_record(calendar_id, event, collected), batch_bytes)
            next_page = _next_page(page)
            if next_page:
                page_token = next_page
                state["page_token"] = page_token
            else:
                next_sync = page.get("nextSyncToken")
                if not isinstance(next_sync, str) or not next_sync:
                    raise GoogleProviderError("calendar_sync_token_missing")
                tokens[calendar_index] = next_sync
                calendar_index += 1
                page_token = None
                if calendar_index == len(selected_ids):
                    calendar_index = 0
                state.update({"tokens": tokens, "calendar_index": calendar_index, "page_token": None})
                # A completed sweep is one receipt boundary; do not query the newly refreshed sync tokens again.
                if calendar_index == 0 and all(isinstance(value, str) for value in tokens):
                    break
            if budget.requests >= MAX_REQUESTS_PER_RECEIPT:
                break
    except _ReceiptPause as pause:
        result_cursor = _cursor_json(state)
        if result_cursor == original_cursor and not records:
            if pause.deferred is not None:
                raise pause.deferred
            raise GoogleProviderError("calendar_cursor_no_progress") from pause
        return GoogleCollectionResult(tuple(records), result_cursor, "truncated")
    complete = calendar_index == 0 and page_token is None and all(isinstance(token, str) for token in tokens)
    return GoogleCollectionResult(tuple(records), _cursor_json(state),
                                  "returned_snapshot" if complete else "truncated")


def _drive_file_record(file: object, content: bytes, mime_type: str, collected_at: datetime) -> IngestionRecord:
    """Map an allowed Drive text file with identity, modification version and typed provenance.

    Only parser-supported text is passed here; the source envelope retains bounded Drive metadata
    and labels the timestamp as provider-modified only when Drive supplied ``modifiedTime``.
    """
    if not isinstance(file, dict) or not isinstance(file.get("id"), str) or not file["id"]:
        raise GoogleProviderError("drive_file_invalid")
    if len(file["id"]) > 512:
        raise GoogleProviderError("drive_file_identity_invalid")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GoogleProviderError("drive_text_not_utf8") from exc
    if len(text) > 1_000_000:
        raise rest.CollectionIncomplete
    if mime_type == "application/json":
        try:
            json.loads(text)
        except ValueError as exc:
            raise GoogleProviderError("drive_json_invalid") from exc
    modified = file.get("modifiedTime")
    observed_at = _utc(modified, fallback=collected_at)
    name = _text(file.get("name", ""), 500)
    modified_version = modified if isinstance(modified, str) else str(file.get("version", "snapshot"))
    return IngestionRecord(
        provider_id=file["id"],
        content=text,
        observed_at=observed_at,
        collected_at=collected_at.astimezone(UTC),
        version=modified_version[:255],
        metadata={
            "name": name,
            "mime_type": mime_type,
            "modified_time": modified,
            "web_view_link": _text(file["webViewLink"], 2048) if isinstance(file.get("webViewLink"), str) else None,
            "md5_checksum": _text(file["md5Checksum"], 64) if isinstance(file.get("md5Checksum"), str) else None,
            "provider_record": _provider_record(
                provider="google_drive", identity=file["id"],
                version=modified_version[:255] if isinstance(modified, str) else None,
                timestamp_basis="provider_modified" if isinstance(modified, str) else "collection",
                provider_modified_at=observed_at if isinstance(modified, str) else None,
                source_fields={
                    "name": name,
                    "mime_type": mime_type,
                    **({"modified_time": _text(modified, 64)} if isinstance(modified, str) else {}),
                    **({"web_view_link": _text(file["webViewLink"], 2048)}
                       if isinstance(file.get("webViewLink"), str) else {}),
                    **({"md5_checksum": _text(file["md5Checksum"], 64)}
                       if isinstance(file.get("md5Checksum"), str) else {}),
                },
            ),
        },
    )


async def _drive_file_content(
    budget: _RequestBudget, file: dict[str, Any], collected_at: datetime,
) -> IngestionRecord | None:
    """Fetch one selected Drive file as exported Google Doc text or an existing parser text MIME."""
    identifier = file.get("id")
    mime_type = file.get("mimeType")
    if not isinstance(identifier, str) or not identifier or not isinstance(mime_type, str):
        raise GoogleProviderError("drive_file_invalid")
    quoted_id = quote(identifier, safe="")
    if mime_type == _GOOGLE_DOC:
        body = await budget.get(f"{DRIVE_ORIGIN}/files/{quoted_id}/export?mimeType=text%2Fplain", accept="text/plain")
        return _drive_file_record(file, body, "text/plain", collected_at)
    if mime_type not in _TEXT_MIMES:
        return None
    body = await budget.get(f"{DRIVE_ORIGIN}/files/{quoted_id}?alt=media", accept=mime_type)
    return _drive_file_record(file, body, mime_type, collected_at)


async def _drive_file_metadata(budget: _RequestBudget, identifier: str) -> dict[str, Any]:
    """Read only Drive file metadata fields required for type filtering and provenance."""
    fields = "id,name,mimeType,modifiedTime,webViewLink,md5Checksum,version,trashed,parents"
    body = await budget.get(f"{DRIVE_ORIGIN}/files/{quote(identifier, safe='')}?{urlencode({'fields': fields})}")
    value = _json(body)
    if not isinstance(value, dict) or value.get("id") != identifier:
        raise GoogleProviderError("drive_file_invalid")
    return value


async def _expand_drive_scope(
    budget: _RequestBudget, scope_ids: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Resolve selected files and recursively expand folders under shared page, byte and record caps.

    Folder membership is read-only and only parser-supported children are later ingested. Repeated
    folders and children are deduplicated, and hitting a page or record bound fails the whole batch
    so the caller cannot advance a partial snapshot cursor.
    """
    files: dict[str, dict[str, Any]] = {}
    pending = list(scope_ids)
    visited_folders: set[str] = set()
    pages = 0
    while pending:
        identifier = pending.pop()
        if identifier in files:
            continue
        item = await _drive_file_metadata(budget, identifier)
        if item.get("trashed") is True:
            continue
        if item.get("mimeType") != "application/vnd.google-apps.folder":
            files[identifier] = item
            if len(files) + len(visited_folders) > MAX_RECORDS:
                raise rest.CollectionIncomplete
            continue
        if identifier in visited_folders:
            continue
        visited_folders.add(identifier)
        if len(files) + len(visited_folders) > MAX_RECORDS:
            raise rest.CollectionIncomplete
        page_token: str | None = None
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise rest.CollectionIncomplete
            params: dict[str, str | int] = {
                "q": f"'{identifier}' in parents and trashed = false",
                "pageSize": 100,
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,md5Checksum,version,trashed)",
            }
            if page_token:
                params["pageToken"] = page_token
            body = await budget.get(f"{DRIVE_ORIGIN}/files?{urlencode(params)}")
            page = _json(body)
            if not isinstance(page, dict) or not isinstance(page.get("files", []), list):
                raise GoogleProviderError("drive_folder_children_invalid")
            for child in page.get("files", []):
                if not isinstance(child, dict) or not isinstance(child.get("id"), str):
                    raise GoogleProviderError("drive_folder_child_invalid")
                if child.get("trashed") is True:
                    continue
                if child.get("mimeType") == "application/vnd.google-apps.folder":
                    pending.append(child["id"])
                else:
                    files[child["id"]] = child
                    if len(files) + len(visited_folders) > MAX_RECORDS:
                        raise rest.CollectionIncomplete
            page_token = _next_page(page)
            if page_token is None:
                break
    return files


async def _drive_change_in_scope(
    budget: _RequestBudget, file: dict[str, Any], scope_ids: tuple[str, ...],
) -> bool:
    """Check selected-file and ancestor-folder membership without retaining an unbounded file index.

    Drive changes include immediate parents. For nested selected folders, walk parent metadata with a
    depth bound; removed items cannot be safely scoped because Drive omits their former parents.
    """
    selected = set(scope_ids)
    identifier = file.get("id")
    if identifier in selected:
        return True
    parents = file.get("parents", [])
    if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
        raise GoogleProviderError("drive_change_parents_invalid")
    pending = list(parents)
    visited: set[str] = set()
    for _ in range(20):
        if not pending:
            return False
        parent_id = pending.pop()
        if parent_id in selected:
            return True
        if parent_id in visited:
            continue
        visited.add(parent_id)
        ancestor = await _drive_file_metadata(budget, parent_id)
        next_parents = ancestor.get("parents", [])
        if not isinstance(next_parents, list) or any(not isinstance(parent, str) for parent in next_parents):
            raise GoogleProviderError("drive_change_parents_invalid")
        pending.extend(next_parents)
    if pending:
        raise rest.CollectionIncomplete
    return False


async def collect_drive(
    access_token: str,
    file_ids: tuple[str, ...],
    *,
    before_request: rest.BeforeSend,
    cursor: str | None = None,
    collected_at: datetime | None = None,
) -> GoogleCollectionResult:
    """Snapshot selected Drive files in receipt-sized steps, then follow its changes feed.

    The cursor carries a bounded current folder page and offset so a receipt can resume traversal
    without rereading accepted files. Parent ancestry scopes nested-folder changes without retaining
    an unbounded index. Google Docs export as plain text; unsupported binary types are skipped.
    """
    if (not file_ids or any(not isinstance(value, str) or not value or len(value) > 512 or "/" in value for value in file_ids)
            or len(set(file_ids)) != len(file_ids)):
        raise GoogleProviderError("drive_scope_invalid")
    original_cursor = cursor
    state = _read_cursor(cursor, "drive")
    expected_ids = sorted(file_ids)
    scope_hash = hashlib.sha256("\0".join(expected_ids).encode("utf-8")).hexdigest()
    stored_scope = state.get("scope_hash") if state is not None else None
    legacy_scope = state.get("scope_ids", state.get("file_ids")) if state is not None else None
    if state is not None and stored_scope != scope_hash and legacy_scope != expected_ids:
        raise GoogleProviderError("drive_cursor_scope_changed")
    collected = collected_at or datetime.now(UTC)
    if collected.tzinfo is None:
        raise GoogleProviderError("collection_time_invalid")
    budget = _RequestBudget(access_token, before_request)
    records: list[IngestionRecord] = []
    batch_bytes = 0
    if state is None:
        state = {"v": 2, "provider": "drive", "phase": "snapshot", "scope_hash": scope_hash,
                 "scope_index": 0, "folders": [], "folder": None}
    elif state.get("phase") is None:
        # Older cursors already hold a changes start token, so continue without repeating the snapshot.
        old_token = state.get("page_token")
        if not isinstance(old_token, str) or not old_token:
            raise GoogleProviderError("drive_cursor_invalid")
        state = {"v": 2, "provider": "drive", "phase": "changes", "scope_hash": scope_hash,
                 "change_token": old_token, "change_items": [], "change_offset": 0}
    try:
        if state.get("phase") == "snapshot":
            if not isinstance(state.get("change_token"), str):
                start_page = _json(await budget.get(
                    f"{DRIVE_ORIGIN}/changes/startPageToken?{urlencode({'fields': 'startPageToken'})}"))
                start_token = start_page.get("startPageToken") if isinstance(start_page, dict) else None
                if not isinstance(start_token, str) or not start_token:
                    raise GoogleProviderError("drive_start_token_invalid")
                state["change_token"] = start_token
            index, folders = state.get("scope_index"), state.get("folders")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= len(file_ids) or not isinstance(folders, list):
                raise GoogleProviderError("drive_cursor_invalid")
            while True:
                folder = state.get("folder")
                if folder is None and folders:
                    folder = {"id": folders.pop(), "page_token": None, "items": [], "offset": 0, "done": False}
                    state["folder"] = folder
                if folder is None and index < len(file_ids):
                    item = await _drive_file_metadata(budget, file_ids[index])
                    if item.get("trashed") is not True:
                        if item.get("mimeType") == "application/vnd.google-apps.folder":
                            folders.append(file_ids[index])
                        else:
                            record = await _drive_file_content(budget, item, collected)
                            if record is not None:
                                batch_bytes = _append_record(records, record, batch_bytes)
                    index += 1
                    state["scope_index"] = index
                    continue
                if folder is None:
                    state["phase"] = "changes"
                    state["change_items"] = []
                    state["change_offset"] = 0
                    break
                items, offset = folder.get("items"), folder.get("offset")
                if not isinstance(items, list) or isinstance(offset, bool) or not isinstance(offset, int):
                    raise GoogleProviderError("drive_cursor_invalid")
                if offset < len(items):
                    child = items[offset]
                    if not isinstance(child, dict) or not isinstance(child.get("id"), str):
                        raise GoogleProviderError("drive_folder_child_invalid")
                    if child.get("trashed") is not True:
                        if child.get("mimeType") == "application/vnd.google-apps.folder":
                            folders.append(child["id"])
                        else:
                            record = await _drive_file_content(budget, child, collected)
                            if record is not None:
                                batch_bytes = _append_record(records, record, batch_bytes)
                    folder["offset"] = offset + 1
                    continue
                if folder.get("done") is True:
                    state["folder"] = None
                    continue
                params: dict[str, str | int] = {
                    "q": f"'{folder['id']}' in parents and trashed = false", "pageSize": 3,
                    "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,md5Checksum,version,trashed,parents)",
                }
                if folder.get("page_token"):
                    params["pageToken"] = folder["page_token"]
                page = _json(await budget.get(f"{DRIVE_ORIGIN}/files?{urlencode(params)}"))
                if not isinstance(page, dict) or not isinstance(page.get("files", []), list):
                    raise GoogleProviderError("drive_folder_children_invalid")
                children = page.get("files", [])
                if len(children) > 3 or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in children):
                    raise GoogleProviderError("drive_folder_child_invalid")
                next_page = _next_page(page)
                folder.update({"items": children, "offset": 0, "page_token": next_page, "done": next_page is None})

        while state.get("phase") == "changes":
            items, offset = state.get("change_items"), state.get("change_offset")
            if not isinstance(items, list) or isinstance(offset, bool) or not isinstance(offset, int):
                raise GoogleProviderError("drive_cursor_invalid")
            if offset < len(items):
                change = items[offset]
                if not isinstance(change, dict) or not isinstance(change.get("fileId"), str):
                    raise GoogleProviderError("drive_change_invalid")
                file = change.get("file")
                if change.get("removed") is not True:
                    if not isinstance(file, dict) or file.get("id") != change["fileId"]:
                        raise GoogleProviderError("drive_change_file_invalid")
                    if file.get("trashed") is not True and await _drive_change_in_scope(budget, file, file_ids):
                        record = await _drive_file_content(budget, file, collected)
                        if record is not None:
                            batch_bytes = _append_record(records, record, batch_bytes)
                state["change_offset"] = offset + 1
                continue
            next_page = state.pop("change_next_page", None)
            if next_page is not None:
                state["change_token"] = next_page
                state["change_items"], state["change_offset"] = [], 0
                continue
            new_start = state.pop("change_new_start", None)
            if new_start is not None:
                if not isinstance(new_start, str) or not new_start:
                    raise GoogleProviderError("drive_next_token_invalid")
                state = {"v": 2, "provider": "drive", "phase": "changes", "scope_hash": scope_hash,
                         "change_token": new_start, "change_items": [], "change_offset": 0}
                break
            token = state.get("change_token")
            if not isinstance(token, str) or not token:
                raise GoogleProviderError("drive_cursor_invalid")
            page = _json(await budget.get(f"{DRIVE_ORIGIN}/changes?{urlencode({
                'pageToken': token, 'pageSize': 3,
                'fields': 'nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,modifiedTime,webViewLink,md5Checksum,version,trashed,parents))',
            })}"))
            if not isinstance(page, dict) or not isinstance(page.get("changes", []), list):
                raise GoogleProviderError("drive_changes_invalid")
            changes = page.get("changes", [])
            if len(changes) > 3 or any(not isinstance(item, dict) for item in changes):
                raise GoogleProviderError("drive_change_invalid")
            next_page = _next_page(page)
            new_start = page.get("newStartPageToken")
            if next_page is None and (not isinstance(new_start, str) or not new_start):
                raise GoogleProviderError("drive_next_token_invalid")
            state["change_items"], state["change_offset"] = changes, 0
            if next_page is not None:
                state["change_next_page"] = next_page
            else:
                state["change_new_start"] = new_start
    except _ReceiptPause as pause:
        encoded = _cursor_json(state)
        if encoded == original_cursor and not records:
            if pause.deferred is not None:
                raise pause.deferred
            raise GoogleProviderError("drive_cursor_no_progress") from pause
        return GoogleCollectionResult(tuple(records), encoded, "truncated")
    return GoogleCollectionResult(tuple(records), _cursor_json(state), "returned_snapshot")


__all__ = [
    "GoogleCollectionResult",
    "GoogleProviderError",
    "collect_calendar",
    "collect_drive",
    "collect_gmail",
]
