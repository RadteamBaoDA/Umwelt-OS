"""Journaled-dispatch Neo4j driver wrapper and dispatch scope for the temporal graph adapter."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from modules.knowledge.temporal._fingerprints import _safe_error, _strip_nul_bytes, _to_native
from modules.knowledge.temporal._types import (
    _UUID,
    MAX_CAPTURE_BYTES,
    MAX_SUPPORT,
    QUERY_SECONDS,
    DispatchOwnership,
    GraphOperationError,
    GraphOperationUnknown,
)
from modules.knowledge.temporal.graph_fence import (
    LOCK_PARTITION_FENCE,
)

logger = logging.getLogger(__name__)


_dispatch_scope: ContextVar[Any | None] = ContextVar("temporal_dispatch_scope", default=None)


_HEALTH_QUERY = "RETURN 1 AS healthy"
_SETUP_PREFIXES = ("CREATE INDEX", "CREATE FULLTEXT INDEX")
_SETUP_METADATA = {"umwelt_dispatch": "setup"}
_CONTROL_METADATA = {"umwelt_dispatch": "control"}


@dataclass
class _DispatchScope:
    """Per-operation graph dispatch state shared by every query of one journaled scope.

    The token (server_run_id nonce + client_id) is stamped into the metadata of every
    transaction, so cessation can find and terminate exactly this scope's transactions.
    poisoned means a write outcome became ambiguous or the scope closed; no later
    command may use the scope. pending counts in-flight transactions.
    """

    ownership: DispatchOwnership
    poisoned: bool = False
    pending: int = 0

    @property
    def token(self) -> str:
        """Return the unguessable per-dispatch marker persisted in the ownership journal."""
        return f"{self.ownership.server_run_id}:{self.ownership.client_id}"

    @property
    def metadata(self) -> dict[str, str]:
        """Return transaction metadata that tags server-side transactions with this dispatch."""
        return {"umwelt_dispatch": self.token, "operation_id": str(self.ownership.operation_id)}


def _positive_server_duration(value: Any) -> bool:
    """Report whether a Neo4j duration setting such as '5s' is a positive (enabled) timeout."""
    return isinstance(value, str) and not re.fullmatch(r"\s*0+(\.0+)?\s*[a-zA-Zμ]*\s*", value)


def _check_bulk_call_shapes(
    calls: Sequence[tuple[Any, dict[str, Any]]],
    args: tuple[Any, ...],
    provider: Any,
) -> None:
    """Require the pinned four statements (episode, node, mention, fact) with their exact param keys."""
    from graphiti_core.models.edges.edge_db_queries import (
        get_entity_edge_save_bulk_query,
        get_episodic_edge_save_bulk_query,
    )
    from graphiti_core.models.nodes.node_db_queries import get_episode_node_save_bulk_query

    (
        (episode_query, episode_params),
        (node_query, node_params),
        (mention_query, mention_params),
        (fact_query, fact_params),
    ) = calls
    if (
        episode_query != get_episode_node_save_bulk_query(provider)
        or not isinstance(episode_params, dict)
        or set(episode_params) != {"episodes"}
        or not isinstance(episode_params["episodes"], list)
        or len(episode_params["episodes"]) != len(args[0])
        or not isinstance(node_query, str)
        or not isinstance(node_params, dict)
        or set(node_params) != {"nodes"}
        or not isinstance(node_params["nodes"], list)
        or not isinstance(mention_query, str)
        or mention_query != get_episodic_edge_save_bulk_query(provider)
        or not isinstance(mention_params, dict)
        or set(mention_params) != {"episodic_edges"}
        or not isinstance(mention_params["episodic_edges"], list)
        or not isinstance(fact_query, str)
        or fact_query != get_entity_edge_save_bulk_query(provider)
        or not isinstance(fact_params, dict)
        or set(fact_params) != {"entity_edges"}
        or not isinstance(fact_params["entity_edges"], list)
    ):
        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")


def _check_bulk_node_payloads(
    node_query: Any,
    node_payloads: list[Any],
    expected_node_ids: set[str],
    provider: Any,
) -> None:
    """Require the node statement to be Graphiti's generated text and payload ids/labels to match exactly."""
    from graphiti_core.models.nodes.node_db_queries import get_entity_node_save_bulk_query

    # The node statement is text derived from payload labels; recomputing it proves no
    # label expression outside Graphiti's validated generator is being dispatched.
    if node_query != get_entity_node_save_bulk_query(provider, node_payloads):
        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
    ids = [str(item.get("uuid")) for item in node_payloads if isinstance(item, dict)]
    if (
        len(ids) != len(node_payloads)
        or len(ids) != len(set(ids))
        or any(not _UUID.fullmatch(value) for value in ids)
        or set(ids) != expected_node_ids
        or any(
            not isinstance(item.get("labels"), list) or "Entity" not in item["labels"]
            for item in node_payloads
        )
    ):
        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")


def _check_bulk_payload_sets(
    args: tuple[Any, ...],
    params: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    expected_node_ids: set[str],
) -> None:
    """Require bounded, unique, same-group episode/mention/fact payloads matching the argument ids."""
    episode_params, mention_params, fact_params = params
    expected_episode_ids = {str(item.uuid) for item in args[0]}
    expected_mention_ids = {str(item.uuid) for item in args[1]}
    expected_fact_ids = {str(item.uuid) for item in args[3]}
    if (
        len(expected_node_ids | expected_episode_ids | expected_mention_ids | expected_fact_ids)
        > MAX_SUPPORT
    ):
        raise GraphOperationError("graph_bulk_capture_limit")
    for payload_key, values in (
        ("episodes", episode_params["episodes"]),
        ("episodic_edges", mention_params["episodic_edges"]),
        ("entity_edges", fact_params["entity_edges"]),
    ):
        if (
            len(values) > MAX_SUPPORT
            or any(not isinstance(item, dict) for item in values)
            or len({str(item.get("uuid")) for item in values}) != len(values)
            or any(not _UUID.fullmatch(str(item.get("uuid"))) for item in values)
            or any(item.get("group_id") != args[0][0].group_id for item in values)
            or {str(item.get("uuid")) for item in values}
            != (
                expected_episode_ids
                if payload_key == "episodes"
                else expected_mention_ids
                if payload_key == "episodic_edges"
                else expected_fact_ids
            )
        ):
            raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")


def _check_bulk_edge_endpoints(
    args: tuple[Any, ...],
    mention_params: dict[str, Any],
    fact_params: dict[str, Any],
) -> None:
    """Require each mention/fact payload to keep the endpoints (and fact support) of its Graphiti edge."""
    for data, edge in zip(mention_params["episodic_edges"], args[1], strict=True):
        if str(data.get("source_node_uuid")) != str(edge.source_node_uuid) or str(
            data.get("target_node_uuid")
        ) != str(edge.target_node_uuid):
            raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
    for data, edge in zip(fact_params["entity_edges"], args[3], strict=True):
        if (
            str(data.get("source_node_uuid")) != str(edge.source_node_uuid)
            or str(data.get("target_node_uuid")) != str(edge.target_node_uuid)
            or tuple(sorted(str(value) for value in data.get("episodes", ())))
            != tuple(sorted(str(value) for value in edge.episodes))
        ):
            raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")


def _validated_bulk_payloads(
    calls: Sequence[tuple[Any, dict[str, Any]]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the pinned four-call Neo4j shape and return its final hydrated node payloads."""
    from graphiti_core.driver.driver import GraphProvider

    driver = kwargs["driver"]
    if (
        driver.provider != GraphProvider.NEO4J
        or getattr(driver, "graph_operations_interface", None) is not None
        or len(calls) != 4
        or len(args) != 5
    ):
        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
    provider = driver.provider
    _check_bulk_call_shapes(calls, args, provider)
    (_, episode_params), (node_query, node_params), (_, mention_params), (_, fact_params) = calls
    node_payloads = node_params["nodes"]
    expected_node_ids = {str(item.uuid) for item in args[2]}
    _check_bulk_node_payloads(node_query, node_payloads, expected_node_ids, provider)
    _check_bulk_payload_sets(args, (episode_params, mention_params, fact_params), expected_node_ids)
    if any(not isinstance(item, dict) for item in node_payloads):
        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
    if any(item.get("group_id") != args[0][0].group_id for item in node_payloads):
        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
    _check_bulk_edge_endpoints(args, mention_params, fact_params)
    return node_payloads  # type: ignore[no-any-return]


async def _dispatch_bulk_write(
    session: Any,
    func: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Journal exact hydrated state, then replay the captured bulk calls in ONE write transaction.

    Ordering: ID/prior receipt -> run the pinned writer into ``session._bulk_capture`` ->
    validate the four captured calls -> final hydrated receipt -> one atomic transaction.
    The capture is always cleared on exit.
    """
    from graphiti_core.driver.driver import GraphProvider

    hook = getattr(session.driver, "_write_intent_hook", None)
    if hook is None:
        raise GraphOperationError("graph_write_intent_unavailable")
    if len(args) != 5 or "driver" not in kwargs or session._bulk_capture is not None:
        raise GraphOperationError("graph_write_intent_arguments_invalid")
    bulk_driver = kwargs["driver"]
    if (
        bulk_driver.provider != GraphProvider.NEO4J
        or getattr(bulk_driver, "graph_operations_interface", None) is not None
    ):
        raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
    # Initial receipt proves the full ID/prior inventory; hydration vectors are not
    # available until the pinned writer has materialized all four structured calls.
    await hook(args[0], args[1], args[2], args[3], kwargs["driver"])
    session._bulk_capture = []
    session._bulk_capture_size = 0
    try:
        result = await func(session, *args, **kwargs)
        calls = session._bulk_capture
        if calls is None:
            raise GraphOperationError("graph_bulk_dispatch_shape_unsupported")
        nodes = _validated_bulk_payloads(calls, args, kwargs)
        await hook(
            args[0],
            args[1],
            args[2],
            args[3],
            kwargs["driver"],
            final_node_payloads=nodes,
        )
        # No Graphiti bulk write reaches Neo4j before the final-vector receipt commits.
        session._bulk_capture = None
        # One explicit transaction: either all four statements commit or none do.
        await session.driver.execute_statements(
            [(str(query), dict(params)) for query, params in calls]
        )
        return result
    finally:
        session._bulk_capture = None
        session._bulk_capture_size = 0


async def _capture_or_run(session: Any, query: Any, kwargs: dict[str, Any]) -> None:
    """Capture bounded normalized bulk calls or dispatch one statement through the protected driver."""
    if isinstance(query, list):
        raise GraphOperationError("graph_dispatch_command_denied")
    # Datetimes are deliberately kept native: the driver sends tz-aware values as temporal types.
    normalized_params = _strip_nul_bytes(dict(kwargs))
    if session._bulk_capture is not None:
        serialized = json.dumps(
            {"query": query, "params": normalized_params},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        if (
            len(session._bulk_capture) >= 4
            or session._bulk_capture_size + len(serialized) > MAX_CAPTURE_BYTES
        ):
            raise GraphOperationError("graph_bulk_capture_limit")
        session._bulk_capture.append((query, copy.deepcopy(normalized_params)))
        session._bulk_capture_size += len(serialized)
        return
    await session.driver.execute_statements([(str(query), normalized_params)])


async def _lock_partition_fence(tx: Any, scope: _DispatchScope) -> None:
    """Lock and read the permanent partition erasure fence in the caller's transaction.

    Raises ``GraphOperationError("graph_partition_erased")`` unless exactly one
    non-erased fence row exists, so no payload statement runs after a tombstone.
    """
    guard = await tx.run(LOCK_PARTITION_FENCE, {"partition_id": scope.ownership.group_id})
    guarded = []
    async for record in guard:
        guarded.append(record.data())
        if len(guarded) > 1:
            break
    if len(guarded) != 1 or guarded[0].get("erased") is not False:
        raise GraphOperationError("graph_partition_erased")


async def _run_statements(
    tx: Any,
    statements: Sequence[tuple[str, dict[str, Any]]],
) -> list[tuple[list[dict[str, Any]], list[str]]]:
    """Run each statement in the open transaction, capping rows at MAX_SUPPORT + 1 (abort beyond)."""
    outputs: list[tuple[list[dict[str, Any]], list[str]]] = []
    for query, params in statements:
        result = await tx.run(query, params)
        rows: list[dict[str, Any]] = []
        async for record in result:
            rows.append(_to_native(record.data()))
            if len(rows) > MAX_SUPPORT + 1:
                raise GraphOperationError("graph_query_result_limit")
        outputs.append((rows, list(result.keys())))
    return outputs


async def _transact_tagged(
    driver: Any,
    statements: Sequence[tuple[str, dict[str, Any]]],
    *,
    read: bool,
    metadata: dict[str, str],
    scope: _DispatchScope | None,
) -> list[tuple[list[dict[str, Any]], list[str]]]:
    """Run statements in ONE explicit tagged transaction and return (rows, keys) for each.

    Scoped requests first lock and check the permanent partition erasure fence in
    this same transaction. Even read routing uses WRITE access for this guard;
    routing hints cannot bypass erasure. No payload runs after a tombstone.
    Never uses driver.execute_query/execute_read/execute_write because they retry.
    Row count above MAX_SUPPORT + 1 aborts and rolls back. A write that fails after the
    commit was sent, is cancelled or times out has an unknown outcome: the scope is
    poisoned so no later command runs under it, and GraphOperationUnknown is raised.
    """
    from neo4j import READ_ACCESS, WRITE_ACCESS

    if not metadata.get("umwelt_dispatch"):
        # Untagged transactions could not be found and terminated by cessation.
        raise GraphOperationError("graph_dispatch_command_denied")
    if scope is not None:
        if scope.poisoned:
            raise GraphOperationUnknown("graph_dispatch_connection_lost")
        scope.pending += 1
    commit_sent = False
    try:
        async with asyncio.timeout(QUERY_SECONDS):
            async with driver.client.session(
                database=driver._database,
                default_access_mode=READ_ACCESS if read and scope is None else WRITE_ACCESS,
            ) as session:
                tx = await session.begin_transaction(metadata=metadata, timeout=QUERY_SECONDS)
                try:
                    if scope is not None:
                        await _lock_partition_fence(tx, scope)
                    outputs = await _run_statements(tx, statements)
                    commit_sent = True
                    await tx.commit()
                except BaseException:
                    if not commit_sent:
                        try:
                            await tx.rollback()
                        except Exception:  # noqa: BLE001  # best effort: server-side timeout reaps it
                            logger.debug("Graph rollback failed")
                    raise
        return outputs
    except GraphOperationError:
        raise
    except BaseException as exc:
        if scope is not None and (commit_sent or not read):
            scope.poisoned = True
        if isinstance(exc, Exception):
            raise _safe_error(exc) from None
        raise
    finally:
        if scope is not None:
            scope.pending -= 1


def _resolve_query_metadata(cypher_query: str, scope: _DispatchScope | None) -> dict[str, str]:
    """Pick the transaction tag: the scope's token, or a fixed setup tag for the health probe and index DDL."""
    if scope is not None:
        return scope.metadata
    if cypher_query == _HEALTH_QUERY or cypher_query.lstrip().startswith(_SETUP_PREFIXES):
        return _SETUP_METADATA
    raise GraphOperationError("graph_dispatch_ownership_required")


def _make_session_type() -> type[Any]:
    """Build the ``SafeSession`` class (needs Graphiti's session base, so it is created lazily).

    The session captures Graphiti's bulk write calls and delegates validation and
    dispatch to the module-level helpers above.
    """
    from graphiti_core.driver.driver import GraphDriverSession, GraphProvider

    class SafeSession(GraphDriverSession):
        """Graphiti session facade that captures/journals bulk writes and dispatches them in one transaction."""

        provider = GraphProvider.NEO4J

        def __init__(self, driver: Any) -> None:
            """Bind the protected driver (carrying the request-scoped prewrite hook)."""
            self.driver = driver
            self._bulk_capture: list[tuple[Any, dict[str, Any]]] | None = None
            self._bulk_capture_size = 0

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            """Release the session; there is no pooled resource held between calls."""
            await self.close()

        async def close(self) -> None:
            """Drop any uncommitted capture; transactions are opened and closed per call."""
            self._bulk_capture = None
            self._bulk_capture_size = 0

        def _validated_bulk_payloads(
            self,
            calls: Sequence[tuple[Any, dict[str, Any]]],
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> list[dict[str, Any]]:
            """Validate the pinned four-call Neo4j shape and return its final hydrated node payloads."""
            return _validated_bulk_payloads(calls, args, kwargs)

        async def execute_write(
            self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
        ) -> Any:
            """Journal exact hydrated state and replay captured bulk calls atomically; pass other writes through."""
            from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk_tx

            if func is add_nodes_and_edges_bulk_tx:
                return await _dispatch_bulk_write(self, func, args, kwargs)
            return await func(self, *args, **kwargs)

        async def run(self, query: str | list[Any], **kwargs: Any) -> Any:
            """Capture bounded normalized bulk calls or dispatch one statement through the protected driver."""
            await _capture_or_run(self, query, kwargs)

    return SafeSession


def _replace_with_hardened_client(
    driver: Any,
    uri: str,
    user: str | None,
    password: str | None,
) -> None:
    """Discard the upstream default client/index task and install the bounded, non-retrying client.

    The upstream constructor opens a default-config client (and may schedule an index task
    when a loop is running); both are discarded so only the hardened client exists. Index
    setup runs once from TemporalGraph.initialize instead.
    """
    from neo4j import AsyncGraphDatabase

    default_client = driver.client
    if driver._init_task is not None:
        driver._init_task.cancel()
        driver._init_task = None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Worker thread: nothing is connected yet, so closing only marks it closed.
        asyncio.run(default_client.close())
    driver.client = AsyncGraphDatabase.driver(
        uri,
        auth=(user or "", password or ""),
        max_connection_pool_size=4,
        connection_timeout=2,
        connection_acquisition_timeout=QUERY_SECONDS,
        # Retrying a write transaction function could double-apply an unknown outcome.
        max_transaction_retry_time=0,
        fetch_size=MAX_SUPPORT + 2,
    )


def _make_driver_type() -> type[Any]:
    """Load Graphiti after telemetry is disabled and wrap every Neo4j query path.

    The returned ``SafeDriver`` subclasses Graphiti's Neo4j driver; the heavy logic
    lives in the module-level helpers above so each piece stays small and testable.
    """
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    session_type = _make_session_type()

    class SafeDriver(Neo4jDriver):
        """Neo4j driver whose every statement is one bounded, tagged, non-retried explicit transaction."""

        def __init__(
            self, uri: str, user: str | None, password: str | None, database: str = "neo4j"
        ) -> None:
            """Build the client with bounded pool/timeouts and no automatic retries or index task."""
            super().__init__(uri, user, password, database)
            _replace_with_hardened_client(self, uri, user, password)

        async def _transact(
            self,
            statements: Sequence[tuple[str, dict[str, Any]]],
            *,
            read: bool,
            metadata: dict[str, str],
            scope: _DispatchScope | None,
        ) -> list[tuple[list[dict[str, Any]], list[str]]]:
            """Run statements in ONE explicit tagged transaction (see ``_transact_tagged``)."""
            return await _transact_tagged(
                self, statements, read=read, metadata=metadata, scope=scope
            )

        async def execute_statements(
            self, statements: Sequence[tuple[str, dict[str, Any]]]
        ) -> None:
            """Write statements atomically in one transaction; requires a dispatch scope."""
            scope = _dispatch_scope.get()
            if scope is None:
                raise GraphOperationError("graph_dispatch_ownership_required")
            await self._transact(statements, read=False, metadata=scope.metadata, scope=scope)

        async def control_query(
            self, query: str, params: dict[str, Any], *, read: bool
        ) -> list[dict[str, Any]]:
            """Run an adapter-owned management/probe statement outside any dispatch scope.

            Used for server-setting probes and cessation SHOW/TERMINATE only; carries a
            fixed control tag so it can never match an operation's dispatch token.
            """
            results = await self._transact(
                [(query, params)], read=read, metadata=_CONTROL_METADATA, scope=None
            )
            return results[0][0]

        async def execute_query(self, cypher_query_: str, **kwargs: Any) -> Any:
            """Run one Graphiti statement as a guarded transaction and return (rows, None, keys).

            Requires an active dispatch scope except for the health probe and index DDL.
            """
            params = dict(kwargs.pop("params", None) or {})
            routing = kwargs.pop("routing_", None)
            database = kwargs.pop("database_", None)
            if database not in (None, self._database):
                raise GraphOperationError("graph_dispatch_partition_mismatch")
            params.update(kwargs)
            read = getattr(routing, "value", routing) == "r"
            scope = _dispatch_scope.get()
            metadata = _resolve_query_metadata(cypher_query_, scope)
            try:
                results = await self._transact(
                    [(cypher_query_, _strip_nul_bytes(params))],
                    read=read,
                    metadata=metadata,
                    scope=scope,
                )
            except GraphOperationError as exc:
                if str(exc) == "graph_index_exists":
                    return None
                raise
            rows, keys = results[0]
            return rows, None, keys

        def session(self, database: str | None = None) -> Any:
            """Return a protected session retaining the request-scoped prewrite hook."""
            return session_type(self)

        async def health_check(self) -> None:
            """Run one connectivity query without printing upstream exceptions."""
            await self.execute_query(_HEALTH_QUERY, routing_="r")

        async def close(self) -> None:
            """Await the owning client close and mark it closed only on success.

            Lifecycle callers shield and join this coroutine under heavy ownership;
            no local timeout cancels upstream cleanup or masks a retryable failure.
            """
            if not getattr(self, "_transport_closed", False):
                await self.client.close()
                self._transport_closed = True

    return SafeDriver
