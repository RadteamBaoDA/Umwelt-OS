"""Opaque list and detail cursor encoding/decoding with filter binding."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from core.errors import ValidationFailedError
from core.pagination import b64url_encode
from core.workspaces.schemas import AccessFence
from modules.news.schemas import (
    StoryCursor,
    StoryFilter,
)


def _filter_hash(filters: StoryFilter) -> str:
    """Hash every story query constraint except cursor and page size."""
    value = filters.model_dump(exclude={"cursor", "limit"}, mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _encode_cursor(cursor: StoryCursor) -> str:
    """Serialize and sign no data, using canonical base64 plus owner/filter binding."""
    raw = cursor.model_dump_json(exclude_none=False)
    return b64url_encode(raw.encode())


def _decode_cursor(value: str, access_fence: AccessFence, filters: StoryFilter) -> StoryCursor:
    """Reject legacy, cross-workspace, stale-admission, or query-mismatched cursors."""
    try:
        if not value or len(value) > 4096 or "=" in value:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        cursor = StoryCursor.model_validate_json(raw)
        if (
            _encode_cursor(cursor) != value
            or cursor.domain != "news_stories"
            or cursor.workspace_id != access_fence.workspace_id
            or cursor.actor_user_id != access_fence.user_id
            or cursor.membership_revision != access_fence.membership_revision
            or cursor.configuration_revision != access_fence.configuration_revision
            or cursor.filter_hash != _filter_hash(filters)
            or cursor.sort != "observed_at_desc_story_id_desc"
        ):
            raise ValueError
        return cursor
    except (ValueError, UnicodeDecodeError, ValidationError) as exc:
        raise ValidationFailedError("Invalid story cursor") from exc


DETAIL_CURSOR_DOMAIN = "news_story_detail"


DETAIL_CURSOR_SORT = "support_key_asc"


_DETAIL_CURSOR_FIELDS = {
    "v",
    "domain",
    "workspace_id",
    "actor_user_id",
    "membership_revision",
    "configuration_revision",
    "story_id",
    "source_ids",
    "as_of",
    "after",
    "source_selection_incomplete",
    "sort",
}


def _encode_detail_cursor(
    fence: AccessFence,
    story_id: UUID,
    source_ids: tuple[UUID, ...],
    as_of: datetime,
    after: tuple[UUID, UUID, UUID],
    source_selection_incomplete: bool,
) -> str:
    """Bind a Story detail evidence token to the admitted workspace, actor, revisions and snapshot."""
    raw = json.dumps(
        {
            "v": 2,
            "domain": DETAIL_CURSOR_DOMAIN,
            "workspace_id": str(fence.workspace_id),
            "actor_user_id": str(fence.user_id),
            "membership_revision": fence.membership_revision,
            "configuration_revision": fence.configuration_revision,
            "story_id": str(story_id),
            "source_ids": sorted(str(value) for value in source_ids),
            "as_of": as_of.isoformat(),
            "after": [str(value) for value in after],
            "source_selection_incomplete": source_selection_incomplete,
            "sort": DETAIL_CURSOR_SORT,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return b64url_encode(raw.encode())


def _decode_detail_cursor(
    value: str,
    fence: AccessFence,
    story_id: UUID,
    requested_source_ids: tuple[UUID, ...],
) -> tuple[tuple[UUID, ...], datetime, tuple[UUID, UUID, UUID], bool]:
    """Reject legacy (six-field), cross-workspace, stale-revision or mismatched detail tokens with HTTP 422."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        parts = json.loads(raw)
        if (
            not isinstance(parts, dict)
            or set(parts) != _DETAIL_CURSOR_FIELDS
            or parts["v"] != 2
            or parts["domain"] != DETAIL_CURSOR_DOMAIN
            or parts["sort"] != DETAIL_CURSOR_SORT
            or parts["workspace_id"] != str(fence.workspace_id)
            or parts["actor_user_id"] != str(fence.user_id)
            or parts["membership_revision"] != fence.membership_revision
            or parts["configuration_revision"] != fence.configuration_revision
            or parts["story_id"] != str(story_id)
            or not isinstance(parts["source_ids"], list)
            or len(parts["source_ids"]) > 32
            or len(set(parts["source_ids"])) != len(parts["source_ids"])
            or (
                requested_source_ids
                and sorted(map(str, requested_source_ids)) != sorted(parts["source_ids"])
            )
            or not isinstance(parts["source_selection_incomplete"], bool)
            or not isinstance(parts["after"], list)
            or len(parts["after"]) != 3
            or b64url_encode(raw.encode()) != value
        ):
            raise ValueError
        as_of = datetime.fromisoformat(parts["as_of"])
        if as_of.utcoffset() is None:
            raise ValueError
        after = (UUID(parts["after"][0]), UUID(parts["after"][1]), UUID(parts["after"][2]))
        return (
            tuple(UUID(item) for item in parts["source_ids"]),
            as_of,
            after,
            parts["source_selection_incomplete"],
        )
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError("Invalid story evidence cursor") from exc
