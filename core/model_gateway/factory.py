"""Single construction point for fenced ``ModelGateway`` clients shared by chat and agent workflows.

Every caller used to repeat the same twelve-argument gateway constructor call, so a new fence or
policy argument had to be threaded through each copy. ``ModelGatewayFactory`` binds the per-run
handles once and derives the per-request pieces (endpoint, credential, identity, revision, kind)
from the resolved ``AIExecutionConfig`` so callers only supply what differs between requests.

The factory lives in ``core`` so it cannot depend on ``modules.settings``: the capability-evidence
lookup is injected as ``evidence_provider`` (callers pass ``settings_public.capability_evidence``).
``gateway_cls`` lets a module keep resolving ``ModelGateway`` through its own namespace so tests that
patch that name keep intercepting construction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from core.model_gateway.client import ModelGateway
from core.model_gateway.schemas import AIExecutionConfig
from core.workspaces.schemas import Scope


@dataclass(frozen=True)
class ModelGatewayFactory:
    """Build ``ModelGateway`` clients bound to one worker's Redis, session factory and scope.

    The factory holds no per-request state: ``create`` is pure construction with no I/O, so it may be
    called once per model request and never opens a session, takes a lock, or commits.

    Attributes:
        session_factory: Source of the short read sessions used for capability evidence.
        redis: Shared Redis handle for the gateway's breaker and rate state.
        scope: Workspace-scoped identity stamped on every request policy.
        approved_endpoint_cidrs: Deployment CIDR allow-list for private endpoints.
        evidence_provider: ``(session_factory, config) -> evidence`` used when ``with_evidence``;
            ``None`` passes ``evidence=None`` to the gateway.
        gateway_cls: Optional constructor override (defaults to ``core.model_gateway.client.ModelGateway``).
    """

    session_factory: Any
    redis: Redis
    scope: Scope
    approved_endpoint_cidrs: tuple[str, ...]
    evidence_provider: Callable[[Any, AIExecutionConfig], Any] | None = None
    gateway_cls: Callable[..., Any] | None = None

    @classmethod
    def for_settings(
        cls,
        session_factory: Any,
        redis: Redis,
        scope: Scope,
        cidrs: Iterable[str],
        **options: Any,
    ) -> ModelGatewayFactory:
        """Normalize a settings CIDR iterable into the immutable tuple the gateway expects."""
        return cls(session_factory, redis, scope, tuple(cidrs), **options)

    def _new_gateway(self, **kwargs: Any) -> ModelGateway:
        """Construct the gateway; a seam so subclasses can resolve the class from their own namespace."""
        return (self.gateway_cls or ModelGateway)(**kwargs)

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
        kwargs: dict[str, Any] = {}
        if with_evidence:
            provider = self.evidence_provider
            kwargs["evidence"] = (
                provider(self.session_factory, config) if provider is not None else None
            )
        return self._new_gateway(
            redis=self.redis,
            base_url=base_url if base_url is not None else config.gateway_base_url,
            api_key=config.gateway_api_key,
            destination_id=destination_id
            if destination_id is not None
            else (config.endpoint_destination_id or ""),
            timeout_seconds=timeout_seconds,
            scope=self.scope,
            gateway_identity=config.gateway_identity,
            model_identity=config.model_identity,
            configuration_revision=config.configuration_revision,
            before_send=before_send,
            approved_endpoint_cidrs=self.approved_endpoint_cidrs,
            allow_public_endpoints=config.allow_public_providers,
            kind=config.gateway_kind,
            **kwargs,
        )
