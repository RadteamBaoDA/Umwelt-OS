"""Built-in one-toggle sources: eligibility, idempotent enable sequence, operator evidence, disable (fakes only)."""

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import builtins
from modules.connectors.backends import FIXED_SCOPE_PROVIDERS, PROVIDER_SOURCE_TYPES
from modules.connectors.provider_specs import (
    BUILTIN_PROVIDERS,
    FREE_PROVIDER_SPECS,
    get_provider_spec,
)
from modules.sources.schemas import ConnectorSource

WORKSPACE = uuid4()
SCOPE = SimpleNamespace(workspace_id=WORKSPACE, user_id=1, role="owner")


def _request(operator: bool = True) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))),
        state=SimpleNamespace(auth_session=SimpleNamespace(owner_id=1 if operator else 2)))


def _source(provider: str, status: str = "active", generation: int = 1) -> ConnectorSource:
    return ConnectorSource(
        id=uuid4(), workspace_id=WORKSPACE, type=PROVIDER_SOURCE_TYPES[provider], status=status,
        generation=generation, configuration={}, local_only=False, provider=provider)


class _World:
    """Patch every collaborator of the toggle and record call order; rows evolve like the real helpers."""

    def __init__(self, provider: str, *, eligible: bool = True) -> None:
        self.provider, self.calls = provider, []
        self.source: ConnectorSource | None = None
        self.row: Any = None
        self.created_name: str | None = None
        self.eligible = eligible
        self.stack = ExitStack()

    def _rec(self, name: str, result: Any = None) -> AsyncMock:
        async def run(*a: Any, **k: Any) -> Any:
            self.calls.append(name)
            return result(*a, **k) if callable(result) else result
        return AsyncMock(side_effect=run)

    def __enter__(self) -> Self:
        def orm() -> Any:
            return SimpleNamespace(id=self.source.id, status=self.source.status) if self.source else None

        def created(*a: Any, **_k: Any) -> Any:
            self.created_name = a[1].name
            self.source = _source(self.provider)
            return orm()

        def resumed(*_a: Any, **_k: Any) -> Any:
            assert self.source is not None
            self.source = self.source.model_copy(update={"status": "active", "generation": self.source.generation + 1})
            return orm()

        def saved(_s: Any, source: Any, _rev: int, _sc: Any, desired: Any, **_k: Any) -> Any:
            self.row = SimpleNamespace(
                state="saved_not_active", desired_enabled=False, applied_revision=0, desired_revision=1,
                source_generation=source.generation, desired_configuration=desired)
            # Persist like the real save so a later enable reads the owner's settings back.
            self.source = source.model_copy(update={"configuration": dict(desired)})
            return source, self.row

        def activated(*_a: Any, **_k: Any) -> Any:
            self.row.state, self.row.desired_enabled = "active", True
            self.row.applied_revision = self.row.desired_revision

        def terms(*_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(terms_revision=2, eligible=self.eligible)

        def paused(*_a: Any, **_k: Any) -> Any:
            assert self.source is not None
            self.source = self.source.model_copy(update={"status": "paused", "generation": self.source.generation + 1})
            return self.source

        patches = {
            "list_provider_sources": (builtins.sources, self._rec("list", lambda *a, **k: [orm()] if self.source else [])),
            "list_provider_connector_sources": (
                builtins.sources, self._rec("list_batch", lambda *a, **k: [self.source] if self.source else [])),
            "create_source": (builtins.sources, self._rec("create", created)),
            "update_source": (builtins.sources, self._rec("resume", resumed)),
            "get_connector_source": (builtins.sources, self._rec("get", lambda *a, **k: self.source)),
            "pause_source_for_connector": (builtins.sources, self._rec("pause", paused)),
            "activation_status": (builtins.provisioning, self._rec("row", lambda *a, **k: self.row)),
            "activation_statuses": (builtins.provisioning, self._rec(
                "rows_batch", lambda _s, ids, **k: {i: self.row for i in ids})),
            "acknowledge_terms": (builtins.provider_terms, self._rec("ack", terms)),
            "record_operator_review": (builtins.provider_terms, self._rec("review", terms)),
            "clear_collection_block": (builtins.scheduler, self._rec("clear")),
            "save_connector_configuration": (builtins, self._rec("save", saved)),
            "activate_native": (builtins, self._rec("activate", activated)),
            "_owner_access": (builtins, self._rec("fence", "fence")),
            "commit_with_replay": (builtins, self._rec("commit")),
        }
        for attr, (target, mock) in patches.items():
            self.stack.enter_context(patch.object(target, attr, mock))
        self.stack.enter_context(patch.object(builtins, "make_source_change", MagicMock()))
        return self

    def __exit__(self, *exc: object) -> None:
        self.stack.close()


def _session() -> Any:
    return SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())


async def _enable(provider: str, use: str = "personal", operator: bool = True) -> Any:
    return await builtins.enable_builtin(
        provider, _session(), _request(operator), SCOPE, builtins.BuiltinEnableRequest(declared_use=use))  # type: ignore[arg-type]


def test_builtin_set_is_derived_fixed_scope_keyless_and_implemented() -> None:
    assert BUILTIN_PROVIDERS and BUILTIN_PROVIDERS <= FIXED_SCOPE_PROVIDERS
    assert {"google_news", "sec_edgar", "openalex", "alpha_vantage", "coingecko"}.isdisjoint(BUILTIN_PROVIDERS)
    assert {s.id for s in FREE_PROVIDER_SPECS if s.category} >= BUILTIN_PROVIDERS


@pytest.mark.parametrize("provider", ["google_news", "coingecko", "nope"])
async def test_scoped_keyed_or_unknown_provider_is_404(provider: str) -> None:
    with pytest.raises(HTTPException) as caught:
        await _enable(provider)
    assert caught.value.status_code == 404


async def test_enable_creates_source_saves_acknowledges_and_activates_in_order() -> None:
    with _World("world_bank") as world:
        read = await _enable("world_bank")
    order = [c for c in world.calls if c in {"create", "save", "ack", "review", "activate"}]
    assert order == ["create", "save", "ack", "activate"]  # open-class: no operator review
    assert read.enabled and world.source is not None and read.source_id == world.source.id
    assert read.category == "economy"
    assert world.row.desired_configuration["history_mode"] == "returned_snapshot"
    assert world.row.desired_configuration["timeout_seconds"] == 30


async def test_enable_is_idempotent_when_already_active() -> None:
    with _World("world_bank") as world:
        await _enable("world_bank")
        world.calls.clear()
        again = await _enable("world_bank")
    assert again.enabled
    assert not {"create", "save", "activate", "review"} & set(world.calls)


async def test_disable_then_enable_reuses_source_and_resaves_new_generation() -> None:
    with _World("world_bank") as world:
        first = await _enable("world_bank")
        off = await builtins.disable_builtin("world_bank", _session(), _request(), SCOPE)  # type: ignore[arg-type]
        assert not off.enabled and off.source_id == first.source_id
        world.calls.clear()
        again = await _enable("world_bank")
    assert again.enabled and again.source_id == first.source_id
    assert "create" not in world.calls and "resume" in world.calls and "save" in world.calls


async def test_review_class_needs_instance_operator_before_anything_is_created() -> None:
    with _World("bbc_world") as world, pytest.raises(HTTPException) as caught:
        await _enable("bbc_world", operator=False)
    assert (caught.value.status_code, caught.value.detail) == (403, "operator_required")
    assert "create" not in world.calls


async def test_review_class_records_operator_evidence_for_the_exact_terms_version() -> None:
    with _World("bbc_world", eligible=False) as world:
        await _enable("bbc_world", use="noncommercial")
        body = builtins.provider_terms.record_operator_review.call_args.kwargs["body"]  # type: ignore[attr-defined]
    assert "review" in world.calls and world.calls.index("review") < world.calls.index("activate")
    spec = get_provider_spec("bbc_world")
    assert spec and body.reviewed_terms_version == spec.checked_on and body.allowed_use == "noncommercial"
    assert body.evidence_ref.startswith("builtin-toggle:bbc_world:") and body.evidence_ref.endswith(f"@{spec.checked_on}")


async def test_commercial_use_on_personal_only_provider_is_409_without_side_effects() -> None:
    with _World("coinpaprika") as world, pytest.raises(HTTPException) as caught:
        await _enable("coinpaprika", use="commercial")
    assert (caught.value.status_code, caught.value.detail) == (409, "provider_terms_ineligible")
    assert "create" not in world.calls


async def test_list_reports_every_builtin_with_off_state_before_first_enable() -> None:
    with _World("world_bank"):
        rows = await builtins.list_builtins(_session(), _request(), SCOPE)  # type: ignore[arg-type]
    assert {r.id for r in rows} == BUILTIN_PROVIDERS
    assert all(r.source_id is None and not r.enabled for r in rows)
    assert all(r.model_dump()["data_shapes"] for r in rows)  # serialized row carries data shapes


def test_every_builtin_schedule_is_accepted_by_connector_config() -> None:
    """Each built-in's default interval must satisfy ConnectorConfig; otherwise enable fails after the Source is created."""
    from modules.connectors.public import ConnectorConfig

    for provider in sorted(BUILTIN_PROVIDERS):
        spec = get_provider_spec(provider)
        assert spec is not None
        # Raises pydantic ValidationError for an interval outside {15, 30, 60, 360, 1440}.
        ConnectorConfig(
            timeout_seconds=30, history_mode="returned_snapshot", schedule_interval_minutes=spec.default_interval_minutes)


@pytest.mark.parametrize("provider", sorted(BUILTIN_PROVIDERS))
async def test_every_builtin_enables_end_to_end(provider: str) -> None:
    """Audit: each built-in reaches an active state through the same enable sequence (review-class via the instance operator)."""
    with _World(provider) as world:
        read = await _enable(provider)
    assert read.enabled, f"{provider} did not activate"
    assert world.source is not None and read.source_id == world.source.id



class _ExpiringRow:
    """Provisioning-row stand-in that fails loudly when read after a rollback expired it, like a real ORM row under asyncio."""

    def __init__(self, **values: Any) -> None:
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "expired", False)

    def __getattr__(self, name: str) -> Any:
        if object.__getattribute__(self, "expired"):
            raise AssertionError(f"read expired ORM attribute {name!r} after rollback")
        return object.__getattribute__(self, "_values")[name]

    def __setattr__(self, name: str, value: Any) -> None:
        if object.__getattribute__(self, "expired"):
            raise AssertionError(f"wrote expired ORM attribute {name!r} after rollback")
        object.__getattribute__(self, "_values")[name] = value


@pytest.mark.parametrize("stale_row", [False, True])
async def test_enable_never_reads_provisioning_row_after_rollback(stale_row: bool) -> None:
    """Regression for MissingGreenlet: each rollback expires loaded rows, so the toggle must use snapshots only."""
    with _World("bbc_world") as world:
        if stale_row:
            await _enable("bbc_world")  # leaves a provisioning row behind
            world.row.desired_configuration = {}  # force step 3 to re-save from an existing row
            world.row.state = "saved_not_active"
        loaded: list[_ExpiringRow] = []
        listed = builtins.provisioning.activation_status

        async def fresh(*a: Any, **k: Any) -> Any:
            row = await listed(*a, **k)
            if row is None:
                return None
            wrapped = _ExpiringRow(**vars(row))
            loaded.append(wrapped)
            return wrapped

        async def rollback() -> None:
            for row in loaded:
                object.__setattr__(row, "expired", True)

        session = _session()
        session.rollback = AsyncMock(side_effect=rollback)
        with patch.object(builtins.provisioning, "activation_status", AsyncMock(side_effect=fresh)):
            read = await builtins.enable_builtin(
                "bbc_world", session, _request(), SCOPE, builtins.BuiltinEnableRequest())  # type: ignore[arg-type]
    assert read.enabled


async def test_enable_honors_explicit_realtime_mode_override() -> None:
    """Explicit realtime_mode on BuiltinEnableRequest persists into ConnectorConfig and overrides default tier."""
    with _World("world_bank") as world:
        read = await builtins.enable_builtin(
            "world_bank",
            _session(),
            _request(),
            SCOPE,  # type: ignore[arg-type]
            builtins.BuiltinEnableRequest(declared_use="personal", realtime_mode="realtime"),
        )
    assert read.enabled
    assert world.row.desired_configuration["realtime_mode"] == "realtime"



_RAW = builtins.BuiltinEnableRequest


async def test_resume_keeps_saved_interval_and_filters() -> None:
    """Options saved on first enable survive disable -> plain re-enable (Resume)."""
    with _World("tuoitre_news") as world:
        await builtins.enable_builtin(
            "tuoitre_news", _session(), _request(), SCOPE,  # type: ignore[arg-type]
            _RAW(schedule_interval_minutes=60, max_items=20, include_keywords=("giá vàng",)))
        await builtins.disable_builtin("tuoitre_news", _session(), _request(), SCOPE)  # type: ignore[arg-type]
        await _enable("tuoitre_news")
    cfg = world.row.desired_configuration
    assert cfg["schedule_interval_minutes"] == 60 and cfg["max_items"] == 20 and cfg["include_keywords"] == ["giá vàng"]


async def test_enable_uses_spec_default_when_nothing_saved() -> None:
    """A default enable sends only the spec interval and no filter keys (no spurious revision bump)."""
    with _World("tuoitre_news") as world:
        await _enable("tuoitre_news")
    cfg = world.row.desired_configuration
    assert cfg["schedule_interval_minutes"] == 30
    assert not {"max_items", "include_keywords", "exclude_keywords", "max_age_days"} & cfg.keys()


async def test_enable_names_new_source() -> None:
    """name applies to the created Source only."""
    with _World("tuoitre_news") as world:
        await builtins.enable_builtin(
            "tuoitre_news", _session(), _request(), SCOPE, _RAW(name="TT kinh tế"))  # type: ignore[arg-type]
    assert world.created_name == "TT kinh tế"


async def test_filter_fields_on_measurement_builtin_are_422() -> None:
    """Filters are refused for providers outside INCREMENTAL_PROVIDERS."""
    with _World("binance"), pytest.raises(HTTPException) as exc:
        await builtins.enable_builtin("binance", _session(), _request(), SCOPE, _RAW(max_items=5))  # type: ignore[arg-type]
    assert exc.value.status_code == 422


async def test_explicit_null_clears_saved_option_and_absent_keeps_it() -> None:
    """M5: a field sent as null clears the saved value; an absent field keeps it."""
    with _World("tuoitre_news") as world:
        await builtins.enable_builtin(
            "tuoitre_news", _session(), _request(), SCOPE, _RAW(max_items=20, max_age_days=3))  # type: ignore[arg-type]
        await builtins.enable_builtin(
            "tuoitre_news", _session(), _request(), SCOPE, _RAW(max_items=None))  # type: ignore[arg-type]
    cfg = world.row.desired_configuration
    assert "max_items" not in cfg and cfg["max_age_days"] == 3


async def test_list_is_batched_and_matches_per_provider_state() -> None:
    """Many Sources: one batch call each, no per-provider reads, oldest-per-provider, same result as _state."""
    specs = [sp for sp in FREE_PROVIDER_SPECS if sp.id in BUILTIN_PROVIDERS]
    on, stale, off = specs[0], specs[1], specs[2]
    fresh = {on.id: [_source(on.id), _source(on.id)], stale.id: [_source(stale.id, status="paused")]}
    batch_sources = [src for group in fresh.values() for src in group]
    active = SimpleNamespace(state="active", desired_enabled=True, applied_revision=2, desired_revision=2, source_generation=1)
    rows = {fresh[on.id][0].id: active, fresh[stale.id][0].id: active}
    list_batch = AsyncMock(return_value=batch_sources)
    status_batch = AsyncMock(side_effect=lambda _s, ids, **k: {i: rows.get(i) for i in ids})
    per_provider = [AsyncMock() for _ in range(3)]
    with patch.object(builtins, "_owner_access", AsyncMock()),             patch.object(builtins.sources, "list_provider_connector_sources", list_batch),             patch.object(builtins.provisioning, "activation_statuses", status_batch),             patch.object(builtins.sources, "list_provider_sources", per_provider[0]),             patch.object(builtins.sources, "get_connector_source", per_provider[1]),             patch.object(builtins.provisioning, "activation_status", per_provider[2]):
        result = {r.id: r for r in await builtins.list_builtins(_session(), _request(), SCOPE)}  # type: ignore[arg-type]
    assert list_batch.await_count == 1 and status_batch.await_count == 1
    assert all(m.await_count == 0 for m in per_provider)
    assert status_batch.await_args.args[1] == {fresh[on.id][0].id, fresh[stale.id][0].id}  # oldest only
    assert result[on.id].source_id == fresh[on.id][0].id and result[on.id].enabled
    assert result[stale.id].source_id == fresh[stale.id][0].id and not result[stale.id].enabled  # paused Source
    assert result[off.id].source_id is None and not result[off.id].enabled
    # Parity with the per-provider path used by enable/disable.
    with patch.object(builtins.sources, "list_provider_sources", AsyncMock(side_effect=lambda _s, p, **k: fresh.get(next(iter(p)), []))),             patch.object(builtins.sources, "get_connector_source", AsyncMock(side_effect=lambda _s, i, **k: next(x for x in batch_sources if x.id == i))),             patch.object(builtins.provisioning, "activation_status", AsyncMock(side_effect=lambda _s, i, **k: rows.get(i))):
        for spec in (on, stale, off):
            single = await builtins._state(_session(), spec, scope=SCOPE, gate=False)  # type: ignore[arg-type]
            assert (single.source_id, single.enabled) == (result[spec.id].source_id, result[spec.id].enabled)


async def test_activation_statuses_maps_rows_and_404s_on_invisible_source() -> None:
    """Batch mirrors activation_status: None for no row, 404 when any id is not visible in scope."""
    from modules.connectors import provisioning
    a, b = uuid4(), uuid4()
    row = SimpleNamespace(source_id=a)
    session = SimpleNamespace(scalars=AsyncMock(return_value=[row]))
    with patch.object(provisioning, "_connector_access", AsyncMock()), \
            patch.object(provisioning.sources, "get_connector_sources", AsyncMock(return_value={a: 1, b: 2})):
        got = await provisioning.activation_statuses(session, frozenset({a, b}), scope=SCOPE, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert got == {a: row, b: None} and session.scalars.await_count == 1
    with patch.object(provisioning, "_connector_access", AsyncMock()), \
            patch.object(provisioning.sources, "get_connector_sources", AsyncMock(return_value={a: 1})), \
            pytest.raises(HTTPException) as exc:
        await provisioning.activation_statuses(session, frozenset({a, b}), scope=SCOPE, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert exc.value.status_code == 404
