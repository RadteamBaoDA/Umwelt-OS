"""Observation history window and retention against real PostgreSQL (needs the Compose harness).

Rows the API cannot create (ingestion batch, source observation, provenance, observation revisions)
are inserted with the owning ORM models, exactly as the ingestion write path persists them.
"""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.workspaces.schemas import WorkspaceContext
from modules.ingestion.models import IngestionBatch, SourceObservation
from modules.ingestion.public import purge_expired_datasource_data
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import DocumentVersion, NormalizedVersionProvenance
from modules.knowledge.observations import public as observations
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import ObservationQuery
from modules.sources.models import SourcePurgeOperation

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)

DISCRIMINATOR = "history-scope"
NOW = datetime.now(UTC).replace(microsecond=0)
POINT = NOW - timedelta(days=3)


class Seed:
    """Identifiers of one seeded source with a document that has a superseded and a current version."""

    def __init__(self, workspace_id: UUID, user_id: int, source_id: UUID, document_id: UUID,
                 old_version_id: UUID, new_version_id: UUID) -> None:
        """Hold the committed row identities used by the assertions."""
        self.workspace_id, self.user_id, self.source_id = workspace_id, user_id, source_id
        self.document_id, self.old_version_id, self.new_version_id = document_id, old_version_id, new_version_id


async def _seed(client: AsyncClient, engine: AsyncEngine) -> Seed:
    """Create a manual source/document through the API and add a second (current) document version.

    Sessions use ``expire_on_commit=False`` so committed identities stay readable without lazy loads.

    The source is retyped to ``api`` (connector-like; ``manual`` sources are retention-exempt) and gets
    ``retention_days = 1`` so the 3-day-old fixtures are past its retention cutoff.
    """
    source = await client.post("/api/v1/sources", json={"type": "manual", "name": f"hist {uuid4().hex[:8]}"})
    source.raise_for_status()
    source_id = UUID(source.json()["id"])
    created = await client.post("/api/v1/documents", json={
        "source_id": str(source_id), "title": "history", "content": f"c {uuid4().hex}", "external_id": f"h-{uuid4().hex}",
    })
    created.raise_for_status()
    document_id = UUID(created.json()["id"])
    async with AsyncSession(engine, expire_on_commit=False) as session:
        workspace_id, user_id = (await session.execute(text(
            "SELECT s.workspace_id, w.owner_user_id FROM sources s JOIN workspaces w ON w.id = s.workspace_id "
            "WHERE s.id = :id"), {"id": source_id})).one()
        old = (await session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id))).one()
        new = DocumentVersion(document_id=document_id, version_number=old.version_number + 1,
                              content="v2", content_hash=uuid4().hex + uuid4().hex)
        session.add(new)
        await session.flush()
        await session.execute(text("UPDATE documents SET current_version = :v WHERE id = :id"),
                              {"v": new.version_number, "id": document_id})
        await session.execute(text(
            "UPDATE sources SET type = 'api', "
            "configuration = configuration || jsonb_build_object('retention_days', 1) WHERE id = :id"
        ), {"id": source_id})
        for version in (old, new):
            session.add(NormalizedVersionProvenance(
                document_id=document_id, document_version_id=version.id, provider_id="ext",
                accepted_record_hash=uuid4().hex + uuid4().hex, normalization_version=1, source_generation=1,
                observed_at=POINT, selection_observed_at=POINT, title="history",
                provenance_json={"provider_scope_discriminator": DISCRIMINATOR},
            ))
        await session.commit()
        return Seed(workspace_id, user_id, source_id, document_id, old.id, new.id)


async def _observation(session: AsyncSession, seed: Seed, *, revision: int, observed_at: datetime,
                       accepted_at: datetime, version_id: UUID, current: bool) -> Observation:
    """Insert one source observation plus the observation revision that references it."""
    batch = IngestionBatch(source_id=seed.source_id, batch_key=uuid4().hex, payload_hash=uuid4().hex + uuid4().hex)
    session.add(batch)
    await session.flush()
    raw = SourceObservation(source_id=seed.source_id, batch_id=batch.id, provider_id="ext",
                            record_hash=uuid4().hex + uuid4().hex, payload={}, observed_at=observed_at)
    session.add(raw)
    await session.flush()
    row = Observation(
        workspace_id=seed.workspace_id, source_id=seed.source_id, external_id="ext", provider="open_meteo",
        provider_scope_discriminator=DISCRIMINATOR, revision=revision, metric="m", symbol=None, region=None,
        observed_at=observed_at, collected_at=accepted_at, accepted_at=accepted_at, value=float(revision), unit="u",
        quality="ok", is_current=current, content_hash=uuid4().hex + uuid4().hex, document_id=seed.document_id,
        document_version_id=version_id, ingestion_observation_id=raw.id, source_generation=1,
    )
    session.add(row)
    await session.flush()
    return row


async def _window(session: AsyncSession, seed: Seed, history: bool) -> list[int]:
    """Run the real window select (no evidence fence) and return revisions newest-observed first."""
    query = ObservationQuery(source_ids=[seed.source_id], from_at=NOW - timedelta(days=10),
                             to_at=NOW + timedelta(days=1), history=history)
    statement = observations._window_statement(query, seed.workspace_id, (Observation.source_id == seed.source_id,))
    rows = (await session.scalars(statement.order_by(Observation.observed_at.desc(), Observation.revision.desc()))).all()
    return [row.revision for row in rows]


async def _keep_history(engine: AsyncEngine, seed: Seed) -> None:
    """Widen retention to 3650 days so tests that do not exercise purge cannot race the hourly cron."""
    async with AsyncSession(engine) as session:
        await session.execute(text(
            "UPDATE sources SET configuration = configuration || jsonb_build_object('retention_days', 3650) "
            "WHERE id = :id"), {"id": seed.source_id})
        await session.commit()


async def _seed_series(session: AsyncSession, seed: Seed) -> None:
    """Two same-point revisions (r2 the later, non-current), plus a newer current row (r3)."""
    await _observation(session, seed, revision=1, observed_at=POINT, accepted_at=POINT + timedelta(hours=1),
                       version_id=seed.old_version_id, current=False)
    await _observation(session, seed, revision=2, observed_at=POINT, accepted_at=POINT + timedelta(hours=2),
                       version_id=seed.old_version_id, current=False)
    await _observation(session, seed, revision=3, observed_at=POINT + timedelta(days=1),
                       accepted_at=POINT + timedelta(days=1), version_id=seed.new_version_id, current=True)
    await session.commit()


@pytest.mark.asyncio
async def test_history_window_returns_superseded_revisions_and_dedupes_corrections(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """History keeps the latest revision per point plus the current row; default mode is current-only."""
    seed = await _seed(ready_owner_client, committed_engine)
    await _keep_history(committed_engine, seed)
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        await _seed_series(session, seed)
        assert await _window(session, seed, True) == [3, 2]
        assert await _window(session, seed, False) == [3]


@pytest.mark.asyncio
async def test_history_evidence_accepts_superseded_version_only_in_history_mode(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """A superseded-version revision passes the evidence fence only in history mode, never for purging sources."""
    seed = await _seed(ready_owner_client, committed_engine)
    await _keep_history(committed_engine, seed)
    scope = WorkspaceContext(user_id=seed.user_id, workspace_id=seed.workspace_id, role="owner", membership_revision=1)
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        row = await _observation(session, seed, revision=1, observed_at=POINT, accepted_at=POINT,
                                 version_id=seed.old_version_id, current=False)
        await session.commit()
        candidate = documents.ObservationEvidenceCandidate(
            observation_id=row.id, source_id=seed.source_id, source_generation=1, provider="open_meteo",
            provider_scope_discriminator=DISCRIMINATOR, external_id="ext", document_id=seed.document_id,
            document_version_id=seed.old_version_id,
        )
        live = {seed.source_id: SimpleNamespace(provider_id="open_meteo", source_generation=1, discriminator=DISCRIMINATOR)}
        kwargs = {"scope": scope, "multi_workspace_enabled": False}
        assert await documents.current_observation_evidence_versions(session, [candidate], live, **kwargs) == {}
        assert await documents.current_observation_evidence_versions(
            session, [candidate], live, include_historical=True, **kwargs) == {row.id: 1}
        # A queued data purge hides the source's evidence even in history mode.
        session.add(SourcePurgeOperation(
            workspace_id=seed.workspace_id, actor_user_id=seed.user_id, membership_revision=1,
            source_id=seed.source_id, generation=1, status="queued",
        ))
        await session.commit()
        assert await documents.current_observation_evidence_versions(
            session, [candidate], live, include_historical=True, **kwargs) == {}


@pytest.mark.asyncio
async def test_deleting_a_document_version_removes_its_observation_rows(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """Observation.document_version_id cascades, so history never exposes rows of a deleted version."""
    seed = await _seed(ready_owner_client, committed_engine)
    await _keep_history(committed_engine, seed)
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        await _seed_series(session, seed)
        await session.execute(text("DELETE FROM document_versions WHERE id = :id"), {"id": seed.old_version_id})
        await session.commit()
        assert await _window(session, seed, True) == [3]


@pytest.mark.asyncio
async def test_retention_purge_removes_expired_noncurrent_rows_and_keeps_current(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """Non-current rows older than the source cutoff are purged; the (equally old) current row stays."""
    seed = await _seed(ready_owner_client, committed_engine)
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        await _seed_series(session, seed)
        await purge_expired_datasource_data(session, now=NOW)
        await session.commit()
        assert await _window(session, seed, True) == [3]


@pytest.mark.asyncio
async def test_retention_purge_keeps_versions_of_manual_sources(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """User-authored ``manual`` sources are retention-exempt: old non-current versions survive the purge."""
    seed = await _seed(ready_owner_client, committed_engine)
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        await session.execute(text("UPDATE sources SET type = 'manual' WHERE id = :id"), {"id": seed.source_id})
        # The purge cutoff filters on observed_at (server default now()), so back-date it as well as created_at.
        await session.execute(text(
            "UPDATE document_versions SET created_at = :t, observed_at = :t WHERE id = :id"),
            {"t": POINT, "id": seed.old_version_id})
        await session.commit()
        await purge_expired_datasource_data(session, now=NOW)
        await session.commit()
        kept = (await session.scalars(select(DocumentVersion.id).where(
            DocumentVersion.document_id == seed.document_id))).all()
        assert seed.old_version_id in kept
        # Positive control: the same old, non-current, unbookmarked, observation-free version of an 'api'
        # source (retention_days = 1 from _seed) is past the cutoff and must be deleted.
        await session.execute(text("UPDATE sources SET type = 'api' WHERE id = :id"), {"id": seed.source_id})
        await session.commit()
        await purge_expired_datasource_data(session, now=NOW)
        await session.commit()
        kept = (await session.scalars(select(DocumentVersion.id).where(
            DocumentVersion.document_id == seed.document_id))).all()
        assert seed.old_version_id not in kept
        assert seed.new_version_id in kept
