"""Declare goals module descriptor for core registration and capability discovery."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class GoalsDescriptor(ModuleDescriptor):
    """Declare goals module metadata, routes, events, and tools."""

    id: str = "goals"
    name: str = "Goals"
    version: str = "1.0.0"
    description: str = "Track strategic goals, milestones, and approved plan execution."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("tasks",)
    optional_dependencies: tuple[str, ...] = ("agents", "knowledge.entities")
    provides: tuple[str, ...] = ("goals",)
    requires: tuple[str, ...] = ("tasks",)
    routes: tuple[str, ...] = (
        "/api/v1/goals",
        "/api/v1/goals/{id}",
        "/api/v1/goals/{id}/accept-plan",
    )
    emitted_events: tuple[str, ...] = ("goal.created", "goal.updated", "goal.plan_accepted")
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = (
        "goals.list",
        "goals.get",
        "goals.create",
        "goals.update",
        "goals.delete",
    )
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (RouterSpec("modules.goals.routes", 340),)
    erasure_module: str | None = "modules.goals.erasure"
    erasure_order: int = 60


descriptor = GoalsDescriptor()
