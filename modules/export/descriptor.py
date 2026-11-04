"""Declare owner-scoped portable exports through explicit public domain projections."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class ExportDescriptor(ModuleDescriptor):
    """Expose credential-free portable data export independently of source module toggles."""

    id: str = "export"
    name: str = "Data export"
    version: str = "1.0.0"
    description: str = "Bounded owner-authorized JSON, Markdown, and CSV projections."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    scheduled_jobs: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("portable_exports",)
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/exports",)
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.export.routes", 40),
    )


descriptor = ExportDescriptor()
