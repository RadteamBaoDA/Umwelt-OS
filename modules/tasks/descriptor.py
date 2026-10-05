"""Declare tasks module descriptor for core registration and capability discovery."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TasksDescriptor:
    """Declare tasks module metadata, routes, events, and tools."""

    id: str = "tasks"
    name: str = "Tasks"
    version: str = "1.0.0"
    description: str = "Manage actionable tasks, due dates, statuses, and optimistic revisions."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("tasks",)
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = (
        "/api/v1/tasks",
        "/api/v1/tasks/{id}",
    )
    emitted_events: tuple[str, ...] = ("task.created", "task.updated", "task.deleted")
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = (
        "tasks.list",
        "tasks.create",
        "tasks.update",
        "tasks.complete",
        "tasks.delete",
    )
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = TasksDescriptor()
