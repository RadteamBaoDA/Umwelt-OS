"""Neo4j-owned partition erasure fence shared by every scoped transaction and erasure.

The unique marker stores only an opaque partition ID and a boolean. It intentionally has
no group_id or content: group erasure must preserve this technical tombstone so delayed
clients cannot recreate erased data after PostgreSQL connection/lease loss. Setting the
property takes an exclusive Neo4j node lock through the payload transaction's commit.
"""

from collections.abc import Mapping, Sequence
from typing import Any

PARTITION_FENCE_CONSTRAINT = (
    "CREATE CONSTRAINT umwelt_partition_erasure IF NOT EXISTS "
    "FOR (f:UmweltPartitionFence) REQUIRE f.partition_id IS UNIQUE"
)
PARTITION_FENCE_SCHEMA = (
    "SHOW CONSTRAINTS YIELD type, entityType, labelsOrTypes, properties "
    "WHERE type = 'UNIQUENESS' AND entityType = 'NODE' "
    "AND labelsOrTypes = ['UmweltPartitionFence'] AND properties = ['partition_id'] "
    "RETURN count(*) AS found"
)
LOCK_PARTITION_FENCE = (
    "MERGE (f:UmweltPartitionFence {partition_id: $partition_id}) "
    "ON CREATE SET f.erased = false "
    "SET f.erased = f.erased RETURN f.erased AS erased"
)
ERASE_PARTITION_FENCE = (
    "MERGE (f:UmweltPartitionFence {partition_id: $partition_id}) "
    "SET f.erased = true RETURN f.erased AS erased"
)


def require_partition_fence_schema(rows: Sequence[Mapping[str, Any]]) -> None:
    """Reject absent/malformed proof of the actual unique partition schema.

    Named IF NOT EXISTS DDL can silently skip a conflicting constraint name; callers
    must run PARTITION_FENCE_SCHEMA after DDL and before graph admission or erasure.
    This validates metadata only and never changes a marker or grants graph access.
    """
    if len(rows) != 1 or type(rows[0].get("found")) is not int or rows[0]["found"] < 1:
        raise ValueError("Graph partition erasure uniqueness is not confirmed")
