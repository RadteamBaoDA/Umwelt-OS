"""Domain-event contracts and the transactional outbox (publisher, dispatcher, registry, replay).

``DomainEvent`` is imported eagerly (it is a dependency-free pydantic model used everywhere). The outbox API
is re-exported lazily so importing ``core.events`` for the event model never pulls the database layer or the
module descriptors (which themselves import ``core.events``).
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from core.events.domain import DomainEvent

if TYPE_CHECKING:
    from core.events.dispatch import dispatch_pending_work as dispatch_pending_work
    from core.events.model import EventOutbox as EventOutbox
    from core.events.publisher import publish_event as publish_event
    from core.events.registry import get_event_registry as get_event_registry
    from core.events.registry import validate_event_contracts as validate_event_contracts

_LAZY = {
    "EventOutbox": "core.events.model",
    "publish_event": "core.events.publisher",
    "dispatch_pending_work": "core.events.dispatch",
    "get_event_registry": "core.events.registry",
    "validate_event_contracts": "core.events.registry",
}

__all__ = [
    "DomainEvent",
    "EventOutbox",
    "dispatch_pending_work",
    "get_event_registry",
    "publish_event",
    "validate_event_contracts",
]


def __getattr__(name: str) -> Any:
    """Resolve the lazily exported outbox names on first access (PEP 562)."""
    if name in _LAZY:
        return getattr(import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'core.events' has no attribute {name!r}")
