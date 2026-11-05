"""Descriptor-driven registry of outbox event consumers (core holds no module knowledge).

Modules declare, in their ``ModuleDescriptor``, which outbox event types they consume
(``event_consumers``), which producers may publish them and which handler class implements the
type-specific rules. The registry resolves those lazy references and validates the declarations:

* every handler's event type is listed in the declaring descriptor's ``consumed_events``;
* every allowed producer is a known descriptor that lists the type in ``emitted_events``;
* an event type is routed by exactly one consumer and at most one module supplies the scope runtime.

``publish_event`` and the dispatcher read only this registry, so adding a consumer means adding a
descriptor entry and a handler, not editing core.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.events.domain import DomainEvent
from core.module_descriptor import ModuleDescriptor
from core.workspaces.schemas import InternalJobScope, Scope

if TYPE_CHECKING:
    from core.events.model import EventOutbox
    from core.workspaces.schemas import AccessFence


class OutboxScopeRuntime(Protocol):
    """Scope/admission services the outbox needs from the module that owns principal resolution."""

    async def admit(
        self, session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
    ) -> AccessFence:
        """Read current admission without locks; raises the owner's authorization errors."""
        ...

    def actor_id(self, scope: Scope) -> int:
        """Return the validated principal id carried by ``scope``."""
        ...

    async def resolve_scope(
        self,
        session: AsyncSession,
        event_id: UUID,
        *,
        multi_workspace_enabled: bool,
    ) -> InternalJobScope | None:
        """Resolve the retained job scope of one stored event, ``None`` when unresolvable."""
        ...


@dataclass
class PublishContext:
    """Mutable inputs of one ``publish_event`` call shared with the type's handler.

    ``payload`` is a private deep copy that handlers may stamp in place (principal identity, Source id).
    """

    event: DomainEvent
    payload: dict[str, Any]
    identity: dict[str, Any]
    scope: Scope
    multi_workspace_enabled: bool


class EventHandler:
    """Type-specific publish, envelope and dispatch rules supplied by a consuming module.

    Subclasses override only what the event type needs. All hooks follow the caller's transaction: they
    perform non-locking reads, never commit, and raise the owner's domain errors on violations.
    """

    async def verify_publish(self, session: AsyncSession, ctx: PublishContext) -> None:
        """Prove the event against its owner state before the outbox row is built (default: accept)."""

    async def verify_outbox(
        self,
        session: AsyncSession,
        ctx: PublishContext,
        outbox: EventOutbox,
    ) -> None:
        """Prove the fully built outbox row before it is added to the session (default: accept)."""

    def valid_envelope(self, event: EventOutbox | Any, scope: InternalJobScope) -> bool:
        """Validate the stored envelope against the retained scope; ``False`` quarantines the event."""
        raise NotImplementedError

    async def validate_dispatch(
        self,
        session: AsyncSession,
        event: EventOutbox,
        scope: InternalJobScope,
        *,
        now: datetime,
        enabled: bool,
    ) -> bool:
        """Decide whether a locked, envelope-valid event may be claimed now (default: yes).

        ``False`` skips it; a handler that fails the event must set ``status = "failed"`` and commit.
        """
        return True


@dataclass(frozen=True)
class EventRoute:
    """Resolved routing of one outbox event type."""

    event_type: str
    job_name: str
    producers: frozenset[str]
    handler_target: str
    consumer_module: str


@dataclass
class EventRegistry:
    """Immutable-by-convention lookup of routes, lazily instantiated handlers and the scope runtime."""

    routes: Mapping[str, EventRoute]
    runtime_target: str
    _handlers: dict[str, EventHandler] = field(default_factory=dict)
    _runtime: OutboxScopeRuntime | None = None

    @property
    def job_names(self) -> dict[str, str]:
        """Event type -> ARQ job name (the historical ``WORKER_BY_EVENT`` table)."""
        return {route.event_type: route.job_name for route in self.routes.values()}

    @property
    def producers(self) -> dict[str, set[str]]:
        """Event type -> allowed producers (the historical ``PRODUCERS_BY_EVENT`` table)."""
        return {route.event_type: set(route.producers) for route in self.routes.values()}

    def handler(self, event_type: str) -> EventHandler:
        """Return the (cached) handler instance for a registered type.

        Raises:
            KeyError: If the type is not declared by any consumer.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = _load_target(self.routes[event_type].handler_target)()
        return self._handlers[event_type]

    def runtime(self) -> OutboxScopeRuntime:
        """Return the declared scope runtime, loading it on first use.

        Raises:
            LookupError: If no descriptor declares ``outbox_runtime``.
        """
        if self._runtime is None:
            if not self.runtime_target:
                raise LookupError("No module declares an outbox scope runtime")
            self._runtime = _load_target(self.runtime_target)
        return self._runtime


def _load_target(target: str) -> Any:
    """Import ``"package.module:attribute"`` lazily and return the attribute."""
    module_name, _, attribute = target.partition(":")
    return getattr(import_module(module_name), attribute)


def build_event_registry(descriptors: Iterable[ModuleDescriptor]) -> EventRegistry:
    """Build and validate the event registry from module descriptors.

    Raises:
        ValueError: On a duplicate event type, a consumer type missing from its descriptor's
            ``consumed_events``, a producer without a descriptor or whose descriptor does not list the type
            in ``emitted_events``, a spec without producers, or more than one declared scope runtime.
    """
    items = tuple(descriptors)
    by_id = {item.id: item for item in items}
    routes: dict[str, EventRoute] = {}
    runtimes = [item.outbox_runtime for item in items if item.outbox_runtime]
    if len(runtimes) > 1:
        raise ValueError("Only one module may declare the outbox scope runtime")
    for descriptor in items:
        for spec in descriptor.event_consumers:
            if spec.event_type in routes:
                raise ValueError(f"Event type {spec.event_type} has more than one consumer")
            if spec.event_type not in descriptor.consumed_events:
                raise ValueError(
                    f"Module {descriptor.id} handles {spec.event_type} but does not declare it in consumed_events"
                )
            if not spec.producers:
                raise ValueError(f"Event type {spec.event_type} declares no producers")
            for producer in spec.producers:
                owner = by_id.get(producer.removeprefix("modules."))
                if owner is None or not producer.startswith("modules."):
                    raise ValueError(
                        f"Producer {producer} of {spec.event_type} has no module descriptor"
                    )
                if spec.event_type not in owner.emitted_events:
                    raise ValueError(
                        f"Module {owner.id} publishes {spec.event_type} but does not declare it in emitted_events"
                    )
            routes[spec.event_type] = EventRoute(
                spec.event_type,
                spec.job_name,
                frozenset(spec.producers),
                spec.handler,
                descriptor.id,
            )
    return EventRegistry(routes=routes, runtime_target=runtimes[0] if runtimes else "")


@cache
def get_event_registry() -> EventRegistry:
    """Return the process-wide registry built from the lifecycle descriptors (validated on first call).

    The descriptor list is imported lazily because module packages import ``core.events``; call this at
    process startup so a bad declaration fails fast instead of at the first publish.
    """
    from core.modules import LIFECYCLE_DESCRIPTORS

    return build_event_registry(LIFECYCLE_DESCRIPTORS)


def validate_event_contracts() -> EventRegistry:
    """Startup hook: validate descriptor event declarations and return the registry.

    Also imports every route's handler and the scope runtime so a mistyped ``module:Attr`` target fails at boot.

    Raises:
        ValueError: See ``build_event_registry``; also when a resolved handler is not an ``EventHandler``.
        AttributeError, ImportError, LookupError: A handler/runtime target cannot be resolved.
    """
    registry = get_event_registry()
    for event_type in registry.routes:
        if not isinstance(registry.handler(event_type), EventHandler):
            raise ValueError(f"Handler for {event_type} is not an EventHandler")
    if registry.runtime_target:
        registry.runtime()
    return registry
