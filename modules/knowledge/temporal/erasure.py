"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

import asyncio
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.config import Settings
from core.erasure_sql import AccountErasureOwner
from modules.knowledge.temporal.graph_fence import (
    ERASE_PARTITION_FENCE,
    PARTITION_FENCE_CONSTRAINT,
    PARTITION_FENCE_SCHEMA,
    require_partition_fence_schema,
)
from modules.knowledge.temporal.models import GraphPartition

from . import models as _models  # noqa: F401 - register this owner's tables

QUERY_SECONDS = 5

ERASURE_TABLES: tuple[str, ...] = (
    "temporal_allocations",
    "temporal_changes",
    "temporal_dispatches",
    "temporal_mappings",
    "temporal_operations",
    "temporal_partitions",
    "temporal_rebuild_dependencies",
    "temporal_receipts",
    "temporal_reconcile_members",
    "temporal_reconcile_runs",
    "temporal_supports",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)


async def erase_account_graph_page(
    ctx: dict[str, Any], scope: AccountErasureScope, cursor: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Delete one bounded graph partition page by a SQL-owned exact group ID.

    First commit the destination erasure fence. It serializes with every scoped graph
    transaction and permanently rejects delayed writers even after SQL lease loss. The
    opaque marker survives content deletion. The original partition row remains durable
    until graph absence is confirmed. SQL preparation ends before Neo4j I/O. Repeat
    the same group after each 100-node/edge page; a retry safely treats absent graph rows as done.
    Each server query is bounded and all Neo4j I/O shares a 30-second deadline. Unknown
    outcomes are retryable because a repeated fence can only remain erased.
    """
    from neo4j import AsyncGraphDatabase, Query

    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    after = UUID(cursor["after"]) if "after" in cursor else None
    async with factory() as session:
        await authorize_account_erasure(session, scope)
        query = select(GraphPartition.id).where(GraphPartition.workspace_id == scope.workspace_id)
        if after is not None:
            query = query.where(GraphPartition.id > after)
        partition_id = await session.scalar(query.order_by(GraphPartition.id).limit(1))
        await session.rollback()
    if partition_id is None:
        return True, {}
    if not settings.graph_host or not settings.graph_username:
        raise RuntimeError("Graph erasure requires graph connection settings")
    uri = f"bolt://{settings.graph_host}:{settings.graph_port}"
    async with asyncio.timeout(30), AsyncGraphDatabase.driver(
        uri, auth=(settings.graph_username, settings.graph_password.get_secret_value()),
        connection_timeout=2, connection_acquisition_timeout=QUERY_SECONDS,
        max_transaction_retry_time=0, max_connection_pool_size=1,
    ) as driver, driver.session(database=settings.graph_database) as graph:
        schema = await graph.run(Query(PARTITION_FENCE_CONSTRAINT, timeout=QUERY_SECONDS))
        await schema.consume()
        schema_proof = await graph.run(Query(PARTITION_FENCE_SCHEMA, timeout=QUERY_SECONDS))
        schema_record = await schema_proof.single(strict=True)
        require_partition_fence_schema([schema_record.data()] if schema_record is not None else [])
        fence = await graph.run(
            Query(ERASE_PARTITION_FENCE, timeout=QUERY_SECONDS), partition_id=str(partition_id),
        )
        fenced = await fence.single(strict=True)
        await fence.consume()  # Confirm auto-commit before any content deletion can start.
        if fenced is None or fenced["erased"] is not True:
            raise RuntimeError("Graph erasure fence was not confirmed")
        # Edges with stale/missing endpoints can survive a node-only deletion.
        edge = await graph.run(
            Query("MATCH ()-[r]-() WHERE r.group_id = $group_id "
                  "WITH DISTINCT r LIMIT 100 DELETE r RETURN count(r) AS removed",
                  timeout=QUERY_SECONDS),
            group_id=str(partition_id),
        )
        edge_record = await edge.single()
        if edge_record is None:
            raise RuntimeError("Graph edge deletion omitted its count")
        edge_count = edge_record["removed"]
        if edge_count:
            return False, dict(cursor)
        foreign_edges = await graph.run(
            Query("MATCH (n)-[r]-() WHERE n.group_id = $group_id "
                  "AND coalesce(r.group_id, '') <> $group_id RETURN 1 AS found LIMIT 1",
                  timeout=QUERY_SECONDS),
            group_id=str(partition_id),
        )
        if await foreign_edges.single():
            raise RuntimeError("Graph partition has a foreign relationship")
        node = await graph.run(
            Query("MATCH (n) WHERE n.group_id = $group_id "
                  "WITH n LIMIT 100 DELETE n RETURN count(n) AS removed",
                  timeout=QUERY_SECONDS),
            group_id=str(partition_id),
        )
        node_record = await node.single()
        if node_record is None:
            raise RuntimeError("Graph node deletion omitted its count")
        node_count = node_record["removed"]
        if node_count:
            return False, dict(cursor)
        # An exact group read is required before advancing past its SQL receipt.
        remaining = await graph.run(
            Query("MATCH (n) WHERE n.group_id = $group_id RETURN count(n) AS found",
                  timeout=QUERY_SECONDS),
            group_id=str(partition_id),
        )
        remaining_record = await remaining.single()
        if remaining_record is None or remaining_record["found"]:
            raise RuntimeError("Graph partition erasure not confirmed")
    return False, {"after": str(partition_id)}
