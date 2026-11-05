"""Platform module descriptor for settings: composition metadata only, not a lifecycle-toggleable module."""

from dataclasses import dataclass

from core.module_descriptor import CapabilitySpec, ModuleDescriptor, RouterSpec


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
    capabilities: tuple[CapabilitySpec, ...] = (
        CapabilitySpec("settings.set_initial_owner_display_name", "modules.settings.public:set_initial_owner_display_name"),
        CapabilitySpec("settings.normalize_display_name", "modules.settings.public:normalize_display_name"),
        CapabilitySpec("settings.read_module_availability", "modules.settings.public:read_module_availability"),
    )
    erasure_module: str | None = "modules.settings.erasure"
    erasure_order: int = 180


descriptor = SettingsDescriptor()
