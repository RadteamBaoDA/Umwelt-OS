"""In-memory Graphiti stand-ins shared by the temporal adapter characterization tests.

``install_fake_graphiti`` swaps the lazily imported ``graphiti_core.edges`` and
``graphiti_core.nodes`` modules for classes backed by a :class:`FakeGraph`, and
:class:`FakeDriver` answers the exact Cypher statements the adapter issues. Every
mutation is appended to ``FakeGraph.events`` so tests can assert ordering.
"""

from __future__ import annotations

import copy
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import graphiti_core  # noqa: F401  # load the real package before its submodules are faked
import pytest

GROUP = "group_1"
DIMS = 3
NOW = datetime(2026, 1, 1, tzinfo=UTC)
VECTOR = [0.5, 0.25, 0.125]


class NodeNotFoundError(Exception):
    """Same class name Graphiti raises; the adapter matches it by name."""


class FakeGraph:
    """In-memory graph plus an ordered event log shared with the journal callback."""

    def __init__(self) -> None:
        self.edges: dict[str, Any] = {}
        self.episodes: dict[str, Any] = {}
        self.mentions: dict[str, Any] = {}
        self.entities: dict[str, Any] = {}
        self.node_links: dict[str, list[dict[str, Any]]] = {}
        self.events: list[tuple[Any, ...]] = []
        self.fail_set_write = False
        self.fail_entity_delete = False


class FakeDriver:
    """Cypher stand-in recognising the statements the temporal adapter issues."""

    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph

    async def execute_query(self, query: str, **params: Any) -> tuple[Any, None, None]:
        """Answer one statement from the in-memory graph and log mutations."""
        graph = self.graph
        if "SET edge.episodes" in query:
            graph.events.append(("write", params["fact_id"]))
            if graph.fail_set_write:
                return [], None, None
            graph.edges[params["fact_id"]].episodes = list(params["episode_ids"])
            return [{"fact_id": params["fact_id"]}], None, None
        if "[mention:MENTIONS]" in query:
            rows = [
                {"mention_id": key}
                for key, item in sorted(graph.mentions.items())
                if str(item.source_node_uuid) == params["episode_id"]
                and params["episode_id"] in graph.episodes
            ]
            return rows, None, None
        if "WHERE entity.uuid IN $entity_ids" in query:
            return (
                [row for ids in params["entity_ids"] for row in graph.node_links.get(ids, [])],
                None,
                None,
            )
        if "WHERE edge.uuid IN $edge_ids" in query:
            return (
                [{"edge_id": item} for item in params["edge_ids"] if item in graph.edges],
                None,
                None,
            )
        if "IN coalesce(edge.episodes" in query:
            rows = [
                {"fact_id": key}
                for key, edge in sorted(graph.edges.items())
                if params["episode_id"] in edge.episodes
            ]
            return rows, None, None
        if "MATCH (source)-[edge]-(entity:Entity" in query:
            return graph.node_links.get(params["entity_id"], []), None, None
        if "SET entity.name_embedding = null" in query:
            graph.events.append(("clear_embedding", params["entity_id"]))
            graph.entities[params["entity_id"]].name_embedding = None
            return [{"entity_id": params["entity_id"]}], None, None
        if "WHERE node.uuid IN $node_ids" in query:
            rows = [
                {"node_id": node_id, "group_id": GROUP}
                for node_id in params["node_ids"]
                if node_id in graph.entities
            ]
            return rows, None, None
        raise AssertionError(f"unexpected query {query}")


class _Episode(SimpleNamespace):
    """Episode copy whose delete() mutates the owning fake graph."""

    async def delete(self, driver: Any) -> None:
        """Remove the episode and its MENTIONS relationships (DETACH DELETE) and log it."""
        graph: FakeGraph = driver.graph
        graph.events.append(("delete_episode", str(self.uuid)))
        graph.episodes.pop(str(self.uuid))
        for key in [
            k for k, v in graph.mentions.items() if str(v.source_node_uuid) == str(self.uuid)
        ]:
            del graph.mentions[key]


def make_edge(fact_id: str, source: str, target: str, episodes: list[str], **extra: Any) -> Any:
    """Return a stored fake fact edge carrying an embedding so fingerprints resolve."""
    return SimpleNamespace(
        uuid=fact_id,
        group_id=GROUP,
        source_node_uuid=source,
        target_node_uuid=target,
        name="REL",
        fact="fact text",
        episodes=list(episodes),
        created_at=NOW,
        reference_time=NOW,
        valid_at=extra.get("valid_at"),
        invalid_at=extra.get("invalid_at"),
        expired_at=extra.get("expired_at"),
        fact_embedding=list(VECTOR),
        attributes={},
    )


def make_entity(entity_id: str, **extra: Any) -> Any:
    """Return a stored fake entity node."""
    return SimpleNamespace(
        uuid=entity_id,
        group_id=GROUP,
        name=extra.get("name", "n"),
        summary=extra.get("summary", "s"),
        attributes={},
        labels=["Entity"],
        created_at=NOW,
        name_embedding=extra.get("embedding", list(VECTOR)),
    )


def _build_modules(graph: FakeGraph) -> dict[str, Any]:  # noqa: C901  # test harness
    """Build fake graphiti_core.edges/nodes modules backed by ``graph``."""

    class EntityEdge(SimpleNamespace):
        """Edge value that can be constructed, saved and fetched like Graphiti's."""

        async def load_fact_embedding(self, driver: Any) -> None:
            """No-op: the fake edge already carries its vector."""

        async def save(self, driver: Any) -> None:
            """Store (replace) the edge and log the write."""
            graph.events.append(("save_fact", str(self.uuid)))
            graph.edges[str(self.uuid)] = copy.deepcopy(SimpleNamespace(**vars(self)))

        @classmethod
        async def get_by_uuids(cls, driver: Any, ids: list[str]) -> list[Any]:
            graph.events.append(("read_edges", tuple(ids)))
            return [
                cls(**vars(copy.deepcopy(graph.edges[item]))) for item in ids if item in graph.edges
            ]

        @classmethod
        async def delete_by_uuids(cls, driver: Any, ids: list[str]) -> None:
            graph.events.append(("delete_fact", tuple(ids)))
            for item in ids:
                graph.edges.pop(item, None)

    class EpisodicEdge:
        @classmethod
        async def get_by_uuids(cls, driver: Any, ids: list[str]) -> list[Any]:
            return [copy.deepcopy(graph.mentions[item]) for item in ids if item in graph.mentions]

    class EpisodicNode:
        @classmethod
        async def get_by_uuids(cls, driver: Any, ids: list[str]) -> list[Any]:
            return [
                _Episode(**vars(copy.deepcopy(graph.episodes[item])))
                for item in ids
                if item in graph.episodes
            ]

        @classmethod
        async def get_by_uuid(cls, driver: Any, uuid: str) -> Any:
            if uuid not in graph.episodes:
                raise NodeNotFoundError(uuid)
            return _Episode(**vars(copy.deepcopy(graph.episodes[uuid])))

    class EntityNode(SimpleNamespace):
        """Entity value that can be constructed, saved and fetched like Graphiti's."""

        async def load_name_embedding(self, driver: Any) -> None:
            """No-op: the fake entity already carries its vector."""

        async def save(self, driver: Any) -> None:
            """Store (replace) the node and log the write."""
            graph.events.append(("save_entity", str(self.uuid)))
            graph.entities[str(self.uuid)] = SimpleNamespace(**vars(self))

        @classmethod
        async def get_by_uuids(
            cls, driver: Any, ids: list[str], group_id: str | None = None
        ) -> list[Any]:
            return [
                cls(**vars(copy.deepcopy(graph.entities[item])))
                for item in ids
                if item in graph.entities
            ]

        @classmethod
        async def get_by_uuid(cls, driver: Any, uuid: str) -> Any:
            if uuid not in graph.entities:
                raise NodeNotFoundError(uuid)
            return cls(**vars(copy.deepcopy(graph.entities[uuid])))

        @classmethod
        async def delete_by_uuids(cls, driver: Any, ids: list[str]) -> None:
            graph.events.append(("delete_entity", tuple(ids)))
            if graph.fail_entity_delete:
                raise RuntimeError("delete failed")
            for item in ids:
                graph.entities.pop(item, None)

    return {
        "graphiti_core.edges": SimpleNamespace(EntityEdge=EntityEdge, EpisodicEdge=EpisodicEdge),
        "graphiti_core.nodes": SimpleNamespace(EntityNode=EntityNode, EpisodicNode=EpisodicNode),
    }


def install_fake_graphiti(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph) -> None:
    """Replace the lazily imported Graphiti edge/node modules with fakes bound to ``graph``."""
    for name, module in _build_modules(graph).items():
        monkeypatch.setitem(sys.modules, name, module)
