"""Declare the notifications module descriptor for core registration and capability discovery."""

from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationsDescriptor:
    """Declare notifications module metadata, routes and events."""

    id: str = "notifications"
    name: str = "Notifications"
    version: str = "1.0.0"
    description: str = "Deduplicated, owner-read-tracked actionable notifications."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("notifications",)
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/notifications", "/api/v1/notifications/{id}")
    emitted_events: tuple[str, ...] = ("notification.created",)
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = NotificationsDescriptor()
