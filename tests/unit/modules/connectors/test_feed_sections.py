"""Wave B: RSS section catalog, validation, multi-section fetch and run stats (fakes only: no DB, no network)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from modules.connectors import builtins, collection, registry
from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.provider_specs import (
    BUILTIN_PROVIDERS,
    applicable_options,
    get_provider_spec,
    get_quota_policies,
)
from modules.connectors.providers import news, rest
from modules.connectors.public import ConnectorConfig, serialize_source_configuration
from modules.connectors.sections import MAX_SECTIONS, SECTION_CATALOG
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import CollectionStateUpdate
from modules.sources.schemas import ConnectorSource
from tests.unit.modules.connectors.test_builtins import SCOPE, _enable, _request, _session, _World
from tests.unit.modules.connectors.test_free_provider_dispatch import Gate, make_run

T0 = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)
PROVIDERS = sorted(SECTION_CATALOG)


def _rss(items: list[tuple[str, datetime]]) -> bytes:
    """Build an RSS body from ``(guid, pubDate)`` pairs."""
    rows = "".join(
        f"<item><title>title {g}</title><guid>{g}</guid><pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>"
        for g, d in items)
    return f"<rss><channel>{rows}</channel></rss>".encode()


def test_catalog_covers_the_eight_providers_and_stays_on_declared_hosts() -> None:
    assert PROVIDERS == sorted(
        ["tuoitre_news", "vnexpress_business", "thanhnien_news", "dantri_news", "bbc_world", "npr_news",
         "fed_press", "lobsters", "github_trending", "wwr_jobs"])
    for provider, catalog in SECTION_CATALOG.items():
        spec = get_provider_spec(provider)
        slugs = [s.slug for s in catalog.sections]
        assert len(slugs) == len(set(slugs)) and catalog.default in slugs
        for section in catalog.sections:
            assert section.label_en and section.label_vi
            parts = urlsplit(section.url)
            assert parts.scheme == "https" and parts.hostname in spec.hosts
        assert len({s.url for s in catalog.sections}) == len(slugs)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_default_section_reproduces_todays_feed_url(provider) -> None:
    catalog = SECTION_CATALOG[provider]
    assert catalog.by_slug()[catalog.default].url == news.FEED_URLS[provider]
    assert news.section_urls(provider, {}) == [(catalog.default, news.FEED_URLS[provider])]
    assert news.preset_url(provider, {}) == news.FEED_URLS[provider]


def test_section_urls_follow_selection_order() -> None:
    urls = news.section_urls("tuoitre_news", {"feed_sections": ["kinh-doanh", "home"]})
    assert urls == [("kinh-doanh", "https://tuoitre.vn/kinh-doanh.rss"), ("home", "https://tuoitre.vn/home.rss")]
    assert news.section_urls("hn_top", {}) == []  # no catalog, no sections


@pytest.mark.parametrize("provider", PROVIDERS)
def test_pilot_cap_covers_six_sections_at_the_default_cadence(provider) -> None:
    day = [p for p in get_quota_policies(provider) if p.window == "day" and p.limit_units is not None]
    assert day and day[0].limit_units == 96 * MAX_SECTIONS


def test_applicable_options_by_provider_shape() -> None:
    assert "feed_sections" in applicable_options("tuoitre_news")
    assert "feed_sections" not in applicable_options("aljazeera_world")
    assert "max_items" in applicable_options("aljazeera_world")
    assert applicable_options("binance") == ["name", "schedule_interval_minutes", "crypto_symbols"]  # measurement: no selection filters


def _src(provider: str, configuration: dict) -> ConnectorSource:
    return ConnectorSource(
        id=uuid4(), workspace_id=uuid4(), type=PROVIDER_SOURCE_TYPES[provider], status="active", generation=1,
        configuration=configuration, local_only=False, provider=provider)


BASE = {"timezone": "UTC", "timeout_seconds": 30, "schedule_interval_minutes": 30, "history_mode": "returned_snapshot"}


def test_registry_accepts_catalog_sections_and_rejects_the_rest() -> None:
    registry.validate(_src("tuoitre_news", {**BASE, "feed_sections": ["home", "kinh-doanh"]}))
    for bad in (["nope"], ["home", "home"], ["home"] * 2):
        with pytest.raises(ValueError):
            registry.validate(_src("tuoitre_news", {**BASE, "feed_sections": bad}))
    with pytest.raises(ValueError):  # a section slug from another provider's catalog
        registry.validate(_src("bbc_world", {**BASE, "feed_sections": ["kinh-doanh"]}))
    with pytest.raises(ValueError):  # provider without a catalog
        registry.validate(_src("aljazeera_world", {**BASE, "feed_sections": ["home"]}))


def test_config_bounds_sections_to_one_through_six() -> None:
    assert ConnectorConfig(feed_sections=["a"]).feed_sections == ("a",)
    for bad in ([], ["a"] * 7):
        with pytest.raises(ValidationError):
            ConnectorConfig(feed_sections=bad)
    source = _src("tuoitre_news", {})
    assert serialize_source_configuration(source, ConnectorConfig(feed_sections=["home"]))["feed_sections"] == ["home"]
    with pytest.raises(ValueError):
        serialize_source_configuration(_src("aljazeera_world", {}), ConnectorConfig(feed_sections=["home"]))


async def test_enable_saves_sections_and_resume_keeps_them() -> None:
    with _World("tuoitre_news") as world:
        await builtins.enable_builtin(
            "tuoitre_news", _session(), _request(), SCOPE,  # type: ignore[arg-type]
            builtins.BuiltinEnableRequest(feed_sections=("home", "kinh-doanh")))
        await builtins.disable_builtin("tuoitre_news", _session(), _request(), SCOPE)  # type: ignore[arg-type]
        await _enable("tuoitre_news")
    assert world.row.desired_configuration["feed_sections"] == ["home", "kinh-doanh"]


async def test_enable_without_sections_stores_none_and_null_clears() -> None:
    with _World("tuoitre_news") as world:
        await _enable("tuoitre_news")
        assert "feed_sections" not in world.row.desired_configuration
        await builtins.enable_builtin(
            "tuoitre_news", _session(), _request(), SCOPE,  # type: ignore[arg-type]
            builtins.BuiltinEnableRequest(feed_sections=("thoi-su",)))
        await builtins.enable_builtin(
            "tuoitre_news", _session(), _request(), SCOPE,  # type: ignore[arg-type]
            builtins.BuiltinEnableRequest(feed_sections=None))
    assert "feed_sections" not in world.row.desired_configuration


@pytest.mark.parametrize(("provider", "sections"), [
    ("tuoitre_news", ("nope",)), ("tuoitre_news", ("home", "home")), ("aljazeera_world", ("home",)),
    ("binance", ("home",)),
])
async def test_enable_rejects_bad_sections_with_422(provider, sections) -> None:
    with _World(provider), pytest.raises(HTTPException) as exc:
        await builtins.enable_builtin(
            provider, _session(), _request(), SCOPE,  # type: ignore[arg-type]
            builtins.BuiltinEnableRequest(feed_sections=sections))
    assert exc.value.status_code == 422


def test_more_than_six_sections_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        builtins.BuiltinEnableRequest(feed_sections=tuple(f"s{i}" for i in range(7)))


def test_builtin_read_exposes_catalog_and_applicable_options() -> None:
    spec = get_provider_spec("tuoitre_news")
    read = builtins._read(spec, None, False)
    catalog = SECTION_CATALOG["tuoitre_news"]
    assert read.default_section == catalog.default
    assert [(s.slug, s.label_en, s.label_vi) for s in read.sections] == [
        (s.slug, s.label_en, s.label_vi) for s in catalog.sections]
    assert "feed_sections" in read.applicable_options
    plain = builtins._read(get_provider_spec("aljazeera_world"), None, False)
    assert plain.sections is None and plain.default_section is None
    assert "tuoitre_news" in BUILTIN_PROVIDERS


# ------------------------------------------------------------------ multi-section runner

def _sections_run(monkeypatch, bodies: dict, configuration: dict, provider="tuoitre_news", state=None):
    gate = Gate(bodies)
    run, accepted = make_run(provider, gate, monkeypatch, state=state, configuration=configuration)
    run.attempt.source.configuration = configuration
    return run, accepted, gate


async def test_default_run_requests_exactly_todays_url(monkeypatch) -> None:
    body = _rss([("a", T0)])
    run, _, gate = _sections_run(monkeypatch, {"*": rest.Fetched(body)}, {})
    await collection._run_feed(run)
    assert [c[0] for c in gate.calls] == [news.FEED_URLS["tuoitre_news"]]


async def test_same_article_in_two_sections_is_one_record_with_the_single_fetch_hash(monkeypatch) -> None:
    shared = ("shared", T0)
    bodies = {
        "https://tuoitre.vn/home.rss": rest.Fetched(_rss([shared, ("only-home", T0 + timedelta(minutes=1))])),
        "https://tuoitre.vn/kinh-doanh.rss": rest.Fetched(_rss([shared, ("only-biz", T0 + timedelta(minutes=2))])),
    }
    run, accepted, gate = _sections_run(monkeypatch, bodies, {"feed_sections": ["home", "kinh-doanh"]})
    await collection._run_feed(run)
    records, _, update = accepted[0]
    assert update is None  # multi-section runs carry no validators
    assert sorted(r.provider_id for r in records) == ["only-biz", "only-home", "shared"]
    assert len(gate.calls) == 2  # one debited GET per section
    assert all("If-None-Match" not in c[1]["headers"] for c in gate.calls)
    single_run, single_accepted, _ = _sections_run(
        monkeypatch, {"*": rest.Fetched(bodies["https://tuoitre.vn/home.rss"].body)}, {})
    await collection._run_feed(single_run)
    single = {r.provider_id: ingestion.native_record_key(r) for r in single_accepted[0][0]}
    merged = {r.provider_id: ingestion.native_record_key(r) for r in records}
    assert merged["shared"] == single["shared"]  # section never enters the hashed metadata
    assert not any("section" in json.dumps(r.metadata) or "tuoitre.vn" in json.dumps(r.metadata) for r in records)
    assert run.extras["selection"]["failed_sections"] == []


async def test_conditional_get_only_when_exactly_one_section_selected(monkeypatch) -> None:
    state = SimpleNamespace(etag='"v1"', last_modified=None, validators_revision=2)
    body = rest.Fetched(_rss([("a", T0)]))
    run, _, gate = _sections_run(monkeypatch, {"*": body}, {"feed_sections": ["home"]}, state=state)
    await collection._run_feed(run)
    assert gate.calls[0][1]["headers"]["If-None-Match"] == '"v1"'  # url == stored url for the default section
    run, _, gate = _sections_run(monkeypatch, {"*": body}, {"feed_sections": ["home", "thoi-su"]}, state=state)
    await collection._run_feed(run)
    assert all("If-None-Match" not in c[1]["headers"] for c in gate.calls)


async def test_partial_section_failure_keeps_records_and_reports_the_failed_slug(monkeypatch) -> None:
    bodies = {
        "https://tuoitre.vn/home.rss": rest.Fetched(_rss([("a", T0)])),
        "https://tuoitre.vn/thoi-su.rss": rest.ProviderHttpError(500),
    }
    run, accepted, _ = _sections_run(monkeypatch, bodies, {"feed_sections": ["home", "thoi-su"]})
    await collection._run_feed(run)
    records, _, _ = accepted[0]
    assert [r.provider_id for r in records] == ["a"]
    assert run.extras["selection"]["failed_sections"] == ["thoi-su"]
    assert collection._run_stats(run, records) == {
        "new": 1, "backlog": 0, "gap": None, "duplicates": 0, "failed_sections": ["thoi-su"]}


async def test_all_sections_failing_fails_the_run_as_before(monkeypatch) -> None:
    run, accepted, _ = _sections_run(
        monkeypatch, {"*": rest.ProviderHttpError(503)}, {"feed_sections": ["home", "thoi-su"]})
    with pytest.raises(rest.ProviderHttpError):
        await collection._run_feed(run)
    assert not accepted


async def test_fence_loss_in_one_section_is_never_swallowed(monkeypatch) -> None:
    bodies = {"*": collection.CollectionFenceLost("admission_lost")}
    run, accepted, _ = _sections_run(monkeypatch, bodies, {"feed_sections": ["home", "thoi-su"]})
    with pytest.raises(collection.CollectionFenceLost):
        await collection._run_feed(run)
    assert not accepted


# ------------------------------------------------------------------ run stats

async def test_accept_native_hands_selection_stats_to_ingestion(monkeypatch) -> None:
    captured = {}

    async def accept(_session, _batch, **kw):
        captured.update(kw)

    monkeypatch.setattr(ingestion, "accept_native_collection", accept)

    async def no_publish(*_a, **_k):
        return None

    monkeypatch.setattr(collection, "_publish_realtime_snapshot", no_publish)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace()

    run = collection.Run(
        ctx={}, factory=factory, settings=None, gate=SimpleNamespace(captured_operation=None),
        attempt=SimpleNamespace(
            source=SimpleNamespace(id=uuid4(), generation=1, provider="tuoitre_news"), connector_revision=2,
            scope=SimpleNamespace(), multi=False, ref=SimpleNamespace()))
    run.extras["selection"] = {"backlog": 4, "gap": ("a", "b"), "duplicates": 2, "failed_sections": ["x"]}
    lease = SimpleNamespace(token=uuid4(), cursor_before=None)
    await collection._accept_native(run, lease, [], "returned_snapshot", T0)
    stats = captured["state_update"].stats
    assert stats == {"new": 0, "backlog": 4, "gap": ["a", "b"], "duplicates": 2, "failed_sections": ["x"]}
    assert len(json.dumps(stats)) < 1024


async def test_settle_request_persists_bounded_stats_on_the_receipt(monkeypatch) -> None:
    from modules.connectors import public as connectors
    from tests.unit.modules.ingestion.test_request_bound_acceptance import RecSession, _settle

    session = RecSession()
    stats = {"new": 3, "backlog": 0, "gap": None, "duplicates": 1, "failed_sections": []}
    await _settle(session, monkeypatch, state_update=CollectionStateUpdate(stats=stats))
    assert session.added[0].collection_stats == stats
    big = RecSession()
    await _settle(big, monkeypatch, state_update=CollectionStateUpdate(stats={"failed_sections": ["x" * 40] * 60}))
    assert big.added[0].collection_stats is None  # over the 1 KB bound: dropped, never truncated mid-JSON
    none = RecSession()
    await _settle(none, monkeypatch)
    assert none.added[0].collection_stats is None
    assert connectors  # imported for monkeypatch target parity


def test_run_read_carries_optional_collection_stats() -> None:
    from modules.ingestion.schemas import RunRead

    assert RunRead.model_fields["collection_stats"].default is None


def test_receipt_migration_adds_and_drops_the_nullable_column() -> None:
    import importlib.util
    from pathlib import Path

    path = Path("infrastructure/postgres/migrations/versions/0007_collection_receipt_stats.py")
    spec = importlib.util.spec_from_file_location("m0007", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0007_collection_receipt_stats" and module.down_revision == "0006_web_search_fallbacks"
    assert callable(module.upgrade) and callable(module.downgrade)
    from modules.ingestion.models import CollectionReceipt

    column = CollectionReceipt.__table__.c.collection_stats
    assert column.nullable and column.type.__class__.__name__ == "JSONB"
