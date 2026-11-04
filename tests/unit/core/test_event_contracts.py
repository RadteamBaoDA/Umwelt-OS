"""Descriptor-driven outbox event contracts: registry validation, publisher coverage, consumer helpers."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.events import DomainEvent
from core.events.consumer import aggregate_key, already_delivered
from core.events.registry import build_event_registry, get_event_registry, validate_event_contracts
from core.module_descriptor import EventConsumerSpec, ModuleDescriptor
from core.modules import LIFECYCLE_DESCRIPTORS

ROOT = Path(__file__).resolve().parents[3]
HANDLER = "modules.ingestion.public:IngestionStageEventHandler"


def test_domain_event_and_outbox_api_import_from_core_events():
    """The package keeps ``DomainEvent`` importable and lazily exposes the outbox API."""
    from core import events

    assert events.DomainEvent is DomainEvent
    assert events.publish_event.__module__ == "core.events.publisher"
    assert events.EventOutbox.__tablename__ == "event_outbox"
    with pytest.raises(AttributeError):
        events.missing_name  # noqa: B018


def test_ingestion_compatibility_reexports_point_at_core():
    """Ingestion keeps thin re-exports for the moved outbox model, publisher and dispatcher."""
    from core.events.dispatch import dispatch_pending_work
    from core.events.model import EventOutbox
    from core.events.publisher import publish_event
    from modules.ingestion import dispatcher, public
    from modules.ingestion.models import EventOutbox as LegacyOutbox

    assert LegacyOutbox is EventOutbox
    assert public.publish_event is publish_event
    assert dispatcher.dispatch_pending_work is dispatch_pending_work


def test_registry_reproduces_the_historical_routing_tables():
    """Job names and producers derived from descriptors equal the former hard-coded dispatcher tables."""
    registry = validate_event_contracts()
    assert registry.job_names == {
        "document.file.uploaded": "process_uploaded_file",
        "document.cleanup.requested": "process_document_cleanup",
        "source.purge.requested": "process_source_purge",
        "source.purge.progressed": "process_source_purge",
        "source.purge.coverage": "process_source_memory_coverage",
        "ingestion.stage.requested": "process_ingestion_event",
        "connector.crawl.requested": "process_ingestion_event",
        "ingestion.normalize.requested": "process_normalize_event",
        "document.version.ready": "process_document_ready",
        "news.document.ready": "process_news_document_ready",
    }
    assert registry.producers["document.version.ready"] == {"modules.ingestion", "modules.knowledge.documents"}
    assert registry.producers["connector.crawl.requested"] == {"modules.connectors"}


def test_every_handler_is_declared_consumed_and_every_producer_declares_emission():
    """Descriptors are consistent: handlers in ``consumed_events``, producers list the type in ``emitted_events``."""
    by_id = {item.id: item for item in LIFECYCLE_DESCRIPTORS}
    for descriptor in LIFECYCLE_DESCRIPTORS:
        for spec in descriptor.event_consumers:
            assert spec.event_type in descriptor.consumed_events
            for producer in spec.producers:
                assert spec.event_type in by_id[producer.removeprefix("modules.")].emitted_events


def _descriptor(**overrides) -> ModuleDescriptor:
    return replace(ModuleDescriptor(id="a"), **overrides)


def test_consumer_missing_from_consumed_events_is_rejected():
    """A handler whose type is not in ``consumed_events`` fails validation."""
    spec = EventConsumerSpec("x.y", "job", ("modules.a",), HANDLER)
    with pytest.raises(ValueError, match="consumed_events"):
        build_event_registry([_descriptor(event_consumers=(spec,), emitted_events=("x.y",))])


def test_producer_missing_from_emitted_events_is_rejected():
    """A producer that does not declare the type in ``emitted_events`` fails validation."""
    spec = EventConsumerSpec("x.y", "job", ("modules.a",), HANDLER)
    with pytest.raises(ValueError, match="emitted_events"):
        build_event_registry([_descriptor(event_consumers=(spec,), consumed_events=("x.y",))])


def test_unknown_producer_duplicate_type_and_second_runtime_are_rejected():
    """Unknown producers, duplicate consumers and two scope runtimes are configuration errors."""
    ok = EventConsumerSpec("x.y", "job", ("modules.a",), HANDLER)
    base = {"consumed_events": ("x.y",), "emitted_events": ("x.y",)}
    with pytest.raises(ValueError, match="no module descriptor"):
        build_event_registry([_descriptor(**base, event_consumers=(replace(ok, producers=("modules.zzz",)),))])
    with pytest.raises(ValueError, match="more than one consumer"):
        build_event_registry([_descriptor(**base, event_consumers=(ok, ok))])
    with pytest.raises(ValueError, match="Only one module"):
        build_event_registry([_descriptor(outbox_runtime="a:b"), replace(ModuleDescriptor(id="b"), outbox_runtime="a:b")])
    with pytest.raises(ValueError, match="no producers"):
        build_event_registry([_descriptor(**base, event_consumers=(replace(ok, producers=()),))])


def _published_literals() -> list[tuple[str, str, str]]:
    """Collect ``(file, type, producer)`` of every ``DomainEvent(...)`` call whose type/producer are literals."""
    found: list[tuple[str, str, str]] = []
    for base in ("modules", "core", "apps"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "DomainEvent":
                    kw = {k.arg: k.value for k in node.keywords}
                    kind, producer = kw.get("type"), kw.get("producer")
                    if isinstance(kind, ast.Constant) and isinstance(producer, ast.Constant):
                        found.append((str(path.relative_to(ROOT)), kind.value, producer.value))
    return found


def test_every_published_event_type_is_declared_by_its_publisher():
    """Each literal ``DomainEvent`` publication is routed and its producer may emit that type."""
    registry = get_event_registry()
    by_id = {item.id: item for item in LIFECYCLE_DESCRIPTORS}
    published = _published_literals()
    assert len(published) >= 6  # guard against the scanner silently matching nothing
    for file, kind, producer in published:
        assert kind in registry.routes, f"{file}: {kind} has no consumer in any descriptor"
        assert producer in registry.routes[kind].producers, f"{file}: {producer} may not publish {kind}"
        assert kind in by_id[producer.removeprefix("modules.")].emitted_events


@pytest.mark.asyncio
async def test_publish_event_rejects_undeclared_types():
    """An event type no descriptor declares is refused before any database access."""
    from datetime import UTC, datetime

    from core.events.publisher import publish_event
    from core.workspaces.schemas import InternalJobScope

    event = DomainEvent(id=uuid4(), type="nope.nothing", version=1, occurred_at=datetime.now(UTC),
                        producer="modules.ingestion", payload={})
    scope = InternalJobScope(workspace_id=uuid4(), actor_user_id=1, membership_revision=1)
    with pytest.raises(ValueError, match="not declared"):
        await publish_event(SimpleNamespace(), event, scope=scope, multi_workspace_enabled=False)


def test_aggregate_key_uses_workspace_and_source():
    """The ordering aggregate is the workspace plus the payload Source (empty when absent)."""
    workspace, source = uuid4(), uuid4()
    with_source = SimpleNamespace(workspace_id=workspace, payload={"source_id": str(source)})
    assert aggregate_key(with_source) == (str(workspace), str(source))
    assert aggregate_key(SimpleNamespace(workspace_id=workspace, payload=None)) == (str(workspace), "")


@pytest.mark.asyncio
async def test_already_delivered_reads_the_outbox_status():
    """Only a ``delivered`` row counts as processed; a missing row does not."""
    class Session:
        def __init__(self, status):
            self.status = status

        async def scalar(self, *_a, **_k):
            return self.status

    assert await already_delivered(Session("delivered"), uuid4()) is True
    assert await already_delivered(Session("queued"), uuid4()) is False
    assert await already_delivered(Session(None), uuid4()) is False
