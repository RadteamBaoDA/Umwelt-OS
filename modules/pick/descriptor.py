"""Platform module descriptor for pick: composition metadata only, not a lifecycle-toggleable module."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class PickDescriptor(ModuleDescriptor):
    """Declare pick routers and erasure ownership for application composition."""

    id: str = "pick"
    name: str = "Pick"
    description: str = "Deterministic selection helpers exposed over the pick API."
    routes: tuple[str, ...] = ("/api/v1/pick",)
    routers: tuple[RouterSpec, ...] = (RouterSpec("modules.pick.routes", 380),)


descriptor = PickDescriptor()
