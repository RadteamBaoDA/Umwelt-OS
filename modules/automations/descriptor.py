"""Declare the automations module descriptor for core registration and dependency validation."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class AutomationsDescriptor(ModuleDescriptor):
    """Declare automations metadata, routes and events; tools back the Automation Agent."""

    id: str = "automations"
    name: str = "Automations"
    version: str = "1.0.0"
    description: str = "Owner-defined bounded rules: trigger, deterministic conditions, actions."
    enabled: bool = True
    # Every action target module is a hard dependency; trigger modules are re-checked per rule.
    dependencies: tuple[str, ...] = (
        "tasks", "goals", "notifications", "dashboard", "agents", "tools", "sources",
        "knowledge.documents", "knowledge.entities", "knowledge.timeline",
    )
    optional_dependencies: tuple[str, ...] = ("chat", "ingestion")
    scheduled_jobs: tuple[str, ...] = ("process_automation_run",)
    provides: tuple[str, ...] = ("automation_rules",)
    requires: tuple[str, ...] = ("tasks", "notifications", "dashboard", "agents")
    routes: tuple[str, ...] = (
        "/api/v1/automations",
        "/api/v1/automations/{id}",
        "/api/v1/automations/preview",
    )
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ("automations.list", "automations.create")
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.automations.routes", 390),
        RouterSpec("modules.automations.routes:webhook_router", 400),
    )
    erasure_module: str | None = "modules.automations.erasure"
    erasure_order: int = 20


descriptor = AutomationsDescriptor()
