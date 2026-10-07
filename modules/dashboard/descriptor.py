from dataclasses import dataclass


@dataclass(frozen=True)
class DashboardDescriptor:
    """Declare Dashboard routes, lifecycle-gated schedules, source dependencies, and events."""

    id: str = "dashboard"
    name: str = "Dashboard"
    version: str = "1.0.0"
    description: str = "Save owner-selected dashboard layouts and gadget configuration."
    enabled: bool = True
    dependencies: tuple[str, ...] = (
        "sources", "tasks", "goals", "news", "knowledge.timeline", "notifications",
        "knowledge.documents", "knowledge.observations",
    )
    scheduled_jobs: tuple[str, ...] = ("run_scheduled_brief", "run_scheduled_highlights")
    provides: tuple[str, ...] = ("dashboards", "gadget_definitions", "daily_context", "daily_brief")
    requires: tuple[str, ...] = ("sources", "world_observations")
    routes: tuple[str, ...] = (
        "/api/v1/dashboards",
        "/api/v1/dashboards/{id}",
        "/api/v1/dashboards/{id}/groups",
        "/api/v1/dashboards/{id}/groups/{group_id}",
        "/api/v1/dashboards/{id}/instances",
        "/api/v1/dashboards/{id}/instances/{instance_id}",
        "/api/v1/dashboards/{id}/layout",
        "/api/v1/gadget-definitions",
        "/api/v1/gadget-definitions/{id}",
        "/api/v1/gadget-definitions/{definition_id}/highlights",
        "/api/v1/gadget-renderers",
        "/api/v1/gadget-sources",
        "/api/v1/dashboard-presets",
        "/api/v1/dashboard-presets/{preset_id}/preview",
        "/api/v1/dashboard-presets/{preset_id}/apply",
        "/api/v1/context/current",
        "/api/v1/context/daily",
        "/api/v1/briefs",
        "/api/v1/briefs/generate",
        "/api/v1/briefs/schedule",
    )
    emitted_events: tuple[str, ...] = ("dashboard.changed",)
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = DashboardDescriptor()
