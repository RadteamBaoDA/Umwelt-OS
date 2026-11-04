"""Single construction point for fenced ``ModelGateway`` clients used by agent workflows.

Every caller used to repeat the same twelve-argument gateway constructor call, so a new fence or
policy argument had to be threaded through each copy. ``ModelGatewayFactory`` binds the per-run
handles once and derives the per-request pieces (endpoint, credential, identity, revision, kind)
from the resolved ``AIExecutionConfig`` so callers only supply what differs between requests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from core.model_gateway.client import ModelGateway
from core.model_gateway.schemas import AIExecutionConfig
from core.workspaces.schemas import InternalJobScope
from modules.settings import public as settings_public


@dataclass(frozen=True)
class ModelGatewayFactory:
    """Build ``ModelGateway`` clients bound to one worker's Redis, session factory and job scope.

    The factory holds no per-request state: ``create`` is pure construction with no I/O, so it may be
    called once per model request and never opens a session, takes a lock, or commits.

    Attributes:
        session_factory: Source of the short read sessions used for capability evidence.
        redis: Shared Redis handle for the gateway's breaker and rate state.
        scope: Workspace-scoped job identity stamped on every request policy.
        approved_endpoint_cidrs: Deployment CIDR allow-list for private endpoints.
    """

    session_factory: Any
    redis: Redis
    scope: InternalJobScope
    approved_endpoint_cidrs: tuple[str, ...]

    @classmethod
    def for_settings(
        cls, session_factory: Any, redis: Redis, scope: InternalJobScope, cidrs: Iterable[str],
    ) -> ModelGatewayFactory:
        """Normalize a settings CIDR iterable into the immutable tuple the gateway expects."""
        return cls(session_factory, redis, scope, tuple(cidrs))

    def create(
        self,
        config: AIExecutionConfig,
        *,
        timeout_seconds: float,
        before_send: Callable[[], Awaitable[None]],
        destination_id: str | None = None,
        base_url: str | None = None,
        with_evidence: bool = True,
    ) -> ModelGateway:
        """Return a gateway for ``config`` with the per-request timeout and egress hook.

        ``config`` must already be role-derived by the caller (``derive_for_role``), because the
        endpoint, credential and identity fields are copied from it verbatim.

        ``destination_id`` and ``base_url`` override the config's values for callers that resolve a
        fallback (for example a placeholder local endpoint); ``with_evidence=False`` skips the
        capability-evidence lookup for one-shot classifiers that never learn capabilities.
        """
        evidence = settings_public.capability_evidence(self.session_factory, config) if with_evidence else None
        kwargs: dict[str, Any] = {"evidence": evidence} if with_evidence else {}
        return ModelGateway(
            redis=self.redis,
            base_url=base_url if base_url is not None else config.gateway_base_url,
            api_key=config.gateway_api_key,
            destination_id=destination_id if destination_id is not None else (config.endpoint_destination_id or ""),
            timeout_seconds=timeout_seconds, scope=self.scope,
            gateway_identity=config.gateway_identity, model_identity=config.model_identity,
            configuration_revision=config.configuration_revision, before_send=before_send,
            approved_endpoint_cidrs=self.approved_endpoint_cidrs,
            allow_public_endpoints=config.allow_public_providers, kind=config.gateway_kind,
            **kwargs,
        )
