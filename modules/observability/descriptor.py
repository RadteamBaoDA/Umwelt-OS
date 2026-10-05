from dataclasses import dataclass


@dataclass(frozen=True)
class ObservabilityDescriptor:
    """Declare owner-only bounded metrics and cross-module run inspection."""

    id: str = "observability"
    name: str = "Observability"
    version: str = "1.0.0"
    description: str = "Bounded metrics, run inspection and telemetry redaction."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("metrics", "run_inspection")
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/system/metrics", "/api/v1/system/runs")
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = ObservabilityDescriptor()
