"""Platform module descriptor for settings: composition metadata only, not a lifecycle-toggleable module."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class SettingsDescriptor(ModuleDescriptor):
    """Declare settings routers and erasure ownership for application composition."""

    id: str = "settings"
    name: str = "Settings"
    description: str = "Owner preferences, AI and retention settings, module lifecycle and onboarding."
    routes: tuple[str, ...] = ("/api/v1/settings",)
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.settings.routes", 230),
        RouterSpec("modules.settings.onboarding_routes", 250),
    )
    erasure_module: str | None = "modules.settings.erasure"
    erasure_order: int = 180


descriptor = SettingsDescriptor()
