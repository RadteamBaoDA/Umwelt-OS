"""Three-phase brief generation: snapshot, per-attempt egress fence, publish-or-discard (P14 T2).

The session, locks, facts and gateway are fakes that log every step, so the tests assert the phase
order, the canonical lock order, and that no transaction is open while the model response is awaited.
"""

import asyncio
import copy
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.dashboard import briefs, context
from modules.dashboard.models import DailyBrief

DAY = date(2026, 10, 7)
TZ = "Asia/Ho_Chi_Minh"
SOURCE = str(uuid4())


def _fact() -> dict[str, Any]:
    return {
        "kind": "stories", "id": str(uuid4()), "title": "Story", "detail": "", "source_ids": [SOURCE],
        "_lineage_status": "supported", "_fact_hash": "a" * 64,
        "_supports": [{"document_id": str(uuid4()), "document_version_id": str(uuid4()),
                       "chunk_id": str(uuid4()), "source_id": SOURCE}],
    }


class _Session:
    def __init__(self, env: "_Env") -> None:
        self.env, self.tx, self.added = env, False, []

    def in_transaction(self) -> bool:
        return self.tx

    async def rollback(self) -> None:
        self.env.log.append("rollback")
        self.tx, self.added = False, []

    async def scalar(self, _statement: object) -> Any:
        self.tx = True
        return self.env.latest

    async def execute(self, _statement: object, _params: object = None) -> None:
        self.tx = True
        self.env.log.append("advisory")

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def add_all(self, objs: list[object]) -> None:
        self.added.extend(objs)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, DailyBrief) and obj.id is None:
                obj.id, obj.status, obj.generated_at = uuid4(), "current", datetime.now(UTC)


class _Env:
    """Mutable world the fakes read: facts, purge state, settings, latest revision and call hooks."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.facts = [_fact()]
        self.purged = False
        self.latest: Any = None
        self.revision = 1
        self.may_send = True
        self.attempts = 1
        self.tx_during_wait: list[bool] = []
        self.on_wait: Callable[[int], None] | None = None
        self.on_config: Callable[[int], None] | None = None
        self.config_calls = 0
        self.committed: list[DailyBrief] = []
        self.session = _Session(self)

    def config(self) -> SimpleNamespace:
        return SimpleNamespace(
            brief_alias="brief", aliases={"brief": "mapping"}, endpoint_destination_id="dest",
            privacy=SimpleNamespace(allow_remote_reasoning=True, reasoning_destinations=["dest"]),
            configuration_revision=self.revision, omniroute_base_url="http://gw", omniroute_api_key="k",
            request_timeout_seconds=5, gateway_identity="g", endpoint_allowed_cidrs=[],
        )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    world = _Env()

    async def widgets(session: _Session, *_args: object, **_kwargs: object) -> list[object]:
        session.tx = True
        world.log.append("widgets")
        return []

    async def facts(session: _Session, _widgets: object, *, owner_id: int, lock_events: bool = False) -> list:
        session.tx = True
        world.log.append("facts+events" if lock_events else "facts")
        return copy.deepcopy(world.facts)

    async def lock(session: _Session, _facts: object, *, extra_brief_ids: object = ()) -> None:
        session.tx = True
        world.log.append("lock")
        if world.purged:
            raise briefs.BriefUnavailable("A cited source is no longer eligible")

    async def config(session: _Session, *_args: object) -> SimpleNamespace:
        session.tx = True
        world.config_calls += 1
        world.log.append("config")
        if world.on_config is not None:
            world.on_config(world.config_calls)
        return world.config()

    async def emit(*_args: object) -> None:
        world.log.append("notify")

    async def commit(session: _Session, _changes: object) -> None:
        world.log.append("commit")
        world.committed.extend(obj for obj in session.added if isinstance(obj, DailyBrief))
        session.tx = False

    class Gateway:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def chat(self, *_args: object, before_send: Any, after_send: Any, **_kwargs: object) -> Any:
            # Mirrors ModelGateway._request: before_send is outside the after_send finally.
            for attempt in range(1, world.attempts + 1):
                await before_send()
                try:
                    world.log.append("egress")
                    await after_send()  # transport body-written hook
                    world.tx_during_wait.append(world.session.in_transaction())
                    world.log.append("wait")
                    if world.on_wait is not None:
                        world.on_wait(attempt)
                finally:
                    await after_send()
                if attempt < world.attempts:
                    continue  # retryable failure after the body was sent
                return {"choices": [{"message": {"content": "Today [1]."}}]}
            raise AssertionError("unreachable")

    monkeypatch.setattr(context, "build_daily_widgets", widgets)
    monkeypatch.setattr(briefs, "_facts", facts)
    monkeypatch.setattr(briefs, "_lock_fact_dependencies", lock)
    monkeypatch.setattr(briefs.settings_public, "get_ai_execution_config", config)
    monkeypatch.setattr(briefs.notifications, "emit", emit)
    monkeypatch.setattr(briefs, "commit_with_replay", commit)
    monkeypatch.setattr(briefs, "ModelGateway", Gateway)
    monkeypatch.setattr(briefs, "may_send", lambda *_args: world.may_send)
    return world


async def _generate(world: _Env, *, guard: Any = None, force: bool = False) -> Any:
    return await briefs.generate_brief(
        world.session, 1, DAY, TZ, settings=SimpleNamespace(), redis=SimpleNamespace(), force=force,
        publish_guard=guard,
    )


def _guard(world: _Env, results: dict[str, bool] | None = None) -> Callable[[Any], Any]:
    async def guard(session: _Session) -> bool:
        session.tx = True
        phase = "B" if "egress" not in world.log else "C"
        world.log.append(f"guard{phase}")
        return (results or {}).get(phase, True)

    return guard


async def test_phases_run_in_order_with_canonical_lock_order_and_no_lock_during_model_wait(env: _Env) -> None:
    brief = await _generate(env, guard=_guard(env))

    assert env.log == [
        # A: snapshot under the locks, then roll back
        "widgets", "facts", "lock", "widgets", "facts+events", "advisory", "config", "rollback",
        # B: guard -> sources/documents -> events, settings recheck, egress, release at body write
        "guardB", "lock", "widgets", "facts+events", "config", "egress", "rollback", "wait",
        # C: guard -> sources/documents -> events -> day advisory, publish
        "guardC", "lock", "widgets", "facts+events", "advisory", "notify", "commit",
    ]
    assert env.tx_during_wait == [False]
    assert brief.revision == 1 and len(env.committed) == 1


@pytest.mark.parametrize("change", ["purge", "local_only"])
async def test_mid_call_purge_or_local_only_flip_discards_the_output(env: _Env, change: str) -> None:
    def mutate(_attempt: int) -> None:
        if change == "purge":
            env.purged = True
        else:
            env.facts = []  # _facts drops facts whose source became local-only

    env.on_wait = mutate
    with pytest.raises(briefs.BriefUnavailable):
        await _generate(env)
    assert env.committed == [] and "commit" not in env.log and "notify" not in env.log
    assert not env.session.in_transaction()


async def test_fingerprint_mismatch_at_before_send_aborts_without_egress(env: _Env) -> None:
    def mutate(call: int) -> None:
        if call == 1:  # end of Phase A: facts change before the first attempt
            env.facts = [{**env.facts[0], "title": "Edited"}]

    env.on_config = mutate
    with pytest.raises(briefs.BriefUnavailable, match="changed during generation"):
        await _generate(env)
    assert "egress" not in env.log and env.committed == []
    assert not env.session.in_transaction()


async def test_retry_refences_and_a_fence_failure_on_attempt_two_releases_the_locks(env: _Env) -> None:
    env.attempts = 2

    def purge_after_first(attempt: int) -> None:
        if attempt == 1:
            env.purged = True

    env.on_wait = purge_after_first
    with pytest.raises(briefs.BriefUnavailable):
        await _generate(env)
    assert env.log.count("egress") == 1
    assert not env.session.in_transaction() and env.committed == []


@pytest.mark.parametrize("exc", [asyncio.CancelledError, KeyboardInterrupt])
async def test_fence_rolls_back_on_base_exception_in_before_send(env: _Env, exc: type[BaseException]) -> None:
    async def guard(session: _Session) -> bool:
        if "rollback" in env.log:  # Phase B
            session.tx = True
            raise exc
        return True

    with pytest.raises(exc):
        await _generate(env, guard=guard)
    assert "egress" not in env.log and not env.session.in_transaction()


async def test_fence_rolls_back_when_the_policy_check_raises(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> bool:
        raise RuntimeError("policy evaluation failed")

    monkeypatch.setattr(briefs, "may_send", broken)
    with pytest.raises(RuntimeError):
        await _generate(env)
    assert "egress" not in env.log and not env.session.in_transaction()


async def test_settings_change_before_egress_is_denied(env: _Env) -> None:
    def bump(call: int) -> None:
        if call == 2:
            env.revision = 2  # the Phase B re-read sees a new configuration revision

    env.on_config = bump
    with pytest.raises(briefs.BriefUnavailable):
        await _generate(env)
    assert "egress" not in env.log and not env.session.in_transaction()


async def test_remote_consent_withdrawn_before_egress_is_denied(env: _Env) -> None:
    def withdraw(call: int) -> None:
        if call == 1:
            env.may_send = False

    env.on_config = withdraw
    with pytest.raises(briefs.BriefUnavailable):
        await _generate(env)
    assert "egress" not in env.log


async def test_concurrent_revision_is_not_reused_and_ours_is_numbered_after_it(env: _Env) -> None:
    def concurrent_publish(_attempt: int) -> None:
        # Another generation committed revision 3 for the same fingerprint while we waited.
        env.latest = SimpleNamespace(id=uuid4(), revision=3, input_fingerprint="same", status="current")

    env.on_wait = concurrent_publish
    brief = await _generate(env)
    assert brief.revision == 4 and [row.revision for row in env.committed] == [4]


@pytest.mark.parametrize("phase", ["B", "C"])
async def test_publish_guard_is_rechecked_before_egress_and_before_publish(env: _Env, phase: str) -> None:
    with pytest.raises(briefs.BriefUnavailable, match="trigger evidence"):
        await _generate(env, guard=_guard(env, {phase: False}))
    assert ("egress" in env.log) is (phase == "C")
    assert env.committed == [] and not env.session.in_transaction()
    # The guard always precedes the brief's own locks in its phase.
    assert env.log[env.log.index(f"guard{phase}") - 1] in {"rollback", "wait"}
