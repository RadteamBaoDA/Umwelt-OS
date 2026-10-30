"""History-mode observation window: opt-in revision history with unchanged default behaviour."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import modules.ingestion.models  # resolve Observation foreign-key targets
import modules.knowledge.documents.models
import modules.sources.models  # noqa: F401
from modules.knowledge.observations import public, routes
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import ObservationQuery

_FROM = datetime(2026, 10, 1, tzinfo=UTC)
_TO = datetime(2026, 10, 2, tzinfo=UTC)


def _sql(history: bool) -> str:
    """Compile the window statement for one query to PostgreSQL text."""
    query = ObservationQuery(source_ids=[uuid4()], from_at=_FROM, to_at=_TO, history=history)
    statement = public._window_statement(query, uuid4(), (Observation.source_id == uuid4(),))
    return str(statement.compile(dialect=postgresql.dialect()))


def test_history_defaults_off() -> None:
    """Legacy callers that omit the flag keep the current-only read."""
    assert ObservationQuery(source_ids=[uuid4()], from_at=_FROM, to_at=_TO).history is False


def test_current_only_statement_is_unchanged() -> None:
    """Default mode filters to current rows and adds no de-duplication subquery."""
    sql = _sql(False)
    assert "is_current IS true" in sql and "EXISTS" not in sql


def test_history_statement_keeps_noncurrent_and_dedupes_latest_revision() -> None:
    """History mode drops the current filter and hides rows superseded at the same observed_at."""
    sql = _sql(True)
    assert "is_current IS true" not in sql
    assert "NOT (EXISTS" in sql and "observed_at =" in sql
    for column in ("metric", "symbol", "region", "unit", "latitude", "longitude"):
        assert f"observations_1.{column}" in sql or f"{column} IS NOT DISTINCT FROM" in sql


async def test_route_forwards_history_and_keeps_default_off(monkeypatch) -> None:
    """The window route forwards ``history`` into the query; omitting it stays current-only."""
    seen: list[ObservationQuery] = []

    async def fake(session, query, cursor, *, scope, multi_workspace_enabled):
        """Capture the query the route built and return an empty page."""
        seen.append(query)
        return (), None, False, {}

    monkeypatch.setattr(public, "list_observations", fake)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))
    for flag in (True, False):
        await routes.read_observations(
            None, None, request, SimpleNamespace(headers={}), source_ids=[uuid4()], from_at=_FROM, to_at=_TO,
            metrics=[], symbols=[], regions=[], limit=100, cursor=None, geospatial_only=False, history=flag,
        )
    assert [q.history for q in seen] == [True, False]


# --- Behaviour of the window select against a real (in-memory SQLite) table -------------------------------

_WORKSPACE = uuid4()
_SOURCE = uuid4()
_OBSERVED = datetime(2026, 10, 1, 6, tzinfo=UTC)
_ACCEPTED = datetime(2026, 10, 1, 7, tzinfo=UTC)


def _row(revision: int, *, row_id: UUID | None = None, ingestion: UUID | None = None, observed=_OBSERVED,
         accepted=_ACCEPTED, current: bool = False, discriminator: str = "d") -> Observation:
    """Build one fully populated revision of the same series point (NULL symbol)."""
    return Observation(
        id=row_id or uuid4(), workspace_id=_WORKSPACE, source_id=_SOURCE, external_id="ext", provider="prov",
        provider_scope_discriminator=discriminator, revision=revision, metric="m", symbol=None, region=None,
        latitude=None, longitude=None, observed_at=observed, collected_at=accepted, accepted_at=accepted,
        value=float(revision), unit="u", quality="ok", is_current=current, content_hash="h",
        document_id=uuid4(), document_version_id=uuid4(), ingestion_observation_id=ingestion or uuid4(),
        source_generation=1,
    )


def _window(rows: list[Observation], history: bool) -> list[int]:
    """Persist ``rows`` into SQLite, run the window select, return revisions newest-observed first."""
    engine = create_engine("sqlite://")
    Observation.__table__.create(engine)
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        query = ObservationQuery(source_ids=[_SOURCE], from_at=_FROM, to_at=_TO, history=history)
        statement = public._window_statement(query, _WORKSPACE, (Observation.source_id == _SOURCE,))
        found = session.scalars(statement.order_by(Observation.observed_at.desc(), Observation.revision.desc())).all()
        return [row.revision for row in found]


def _corrected_series() -> list[Observation]:
    """Two same-point revisions (later accepted one non-current) plus a newer current row."""
    return [
        _row(1, accepted=_ACCEPTED),
        _row(2, accepted=_ACCEPTED + timedelta(minutes=5)),
        _row(3, observed=_OBSERVED + timedelta(hours=1), current=True),
    ]


def test_history_window_returns_latest_revision_per_point_and_current_row() -> None:
    """The older same-point revision is hidden; the later non-current one and the newer current row remain."""
    assert _window(_corrected_series(), True) == [3, 2]


def test_default_window_returns_only_current_rows() -> None:
    """Default mode ignores non-current revisions entirely."""
    assert _window(_corrected_series(), False) == [3]


def test_history_dedupe_tie_uses_current_selection_key() -> None:
    """Exactly equal accepted_at: the larger ingestion id wins (as current selection), not the larger row id."""
    low_ingestion, high_ingestion = UUID(int=1), UUID(int=2)
    winner_by_ingestion = _row(1, row_id=UUID(int=10), ingestion=high_ingestion)
    loser = _row(2, row_id=UUID(int=20), ingestion=low_ingestion)  # larger row id, smaller ingestion id
    assert _window([winner_by_ingestion, loser], True) == [1]


def test_history_dedupe_does_not_cross_provider_scope() -> None:
    """Same point under a different provider-scope discriminator is a separate series, not a correction."""
    rows = [_row(1, discriminator="a"), _row(2, discriminator="b", accepted=_ACCEPTED + timedelta(minutes=5))]
    assert sorted(_window(rows, True)) == [1, 2]


async def test_list_observations_forwards_history_to_evidence_check(monkeypatch) -> None:
    """``list_observations`` passes ``query.history`` as ``include_historical`` and returns accepted rows."""
    row = _row(1, current=True)
    seen: list[bool] = []

    async def admit(session, *, scope, multi_workspace_enabled):
        """Fake owner admission returning a fence with the fields the cursor fingerprint reads."""
        return SimpleNamespace(workspace_id=_WORKSPACE, user_id="u", membership_revision=1, configuration_revision=1)

    async def get_source(session, source_id, *, scope, multi_workspace_enabled):
        """Fake an active generation-1 source."""
        return SimpleNamespace(status="active", generation=1)

    async def get_scope(session, source_id, generation, *, scope, multi_workspace_enabled):
        """Fake the current provider-scope snapshot matching the row's fences."""
        return SimpleNamespace(
            source_id=_SOURCE, source_generation=1, provider_id="prov", discriminator="d",
        )

    async def evidence(session, candidates, current_scopes, *, scope, multi_workspace_enabled, include_historical):
        """Record the flag and accept every candidate with version number 7."""
        seen.append(include_historical)
        return {candidate.observation_id: 7 for candidate in candidates}

    monkeypatch.setattr(public, "_admit", admit)
    monkeypatch.setattr(public.sources, "get_connector_source", get_source)
    monkeypatch.setattr(public.connectors, "get_current_provider_scope", get_scope)
    monkeypatch.setattr(public.documents, "current_observation_evidence_versions", evidence)
    for flag in (True, False):
        session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [row])))
        query = ObservationQuery(source_ids=[_SOURCE], from_at=_FROM, to_at=_TO, history=flag)
        rows, _, _, versions = await public.list_observations(
            session, query, scope=SimpleNamespace(workspace_id=_WORKSPACE), multi_workspace_enabled=False,
        )
        assert rows == (row,) and versions == {row.id: 7}
    assert seen == [True, False]
