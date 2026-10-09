from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import upgrade_templates as ut


class _Result:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Session:
    def __init__(self, ids=(), scalars=None, row=None):
        self.ids, self.scalar_values, self.row = list(ids), list(scalars or []), row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def scalars(self, _statement):
        return _Result(self.ids)

    async def scalar(self, _statement, _params=None):
        return self.scalar_values.pop(0)

    async def get(self, _model, _id):
        return self.row

    async def rollback(self):
        return None


def _settings(key=""):
    return SimpleNamespace(
        n8n_api_key=SimpleNamespace(get_secret_value=lambda: key), n8n_service_url="http://n8n", multi_workspace_enabled=True)


@pytest.mark.asyncio
async def test_run_tallies_each_outcome_and_reports_resume_cursor(monkeypatch):
    ids = [uuid4() for _ in range(4)]
    outcomes = iter(["upgraded", "reconciliation_required", "skipped", "would_upgrade"])

    async def one(_factory, _id, _api, *, apply, flag):
        assert apply is False and flag is True  # default is a dry run
        return next(outcomes)

    monkeypatch.setattr(ut, "_upgrade_one", one)
    summary = await ut.run(lambda: _Session(ids), _settings(), limit=4)
    assert summary == {
        "dry_run": True, "examined": 4, "upgraded": 1, "would_upgrade": 1, "reconciliation_required": 1,
        "skipped": 1, "last_source_id": str(ids[-1]), "more": True,
    }


@pytest.mark.asyncio
async def test_run_with_nothing_stale_is_empty():
    summary = await ut.run(lambda: _Session(), _settings(), apply=True)
    assert summary["examined"] == 0 and summary["last_source_id"] is None and summary["more"] is False


def _wire(monkeypatch, *, phase="deactivating_old", source_status="active", terms=None):
    calls = []
    source = SimpleNamespace(id=uuid4(), generation=1, status=source_status, type="rss")
    owner = SimpleNamespace(user_id=5, revision=1)
    row = SimpleNamespace(source_generation=1, desired_revision=7, state="saved_not_active")

    async def enabled(*_a, **_k):
        return True

    async def fence(*_a, **_k):
        return "fence"

    async def get_source(*_a, **_k):
        return source

    async def eligible(*_a, **_k):
        if terms:
            raise HTTPException(status_code=409, detail="provider_terms_ineligible")

    async def begin(_s, source_id, revision, target, **_k):
        calls.append(("begin", revision, target))
        return row

    async def advance(*_a, **_k):
        calls.append(("advance",))
        return phase

    async def commit(*_a, **_k):
        calls.append(("commit",))

    monkeypatch.setattr(ut, "module_is_enabled", enabled)
    monkeypatch.setattr(ut, "read_access_fence", fence)
    monkeypatch.setattr(ut.sources, "get_connector_source", get_source)
    monkeypatch.setattr(ut.provider_terms, "require_terms_eligible", eligible)
    monkeypatch.setattr(ut.provisioning, "begin_backend_transition_in_uow", begin)
    monkeypatch.setattr(ut.provisioning, "advance_backend_transition", advance)
    monkeypatch.setattr(ut, "commit_with_replay", commit)
    monkeypatch.setattr(ut, "make_source_change", lambda *a, **k: None)
    monkeypatch.setattr(ut, "registry", SimpleNamespace(SUPPORTED_TYPES={"rss"}))
    return calls, lambda: _Session(scalars=[uuid4(), owner], row=row)


@pytest.mark.asyncio
async def test_apply_runs_the_n8n_transition_saga_and_never_native(monkeypatch):
    calls, factory = _wire(monkeypatch, phase="activating_new")
    assert await ut._upgrade_one(factory, uuid4(), None, apply=True, flag=True) == "upgraded"
    assert calls == [("begin", 7, "n8n"), ("commit",), ("advance",)]


@pytest.mark.asyncio
async def test_dry_run_changes_nothing(monkeypatch):
    calls, factory = _wire(monkeypatch)
    assert await ut._upgrade_one(factory, uuid4(), None, apply=False, flag=True) == "would_upgrade"
    assert calls == []


@pytest.mark.asyncio
async def test_ambiguous_stop_is_reconciliation_required(monkeypatch):
    _calls, factory = _wire(monkeypatch, phase="reconciliation_required")
    assert await ut._upgrade_one(factory, uuid4(), None, apply=True, flag=True) == "reconciliation_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"source_status": "paused"}, {"terms": True}])
async def test_ineligible_sources_are_skipped_untouched(monkeypatch, kwargs):
    calls, factory = _wire(monkeypatch, **kwargs)
    assert await ut._upgrade_one(factory, uuid4(), None, apply=True, flag=True) == "skipped"
    assert calls == []


def test_transition_sweep_cursor_key_is_registered():
    from modules.connectors import worker

    assert "connectors_transitions" in worker._CURSOR_KEYS  # an unregistered key made every sweep raise KeyError
