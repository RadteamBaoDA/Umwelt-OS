"""Topic watch provider: config validation, capability tables and provisioning command order."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.errors import ConflictError
from modules.connectors import registry, topic_watch
from modules.connectors.backends import (
    FIXED_SCOPE_PROVIDERS,
    GATED_EXECUTOR_PROVIDERS,
    PROVIDER_SCOPE_FIELDS,
)
from modules.connectors.public import ConnectorConfig, source_data_shapes
from modules.connectors.topic_watch import TopicWatchConfig
from modules.sources.schemas import ConnectorSource

BASE = {
    "timezone": "UTC",
    "timeout_seconds": 30,
    "history_mode": "returned_snapshot",
    "topic_watch_topic": "solid-state batteries",
    "topic_watch_max_items": 5,
    "topic_watch_token_budget": 23400,
}


def _src(configuration: dict) -> ConnectorSource:
    return ConnectorSource(
        id=uuid4(),
        workspace_id=uuid4(),
        type="web",
        status="active",
        generation=1,
        configuration=configuration,
        local_only=False,
        provider="topic_watch",
    )


def test_valid_config_accepted() -> None:
    registry.validate(_src(BASE))
    registry.validate(
        _src(
            {
                **BASE,
                "topic_watch_keep_rule": "only peer reviewed",
                "topic_watch_model_role": "background",
            }
        )
    )


@pytest.mark.parametrize(
    "missing", ["topic_watch_topic", "topic_watch_max_items", "topic_watch_token_budget"]
)
def test_config_requires_topic_items_budget(missing: str) -> None:
    config = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ValueError):
        registry.validate(_src(config))


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValueError):
        registry.validate(_src({**BASE, "news_query": "x"}))


def test_sites_and_languages_validated() -> None:
    cfg = ConnectorConfig(topic_watch_include_sites=["EXAMPLE.com"], topic_watch_languages=["en"])
    assert cfg.topic_watch_include_sites == ["example.com"]
    for bad in (
        {"topic_watch_include_sites": ["a b"]},
        {"topic_watch_languages": ["eng"]},
        {"topic_watch_exclude_sites": ["x" * 254]},
        {"topic_watch_max_items": 11},
        {"topic_watch_topic": ""},
        {"topic_watch_model_role": "other"},
    ):
        with pytest.raises(ValidationError):
            ConnectorConfig(**bad)


def test_topic_watch_is_gated_executor_not_fixed_scope() -> None:
    assert "topic_watch" not in FIXED_SCOPE_PROVIDERS
    assert "topic_watch" in GATED_EXECUTOR_PROVIDERS
    assert "topic_watch_topic" in PROVIDER_SCOPE_FIELDS["topic_watch"]


def test_data_shape_is_news() -> None:
    assert source_data_shapes("topic_watch", "web") == {"news"}


def _config() -> TopicWatchConfig:
    return TopicWatchConfig(
        topic="t",
        keep_rule=None,
        languages=("en",),
        include_sites=(),
        exclude_sites=(),
        max_items=5,
        token_budget=23400,
        model_role="background",
    )


def _patch(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    source = _src({})
    row = SimpleNamespace(desired_revision=0, state="")

    async def create_source(session, payload, **kw):
        calls.append(("create", payload.provider, payload.type, payload.name))
        return source

    async def save(session, src, rev, cfg, desired, **kw):
        calls.append(("save", cfg["topic_watch_topic"]))
        return src, row

    async def lock(session, source_id, slots=(), **kw):
        return SimpleNamespace(local_only=False), row, {}

    async def activate(session, source_id, fence, r, **kw):
        calls.append(("activate",))
        return ""

    async def schedule(session, **kw):
        calls.append(("schedule", kw["enabled"], kw["interval_minutes"]))

    monkeypatch.setattr(topic_watch.sources, "create_source", create_source)
    monkeypatch.setattr(topic_watch, "save_connector_configuration", save)
    monkeypatch.setattr(topic_watch.provisioning, "lock_connector", lock)
    monkeypatch.setattr(topic_watch, "activate_native_in_uow", activate)
    monkeypatch.setattr(topic_watch.scheduler, "upsert_schedule", schedule)
    monkeypatch.setattr(topic_watch.sources, "get_connector_source", _async_return(source))
    monkeypatch.setattr(topic_watch.provisioning, "activation_status", _async_return(row))


def _async_return(value):
    async def inner(*a, **k):
        return value

    return inner


async def test_provision_creates_source_activates_and_disables_schedule(monkeypatch) -> None:
    calls: list = []
    _patch(monkeypatch, calls)
    scope = SimpleNamespace(workspace_id=uuid4(), user_id=1)
    sid = await topic_watch.provision_topic_watch_source(
        object(), name="n" * 300, config=_config(), scope=scope, multi_workspace_enabled=False
    )
    assert sid is not None
    assert [c[0] for c in calls] == ["create", "save", "activate", "schedule"]
    assert calls[0][1:3] == ("topic_watch", "web") and len(calls[0][3]) == 200
    assert calls[-1] == ("schedule", False, 1440)


async def test_provision_activation_error_raises_conflict(monkeypatch) -> None:
    calls: list = []
    _patch(monkeypatch, calls)

    async def activate(*a, **k):
        return "native_unsupported"

    async def archive(_session, sid, **_k):
        calls.append(("archive",))

    class Session:
        async def rollback(self) -> None:
            calls.append(("rollback",))

    monkeypatch.setattr(topic_watch, "activate_native_in_uow", activate)
    monkeypatch.setattr(topic_watch.sources, "archive_source", archive)
    with pytest.raises(ConflictError):
        await topic_watch.provision_topic_watch_source(
            Session(),  # type: ignore[arg-type]
            name="n",
            config=_config(),
            scope=SimpleNamespace(workspace_id=uuid4(), user_id=1),
            multi_workspace_enabled=False,
        )
    assert [c[0] for c in calls[-2:]] == ["rollback", "archive"]  # no orphan Source


async def test_update_rewrites_config_reactivates_and_keeps_schedule_disabled(monkeypatch) -> None:
    calls: list = []
    _patch(monkeypatch, calls)
    await topic_watch.update_topic_watch_source(
        object(),
        uuid4(),
        _config(),
        scope=SimpleNamespace(workspace_id=uuid4(), user_id=1),
        multi_workspace_enabled=False,
    )
    assert [c[0] for c in calls] == ["save", "activate", "schedule"]
    assert calls[-1][1] is False


async def test_retire_archives_by_default_and_purges_on_request(monkeypatch) -> None:
    seen: list = []

    async def archive(session, sid, **kw):
        seen.append("archive")

    async def purge(session, sid, **kw):
        seen.append("purge")

    monkeypatch.setattr(topic_watch.sources, "archive_source", archive)
    monkeypatch.setattr(topic_watch.sources, "start_source_purge", purge)
    scope = SimpleNamespace(workspace_id=uuid4(), user_id=1)
    kw = {"scope": scope, "multi_workspace_enabled": False}
    await topic_watch.retire_topic_watch_source(object(), uuid4(), purge_items=False, **kw)
    await topic_watch.retire_topic_watch_source(object(), uuid4(), purge_items=True, **kw)
    assert seen == ["archive", "purge"]
