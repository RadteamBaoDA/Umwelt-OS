"""Platform module descriptor for translations: composition metadata only, not a lifecycle-toggleable module."""

from dataclasses import dataclass

from core.module_descriptor import CapabilitySpec, ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class TranslationsDescriptor(ModuleDescriptor):
    """Declare translations routers and erasure ownership for application composition."""

    id: str = "translations"
    name: str = "Translations"
    description: str = "On-demand resource translation with protected-content handling."
    routes: tuple[str, ...] = ("/api/v1/translations",)
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.translations.routes", 240),
    )
    capabilities: tuple[CapabilitySpec, ...] = (
        CapabilitySpec("translations.purge_resource_translations", "modules.translations.public:purge_resource_translations"),
    )
    erasure_module: str | None = "modules.translations.erasure"
    erasure_order: int = 230


descriptor = TranslationsDescriptor()
