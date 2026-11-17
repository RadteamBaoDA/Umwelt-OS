"""Agent-facing ``ModelGatewayFactory`` binding the settings capability-evidence lookup.

The implementation moved to ``core.model_gateway.factory`` (shared with chat). This module keeps the
historical import path and wires ``settings_public.capability_evidence``; ``ModelGateway`` is re-exported here and resolved through this namespace so the historical patch target
``modules.agents.gateway_factory.ModelGateway`` keeps intercepting construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.model_gateway.client import ModelGateway
from core.model_gateway.factory import ModelGatewayFactory as CoreModelGatewayFactory
from core.model_gateway.schemas import AIExecutionConfig
from modules.settings import public as settings_public

__all__ = ["ModelGateway", "ModelGatewayFactory", "settings_public"]


def _capability_evidence(session_factory: Any, config: AIExecutionConfig) -> Any:
    """Resolve capability evidence through ``settings_public`` at call time (patch-friendly)."""
    return settings_public.capability_evidence(session_factory, config)


@dataclass(frozen=True)
class ModelGatewayFactory(CoreModelGatewayFactory):
    """Core factory defaulting to the settings capability-evidence provider."""

    evidence_provider: Any = _capability_evidence

    def _new_gateway(self, **kwargs: Any) -> ModelGateway:
        """Construct through this module's ``ModelGateway`` name (patch-compatible with the old location)."""
        return (self.gateway_cls or ModelGateway)(**kwargs)
