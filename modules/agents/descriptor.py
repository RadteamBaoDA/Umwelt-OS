"""Declare the assistant and fixed-roster specialist profile capability."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class AgentDescriptor(ModuleDescriptor):
    """Expose bounded owner assistant and profile-run contracts through Agents."""

    id: str = "agents"
    name: str = "Agents"
    version: str = "1.0.0"
    description: str = "Bounded assistant and owner-configured specialist orchestration."
    enabled: bool = True
    dependencies: tuple[str, ...] = ("tools", "chat")
    scheduled_jobs: tuple[str, ...] = ("process_agent_run",)
    provides: tuple[str, ...] = ("agent_runs",)
    requires: tuple[str, ...] = ("tool_registry",)
    routes: tuple[str, ...] = ("/api/v1/agents", "/api/v1/agents/profiles", "/api/v1/agent-runs")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ("agents.handoff",)
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.agents.routes", 460),
    )
    erasure_module: str | None = "modules.agents.erasure"
    erasure_order: int = 10


descriptor = AgentDescriptor()
