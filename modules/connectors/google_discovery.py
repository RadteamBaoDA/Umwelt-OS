"""Bounded, read-only Google resource discovery for owner connector setup."""

import re
from typing import Any
from urllib.parse import urlencode

from modules.connectors.providers import google_api, rest

_FOLDER_MIME = "application/vnd.google-apps.folder"
_MAX_DISCOVERY_PAGES = 5
_MAX_DISCOVERY_ITEMS = 500


async def discover_google_resources(
    provider: str,
    access_token: str,
    *,
    parent_id: str | None,
    before_request: rest.BeforeSend,
) -> tuple[list[dict[str, str]], bool]:
    """List safe resource choices using fixed Google endpoints and bounded pinned requests.

    Drive folders are returned as selectable IDs and can also be passed back as the next
    ``parent_id`` to browse; unsupported Drive MIME types are omitted. Gmail labels and calendars
    are listed directly. Each response uses the shared DNS-pinned transport and is limited to five
    pages, 500 rows, and the existing 25 MiB transport budget. ``before_request`` rechecks owner,
    source and grant authority immediately before every send. Only bounded names, IDs, MIME types
    and timestamps are returned; tokens and account data never leave this function.
    """
    budget = google_api._RequestBudget(access_token, before_request)
    if provider == "google_mail":
        body = await budget.get(f"{google_api.GMAIL_ORIGIN}/labels")
        payload = google_api._json(body)
        raw_rows = payload.get("labels") if isinstance(payload, dict) else None
        if not isinstance(raw_rows, list):
            raise google_api.GoogleProviderError("gmail_labels_invalid")
        label_rows: list[Any] = raw_rows
        return _safe_rows(label_rows, kind="label"), len(label_rows) > _MAX_DISCOVERY_ITEMS
    if provider == "google_calendar":
        calendar_rows: list[Any] = []
        page_token: str | None = None
        for _ in range(_MAX_DISCOVERY_PAGES):
            calendar_params: dict[str, str | int] = {"maxResults": 250}
            if page_token:
                calendar_params["pageToken"] = page_token
            body = await budget.get(
                f"https://www.googleapis.com/calendar/v3/users/me/calendarList?{urlencode(calendar_params)}"
            )
            payload = google_api._json(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
                raise google_api.GoogleProviderError("calendar_list_invalid")
            items: list[Any] = payload.get("items", [])
            calendar_rows.extend(items)
            next_token = payload.get("nextPageToken")
            if next_token is None:
                return (
                    _safe_rows(calendar_rows[:_MAX_DISCOVERY_ITEMS], kind="calendar"),
                    len(calendar_rows) > _MAX_DISCOVERY_ITEMS,
                )
            if not isinstance(next_token, str) or not next_token:
                raise google_api.GoogleProviderError("calendar_page_token_invalid")
            page_token = next_token
            if len(calendar_rows) >= _MAX_DISCOVERY_ITEMS:
                return _safe_rows(calendar_rows[:_MAX_DISCOVERY_ITEMS], kind="calendar"), True
        raise rest.CollectionIncomplete
    if provider == "google_drive":
        if parent_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{5,200}", parent_id):
            raise google_api.GoogleProviderError("drive_parent_invalid")
        query_parent = parent_id or "root"
        drive_query = f"'{query_parent.replace(chr(39), chr(92) + chr(39))}' in parents and trashed = false"
        drive_rows: list[Any] = []
        page_token = None
        for _ in range(_MAX_DISCOVERY_PAGES):
            drive_params: dict[str, str | int] = {
                "q": drive_query,
                "pageSize": 100,
                "orderBy": "folder,name",
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            }
            if page_token:
                drive_params["pageToken"] = page_token
            body = await budget.get(f"{google_api.DRIVE_ORIGIN}/files?{urlencode(drive_params)}")
            payload = google_api._json(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("files", []), list):
                raise google_api.GoogleProviderError("drive_file_list_invalid")
            files: list[Any] = payload.get("files", [])
            drive_rows.extend(files)
            next_token = payload.get("nextPageToken")
            if next_token is None:
                return _drive_choices(drive_rows[:_MAX_DISCOVERY_ITEMS]), len(drive_rows) > _MAX_DISCOVERY_ITEMS
            if not isinstance(next_token, str) or not next_token:
                raise google_api.GoogleProviderError("drive_page_token_invalid")
            page_token = next_token
            if len(drive_rows) >= _MAX_DISCOVERY_ITEMS:
                return _drive_choices(drive_rows[:_MAX_DISCOVERY_ITEMS]), True
        raise rest.CollectionIncomplete
    raise google_api.GoogleProviderError("google_discovery_provider_invalid")


def _safe_rows(values: list[Any], *, kind: str) -> list[dict[str, str]]:
    """Project provider rows into bounded identifiers and labels, rejecting malformed API schemas."""
    result: list[dict[str, str]] = []
    for value in values[:_MAX_DISCOVERY_ITEMS]:
        if not isinstance(value, dict):
            raise google_api.GoogleProviderError("google_discovery_row_invalid")
        identifier = value.get("id")
        name = value.get("name") if kind == "label" else value.get("summary") if kind == "calendar" else value.get("name")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
            raise google_api.GoogleProviderError("google_discovery_id_invalid")
        if kind == "label" and not re.fullmatch(r"[A-Za-z0-9_-]{1,225}", identifier):
            raise google_api.GoogleProviderError("google_discovery_id_invalid")
        if kind == "calendar" and not re.fullmatch(r"[^\s\x00-\x1f]{1,256}", identifier):
            raise google_api.GoogleProviderError("google_discovery_id_invalid")
        if kind == "drive" and not re.fullmatch(r"[A-Za-z0-9_-]{5,200}", identifier):
            raise google_api.GoogleProviderError("google_discovery_id_invalid")
        if kind == "drive" and not isinstance(value.get("mimeType"), str):
            raise google_api.GoogleProviderError("drive_mime_type_invalid")
        if not isinstance(name, str) or not name or len(name) > 512:
            raise google_api.GoogleProviderError("google_discovery_name_invalid")
        row = {"id": identifier, "name": name[:255], "kind": kind}
        mime_type = value.get("mimeType")
        modified = value.get("modifiedTime")
        if isinstance(mime_type, str):
            row["mime_type"] = mime_type[:255]
            if kind == "drive" and mime_type == _FOLDER_MIME:
                row["kind"] = "folder"
        if isinstance(modified, str):
            row["modified_time"] = modified[:64]
        result.append(row)
    return result


def _drive_choices(values: list[Any]) -> list[dict[str, str]]:
    """Expose folders and only text or Google Docs file types supported by the document parser."""
    supported = google_api._TEXT_MIMES | {"application/vnd.google-apps.document", _FOLDER_MIME}
    projected = _safe_rows(values, kind="drive")
    return [row for row in projected if row.get("mime_type") in supported]
