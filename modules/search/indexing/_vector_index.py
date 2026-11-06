"""HNSW vector-index build (outside a transaction on the direct engine), generation activation and version purge."""

import logging
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from core.database import direct_engine
from core.workspaces.schemas import Scope
from modules.knowledge.documents import public as documents
from modules.search.indexing._common import MAX_VECTOR_DIMENSIONS
from modules.search.models import IndexGeneration

logger = logging.getLogger("modules.search.indexing")


def vector_index_name(generation_id: UUID) -> str:
    """Return the per-generation partial HNSW index name (derived from a UUID, never request text)."""
    return f"ix_search_vector_{generation_id.hex}"


def vector_index_statements(generation_id: UUID, dimensions: int) -> tuple[str, str]:
    """Return ``(drop_invalid_leftover, create_concurrently)`` SQL for one generation's HNSW index.

    Identifier and dimensions originate from a UUID and a bounded integer (validated here), never from
    request text, so f-string interpolation is safe. The partial predicate matches ``_vector_ids`` so the
    planner can prove it.
    """
    if not 1 <= dimensions <= MAX_VECTOR_DIMENSIONS:
        raise ValueError("Invalid generation dimensions")
    name = vector_index_name(generation_id)
    drop = f"DROP INDEX CONCURRENTLY IF EXISTS {name}"
    create = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON search_index_items "
        f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
        f"WHERE generation_id = '{generation_id}' AND status = 'succeeded'"
    )
    return drop, create


# Upper bound for one concurrent HNSW build, including its wait for older transactions to finish.
HNSW_BUILD_STATEMENT_TIMEOUT_MS = 2 * 60 * 60 * 1000


async def build_vector_index_concurrently(
    engine: AsyncEngine, generation_id: UUID, dimensions: int
) -> None:
    """Build one generation's HNSW index with ``CREATE INDEX CONCURRENTLY`` on its own autocommit connection.

    Takes no lock that blocks writes to ``search_index_items`` (the previous in-transaction build did) but must
    not be called while the caller's own transaction holds writes on that table, because the concurrent build
    waits for them. The statement timeout is raised to ``HNSW_BUILD_STATEMENT_TIMEOUT_MS`` (finite, 2 h) and the lock timeout lifted for the build and reset afterwards. An *invalid*
    index left by an earlier interrupted build is dropped first (``IF NOT EXISTS`` would silently keep it),
    and a build that fails here drops its own invalid leftover before re-raising so a retry starts clean.

    The connection comes from ``core.database.direct_engine(engine)``: the build holds a session-level advisory
    lock and session ``SET``/``RESET`` across several autocommit statements, which a transaction pooler would
    hand to other clients between statements. In PgBouncer mode that is the attached direct (unpooled) engine;
    in direct mode it is ``engine`` itself.
    """
    drop, create = vector_index_statements(generation_id, dimensions)
    name = vector_index_name(generation_id)
    async with direct_engine(engine).connect() as raw:
        connection = await raw.execution_options(isolation_level="AUTOCOMMIT")
        locked = False
        try:
            # Finite on purpose (was 0 = unbounded): the build waits for older open transactions, so an
            # idle-in-transaction session would otherwise hang it forever. On timeout the invalid leftover is
            # dropped below and the next indexing pass retries. See docs/operations.md "Search HNSW indexes".
            await connection.execute(
                text(f"SET statement_timeout = {HNSW_BUILD_STATEMENT_TIMEOUT_MS}")
            )
            await connection.execute(text("SET lock_timeout = 0"))
            # Session-level advisory lock keyed by the index name serializes builders of the same generation
            # (the generation row lock is released before the build). Without it a second finalize would see the
            # first builder's in-progress (indisvalid = false) index as a leftover and drop it. Blocking is
            # intended: after the first builder finishes, the probe below finds a valid index and the create is a no-op.
            await connection.execute(
                text("SELECT pg_advisory_lock(hashtext(:name))"), {"name": name}
            )
            locked = True
            invalid = (
                await connection.execute(
                    text(
                        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "WHERE c.relname = :name AND NOT i.indisvalid"
                    ),
                    {"name": name},
                )
            ).first()
            if invalid is not None:
                await connection.execute(text(drop))
            try:
                await connection.execute(text(create))
            except BaseException:
                # A failed/cancelled concurrent build leaves an INVALID index behind; remove it best-effort
                # (we still hold the advisory lock, so no other builder's index can be dropped here).
                try:
                    await connection.execute(text(drop))
                except Exception:  # cleanup must not mask the build failure
                    logger.warning(
                        "Could not drop invalid HNSW index %s after a failed build",
                        name,
                        exc_info=True,
                    )
                raise
        finally:
            # Pooled connection: release the session lock and timeouts, but never let a broken/cancelled
            # connection mask the original error; discard it instead so the pool cannot reuse dirty state.
            try:
                if locked:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": name}
                    )
                await connection.execute(text("RESET statement_timeout"))
                await connection.execute(text("RESET lock_timeout"))
            except BaseException:
                logger.warning(
                    "Could not reset HNSW build connection state; invalidating it", exc_info=True
                )
                try:
                    await connection.invalidate()
                except Exception:
                    logger.debug("Invalidate after failed reset also failed", exc_info=True)


def async_engine_for_session(session: AsyncSession) -> AsyncEngine:
    """Return the ``AsyncEngine`` behind ``session`` (bound directly or through an ``AsyncConnection``).

    ``AsyncEngine.engine`` is the *sync* engine, so it must never be read off an engine bind.

    Raises:
        TypeError: the session is not bound to an async engine/connection.
    """
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    raise TypeError("Concurrent index build requires an AsyncEngine-bound session")


async def activate_generation(
    session: AsyncSession,
    generation: IndexGeneration,
    *,
    scope: Scope,
) -> None:
    """Retire only the admitted workspace's active predecessor and mark ``generation`` active.

    The generation's HNSW index must already exist: it is built before this call by
    ``build_vector_index_concurrently`` (``CREATE INDEX CONCURRENTLY`` cannot run inside this transaction
    and a transactional build would block writes to ``search_index_items``). Locks the predecessor row
    ``FOR UPDATE`` inside the caller's transaction; the caller commits.
    """
    if generation.dimensions is None or not 1 <= generation.dimensions <= MAX_VECTOR_DIMENSIONS:
        raise ValueError("Invalid generation dimensions")
    prior = await session.scalar(
        select(IndexGeneration)
        .where(
            IndexGeneration.workspace_id == scope.workspace_id,
            IndexGeneration.status == "active",
            IndexGeneration.id != generation.id,
        )
        .with_for_update()
    )
    if prior is not None:
        prior.status = "retired"
        await session.flush()
    generation.status = "active"
    generation.error_code = None


async def purge_version_embeddings(session: AsyncSession, document_version_id: UUID) -> None:
    """Delete every generation's index items for the chunks of one (superseded) document version.

    Runs in the caller's transaction and takes no locks beyond the deletes. Scope is exactly the
    chunks of ``document_version_id`` (served by the ``search_index_items(chunk_id)`` index), so
    other versions, documents and workspaces are untouched; the version id is workspace-owned.
    """
    await session.execute(
        text(
            "DELETE FROM search_index_items WHERE chunk_id IN "
            "(SELECT id FROM document_chunks WHERE document_version_id = :v)"
        ),
        {"v": document_version_id},
    )


documents.register_version_superseded_hook(purge_version_embeddings)
