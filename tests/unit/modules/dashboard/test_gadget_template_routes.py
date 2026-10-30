"""Route-level coverage of the gadget template flow with persistence stubbed (no database)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_session
from core.workspaces.dependencies import require_workspace_observer_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.dashboard import public, routes
from modules.dashboard.schemas import DashboardDetail

KINDS = {"kp": ("noaa_kp", "api"), "news": ("bbc_world", "rss")}


class FakeSession:
    """Collects added rows and assigns identities on flush, like the ORM would."""

    def __init__(self) -> None:
        """Start with no added rows."""
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        """Record one added ORM row."""
        self.added.append(row)

    def add_all(self, rows: Any) -> None:
        """Record several added ORM rows."""
        self.added.extend(rows)

    async def flush(self) -> None:
        """Assign id/revision defaults to rows that lack them (server defaults in production)."""
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()
            if hasattr(row, "revision") and row.revision is None:
                row.revision = 1

    async def scalar(self, _stmt: object) -> int:
        """Every count query (quota checks) reports an empty owner."""
        return 0

    async def scalars(self, _stmt: object) -> SimpleNamespace:
        """Reference lookups find no consuming dashboards."""
        return SimpleNamespace(all=list)

    async def execute(self, _stmt: object) -> None:
        """Replacement deletes are not exercised."""


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeSession, dict[UUID, tuple[str, str]]]:
    """App with the dashboard router, stubbed auth/session and stubbed source/persistence seams."""
    session = FakeSession()
    kinds: dict[UUID, tuple[str, str]] = {}
    scope = WorkspaceContext(workspace_id=uuid4(), user_id=1, role="owner", membership_revision=1)

    async def noop(*_a: object, **_k: object) -> None:
        return None

    async def get_sources(_s: object, ids: object, **_k: object) -> list[object]:
        return [SimpleNamespace(provider=kinds[i][0], type=kinds[i][1]) for i in ids]  # type: ignore[attr-defined]

    async def states(_s: object, ids: object, **_k: object) -> dict[UUID, object]:
        return {i: SimpleNamespace(id=i, status="active", generation=1, provider=kinds[i][0], type=kinds[i][1])
                for i in ids}  # type: ignore[attr-defined]

    async def active_kinds(*_a: object, **_k: object) -> list[tuple[UUID, str, str]]:
        return [(i, p, t) for i, (p, t) in kinds.items()]

    async def read_dashboard(_s: object, dashboard: Any, **_k: object) -> DashboardDetail:
        now = datetime.now(UTC)
        empty = {"columns": 20, "items": []}
        return DashboardDetail.model_validate({
            "id": dashboard.id, "name": dashboard.name, "revision": 1, "created_at": now, "updated_at": now,
            "groups": [], "instances": [], "layouts": {"desktop": empty, "mobile": empty}})

    async def definition_states(*_a: object, **_k: object) -> dict[UUID, object]:
        return {}

    for name in ("_admit", "_lock_owner_creation_quota", "_lock_selected_sources", "validate_highlight_rules",
                 "commit_with_replay"):
        monkeypatch.setattr(public, name, noop)
    monkeypatch.setattr(public, "make_dashboard_change", lambda *a, **k: None)
    monkeypatch.setattr(public, "_source_states", states)
    monkeypatch.setattr(public, "_definition_source_states", definition_states)
    monkeypatch.setattr(public, "_dashboard_read", read_dashboard)
    monkeypatch.setattr(public.sources, "get_gadget_sources", get_sources)
    monkeypatch.setattr(public.sources, "list_active_source_kinds", active_kinds)

    app = FastAPI()
    app.state.settings = SimpleNamespace(multi_workspace_enabled=False)
    app.include_router(routes.router)
    for dep in routes.router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: None
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_workspace_write] = lambda: scope
    app.dependency_overrides[require_workspace_observer_read] = lambda: scope
    return TestClient(app), session, kinds


def test_create_and_rename_legacy_definition_and_counts(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """finance_chart + noaa_kp + kp_index creates (201); a legacy off-catalog row renames (200); counts are served."""
    http, _session, kinds = client
    source = uuid4()
    kinds[source] = KINDS["kp"]
    created = http.post("/api/v1/gadget-definitions", json={
        "name": "kp", "renderer": "finance_chart", "source_ids": [str(source)], "scope": {"metrics": ["kp_index"]}})
    assert created.status_code == 201, created.text
    assert created.json()["scope"]["metrics"] == ["kp_index"]

    legacy = SimpleNamespace(
        id=uuid4(), revision=1, name="old", renderer="provider_observation", source_ids=[str(source)],
        scope={"metrics": ["legacy_metric"]}, filters={}, highlight_rules=[])

    async def lock_definition(*_a: object, **_k: object) -> object:
        return legacy

    monkeypatch.setattr(public, "_lock_definition", lock_definition)
    renamed = http.patch(f"/api/v1/gadget-definitions/{legacy.id}", json={"expected_revision": 1, "name": "new"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "new"

    counts = http.get("/api/v1/gadget-renderers/compatible-counts")
    assert counts.status_code == 200
    assert counts.json()["finance_chart"] == 1 and counts.json()["news_feed"] == 0 and "tasks" not in counts.json()


def test_preset_alias_preview_and_apply_bind_sources_and_seed_scope(client: Any) -> None:
    """The retired id finance_variant previews/applies as finance: proposed sources by default, explicit -> seeded scope."""
    http, session, kinds = client
    kp, news = uuid4(), uuid4()
    kinds[kp], kinds[news] = KINDS["kp"], KINDS["news"]

    unbound = http.post("/api/v1/dashboard-presets/finance_variant/preview", json={"slot_sources": {}})
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["preset_id"] == "finance"
    proposed = {s["slot_id"]: s["source_ids"] for s in unbound.json()["slots"]}
    assert proposed == {"finance_chart": [str(kp)], "news_feed": [str(news)]}

    body = {"slot_sources": {"finance_chart": [str(kp)], "news_feed": [str(news)]}}
    preview = http.post("/api/v1/dashboard-presets/finance_variant/preview", json=body)
    assert preview.status_code == 200 and all(not s["warnings"] for s in preview.json()["slots"])
    applied = http.post("/api/v1/dashboard-presets/finance_variant/apply", json={
        **body, "preview_fingerprint": preview.json()["preview_fingerprint"], "mode": "create"})
    assert applied.status_code == 201, applied.text
    definitions = {row.renderer: row for row in session.added if hasattr(row, "renderer")}
    assert definitions["finance_chart"].source_ids == [str(kp)]
    assert definitions["finance_chart"].scope == {"metrics": ["kp_index"], "lookback_days": 30}
    assert definitions["news_feed"].source_ids == [str(news)]


def test_metric_catalog_route_is_typed(client: Any) -> None:
    """The catalog is served through ProviderMetricsRead: metrics[{id,unit}], symbol_keyed, suggestions, open flag."""
    http, _session, _kinds = client
    body = http.get("/api/v1/gadget-metric-catalog").json()
    assert body["noaa_kp"] == {"metrics": [{"id": "kp_index", "unit": "kp"}], "symbol_keyed": False,
                               "suggested_symbols": [], "metrics_open": False,
                               "charts_as_series": True, "region_keyed": False, "suggested_regions": [],
                               "seed_symbol": True, "cadence_days": 0.125, "default_lookback_days": 30}
    assert body["usgs"]["charts_as_series"] is False
    assert body["world_bank"]["region_keyed"] is False and body["world_bank"]["suggested_regions"] == []
    assert body["world_bank"]["default_lookback_days"] == 3650
    assert body["openaq"]["region_keyed"] is True
    assert body["binance"]["suggested_symbols"] == ["BTCUSDT"] and body["binance"]["seed_symbol"] is False
