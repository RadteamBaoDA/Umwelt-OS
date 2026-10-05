"""Unit tests for notification module schemas, link path validation, and channel routing.

Covers:
- NotificationEmit (NotificationCreate) validation: dedupe_key, kind, title, body, params
- In-app relative link security validation (rejecting absolute URLs, schemes, backslashes, double slashes)
- NotificationRead projection and NotificationPage schema
- NotificationPatch validation (marking read/unread)
- Channel routing: notification kinds (task_due, automation, system, approval, news) and parameter payload boundaries
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.notifications.schemas import (
    NotificationEmit,
    NotificationPage,
    NotificationPatch,
    NotificationRead,
)


class TestNotificationEmitSchemas:
    """Tests for NotificationEmit / creation validation and in-app link safety."""

    def test_notification_emit_minimal(self) -> None:
        """Verify minimal valid emission with required kind and dedupe_key."""
        emit = NotificationEmit(
            dedupe_key="task-due-1001",
            kind="task_due",
            title="Task deadline approaching",
        )
        assert emit.dedupe_key == "task-due-1001"
        assert emit.kind == "task_due"
        assert emit.title == "Task deadline approaching"
        assert emit.body is None
        assert emit.params == {}
        assert emit.link is None

    def test_notification_emit_valid_in_app_link(self) -> None:
        """Verify valid relative in-app links starting with a single '/'."""
        valid_links = [
            "/tasks/123",
            "/chat?date=2026-10-05",
            "/dashboard",
            "/settings/privacy",
        ]
        for link in valid_links:
            emit = NotificationEmit(
                dedupe_key=f"key-{link}",
                kind="system",
                link=link,
            )
            assert emit.link == link

    def test_notification_emit_rejects_external_and_malformed_links(self) -> None:
        """Reject links with protocol schemes, protocol-relative '//', backslashes, or missing leading '/'."""
        invalid_links = [
            "https://evil.com/phishing",
            "http://localhost:3000",
            "//external-site.com",
            "/tasks\\backwards",
            "tasks/no-leading-slash",
            "javascript:alert(1)",
        ]
        for bad_link in invalid_links:
            with pytest.raises(ValidationError, match="link must be a relative in-app path"):
                NotificationEmit(
                    dedupe_key="key",
                    kind="system",
                    link=bad_link,
                )

    def test_notification_emit_extra_fields_forbidden(self) -> None:
        """Extra fields are rejected to prevent unauthorized parameter tampering."""
        with pytest.raises(ValidationError):
            NotificationEmit.model_validate({
                "dedupe_key": "k1",
                "kind": "system",
                "unauthorized_field": "injected",
            })

    def test_notification_emit_params_bound(self) -> None:
        """Params dictionary bounds: at most 16 entries of string or integer."""
        valid_params = {f"k{i}": i for i in range(16)}
        emit = NotificationEmit(dedupe_key="key", kind="system", params=valid_params)
        assert len(emit.params) == 16

        oversized_params = {f"k{i}": i for i in range(17)}
        with pytest.raises(ValidationError):
            NotificationEmit(dedupe_key="key", kind="system", params=oversized_params)


class TestNotificationReadAndPageSchemas:
    """Tests for NotificationRead, NotificationPage, and NotificationPatch."""

    def test_notification_read_projection(self) -> None:
        """Verify NotificationRead model properties."""
        notif_id = uuid4()
        now = datetime.now(UTC)
        read = NotificationRead(
            id=notif_id,
            kind="task_due",
            title="Overdue task",
            body="Review quarterly taxes is due today.",
            params={"task_id": "123", "priority": 1},
            link="/tasks/123",
            read_at=None,
            created_at=now,
        )
        assert read.id == notif_id
        assert read.kind == "task_due"
        assert read.read_at is None
        assert read.created_at == now

    def test_notification_page_schema(self) -> None:
        """Verify NotificationPage includes items and unread count."""
        page = NotificationPage(items=[], unread_count=5)
        assert page.items == []
        assert page.unread_count == 5

    def test_notification_patch_schema(self) -> None:
        """Verify NotificationPatch validates boolean read flag."""
        patch_read = NotificationPatch(read=True)
        assert patch_read.read is True

        patch_unread = NotificationPatch(read=False)
        assert patch_unread.read is False


class TestChannelRouting:
    """Tests for channel and kind routing categorization."""

    def test_channel_kinds_and_routing_purposes(self) -> None:
        """Verify distinct notification channels and their routing targets."""
        channels = [
            ("task_due", {"task_id": "t-1"}, "/tasks/t-1"),
            ("goal_deadline", {"goal_id": "g-1"}, "/goals/g-1"),
            ("automation", {"automation_id": "a-1"}, "/automations"),
            ("agent_approval", {"approval_id": "appr-1"}, "/approvals"),
            ("news", {"story_id": "s-1"}, "/news"),
            ("system", {"code": "HEALTH_OK"}, "/settings"),
        ]
        for kind, params, link in channels:
            emit = NotificationEmit(
                dedupe_key=f"dedupe-{kind}-1",
                kind=kind,
                title=f"Notification for {kind}",
                params=params,
                link=link,
            )
            assert emit.kind == kind
            assert emit.link == link
            assert emit.params == params
