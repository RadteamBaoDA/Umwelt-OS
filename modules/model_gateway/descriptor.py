"""Platform module descriptor for model_gateway: composition metadata only, not a lifecycle-toggleable module."""

from dataclasses import dataclass

from core.module_descriptor import ModuleDescriptor, RouterSpec


@dataclass(frozen=True)
class ModelGatewayDescriptor(ModuleDescriptor):
    """Declare model_gateway routers and erasure ownership for application composition."""

    id: str = "model_gateway"
    name: str = "Model gateway"
    description: str = "Owner model-provider configuration and connectivity tests."
    routes: tuple[str, ...] = ("/api/v1/settings/models",)
    routers: tuple[RouterSpec, ...] = (
        RouterSpec("modules.model_gateway.routes", 260),
    )


descriptor = ModelGatewayDescriptor()
