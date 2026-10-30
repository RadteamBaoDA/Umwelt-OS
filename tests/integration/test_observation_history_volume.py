"""History-window query volume check against real PostgreSQL (needs the Compose harness)."""

import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modules.ingestion.models import IngestionBatch, SourceObservation
from modules.knowledge.observations import public as observations
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import ObservationQuery
from tests.integration.test_observation_history import DISCRIMINATOR, NOW, _seed

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)

SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE")
POINTS = 14 * 24 * 4  # 14 days at 15-minute cadence
START = NOW - timedelta(days=14)


@pytest.mark.asyncio
async def test_history_window_scales_to_two_week_fifteen_minute_series(
    ready_owner_client: AsyncClient, committed_engine: AsyncEngine,
) -> None:
    """5 symbols x 1344 points, each with a superseded and a current revision: one row per point, under 2s."""
    seed = await _seed(ready_owner_client, committed_engine)
    async with AsyncSession(committed_engine, expire_on_commit=False) as session:
        # This test does not exercise purge: undo _seed's 1-day window so the hourly cron cannot race the data.
        await session.execute(text(
            "UPDATE sources SET configuration = configuration || jsonb_build_object('retention_days', 3650) "
            "WHERE id = :id"), {"id": seed.source_id})
        await session.commit()
        batch = IngestionBatch(source_id=seed.source_id, batch_key=uuid4().hex, payload_hash=uuid4().hex + uuid4().hex)
        session.add(batch)
        await session.flush()
        for symbol in SYMBOLS:
            raws, rows = [], []
            for i in range(POINTS):
                observed = START + timedelta(minutes=15 * i)
                for revision in (1, 2):
                    raw = SourceObservation(source_id=seed.source_id, batch_id=batch.id, provider_id="ext",
                                            record_hash=uuid4().hex + uuid4().hex, payload={}, observed_at=observed)
                    raws.append(raw)
                    rows.append((raw, observed, revision))
            session.add_all(raws)
            await session.flush()
            session.add_all(Observation(
                workspace_id=seed.workspace_id, source_id=seed.source_id, external_id=f"{symbol}-{i}",
                provider="open_meteo", provider_scope_discriminator=DISCRIMINATOR, revision=revision, metric="m",
                symbol=symbol, region=None, observed_at=observed, collected_at=observed,
                accepted_at=observed + timedelta(minutes=revision), value=float(revision), unit="u", quality="ok",
                is_current=revision == 2, content_hash=uuid4().hex + uuid4().hex, document_id=seed.document_id,
                document_version_id=seed.new_version_id if revision == 2 else seed.old_version_id,
                ingestion_observation_id=raw.id, source_generation=1,
            ) for i, (raw, observed, revision) in enumerate((r[0], r[1], r[2]) for r in rows))
            await session.commit()
        await session.execute(text("ANALYZE observations"))
        total = (await session.execute(text("SELECT count(*) FROM observations WHERE source_id = :s"),
                                       {"s": seed.source_id})).scalar_one()
        assert total == len(SYMBOLS) * POINTS * 2

        query = ObservationQuery(source_ids=[seed.source_id], symbols=["CCC"], from_at=START - timedelta(hours=1),
                                 to_at=NOW + timedelta(hours=1), history=True)
        statement = observations._window_statement(
            query, seed.workspace_id, (Observation.source_id == seed.source_id,),
        ).where(Observation.symbol == "CCC")
        started = time.perf_counter()
        rows = (await session.scalars(statement.order_by(Observation.observed_at.desc(), Observation.id.desc()))).all()
        elapsed = time.perf_counter() - started
        assert len(rows) == POINTS
        assert len({row.observed_at for row in rows}) == POINTS
        assert all(row.revision == 2 for row in rows)
        assert elapsed < 2.0

        compiled = statement.order_by(Observation.observed_at.desc(), Observation.id.desc()).compile(
            dialect=session.bind.dialect, compile_kwargs={"literal_binds": True})
        plan = (await session.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + str(compiled)))).scalars().all()
        # The harness runs pytest -q, which hides captured stdout, so the plan goes to a temp file.
        Path(tempfile.gettempdir(), "history-plan.txt").write_text("\n".join(plan), encoding="utf-8")
