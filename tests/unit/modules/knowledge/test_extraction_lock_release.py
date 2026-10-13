"""Timeline/entity extraction release DB locks before provider I/O and re-lock-and-compare before publication."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.model_gateway.client import PrivacyPolicyDenied
from modules.knowledge.entities import worker as entities_worker
from modules.timeline import worker as timeline_worker

WS = uuid4()
DOC, SRC = uuid4(), uuid4()


class _Factory:
    """Session factory yielding a fresh mock session per use so outer/inner sessions are distinguishable."""

    def __init__(self) -> None:
        self.sessions: list[MagicMock] = []
        self._current: MagicMock | None = None

    def __call__(self) -> "_Factory":
        session = MagicMock()
        session.rollback = AsyncMock()
        session.commit = AsyncMock()
        session.scalar = AsyncMock(return_value=WS)
        self.sessions.append(session)
        self._current = session
        return self

    async def __aenter__(self) -> MagicMock:
        assert self._current is not None
        return self._current

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Gateway:
    """Stand-in ModelGateway exposing the before_send hook it was built with."""

    instance: "_Gateway | None" = None

    def __init__(self, **kwargs: Any) -> None:
        self.before_send = kwargs["before_send"]
        _Gateway.instance = self

    async def structured(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        return {}


def _harness(monkeypatch: pytest.MonkeyPatch, name: str) -> SimpleNamespace:
    """Patch every collaborator of one worker so lock/inference ordering can be observed."""
    timeline = name == "timeline"
    mod: Any = timeline_worker if timeline else entities_worker
    target = mod.timeline if timeline else mod.entities
    factory = _Factory()
    mapping = SimpleNamespace(model="m")
    alias = mod.ALIAS if timeline else mod.EXTRACTION_ALIAS
    config = SimpleNamespace(
        configuration_revision=1, gateway_identity="g", endpoint_destination_id="d", aliases={alias: mapping},
        gateway_base_url="u", gateway_api_key="k", request_timeout_seconds=10, endpoint_allowed_cidrs=(),
        allow_public_providers=False, system_prompts={}, workspace_id=WS, actor_user_id=1, membership_revision=1,
        endpoint_policy_denied=False, gateway_credential_configured=True,
        privacy=SimpleNamespace(allow_remote_reasoning=False, reasoning_destinations=()),
    )
    ready = SimpleNamespace(
        document_id=DOC, source_id=SRC, source_generation=1, raw_uri="raw", mime_type="text/plain", local_only=False,
    )
    data = SimpleNamespace(document_id=DOC, source_id=SRC, source_generation=1, chunks=[], observed_at=None)
    fresh = SimpleNamespace(id=SRC, status="active", generation=1, local_only=False)
    m = SimpleNamespace(
        factory=factory, ready=ready, fence=["f1"], ready_sessions=[],
        lock_source=AsyncMock(return_value=fresh),
        set_error=AsyncMock(), publish=AsyncMock(return_value=False), commit_replay=AsyncMock(),
    )

    async def fence(*_a: Any, **_k: Any) -> str:
        return str(m.fence[0])

    async def get_ready(session: Any, *_a: Any, **_k: Any) -> Any:
        m.ready_sessions.append(session)
        return ready

    scope = SimpleNamespace(workspace_id=WS)
    monkeypatch.setattr(mod, "_workspace_job_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(mod.settings_public, "module_is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(mod.settings_public, "get_ai_execution_config", AsyncMock(return_value=config))
    monkeypatch.setattr(mod.settings_public, "derive_for_alias", lambda cfg, _alias: cfg)
    monkeypatch.setattr(target, "claim_extraction_work",
                        AsyncMock(return_value=SimpleNamespace(document_version_id=uuid4(), source_generation=1)))
    monkeypatch.setattr(target, "set_extraction_work_error", m.set_error)
    monkeypatch.setattr(mod.workspaces, "read_access_fence", fence)
    monkeypatch.setattr(mod.documents, "get_ready_version_ref", get_ready)
    monkeypatch.setattr(mod.documents, "lock_document_for_extraction", AsyncMock(return_value=True))
    monkeypatch.setattr(mod.documents, "read_extraction_input", AsyncMock(return_value=data))
    monkeypatch.setattr(mod.sources, "lock_source", m.lock_source)
    monkeypatch.setattr(mod, "_dependency_snapshot", AsyncMock(return_value=("fp", True)))
    monkeypatch.setattr(mod, "ModelGateway", _Gateway)
    monkeypatch.setattr(mod, "extraction_messages", lambda *_a, **_k: [])
    monkeypatch.setattr(mod, "response_schema", dict)
    monkeypatch.setattr(mod, "commit_with_replay", m.commit_replay)
    if timeline:
        monkeypatch.setattr(mod, "_allowed", lambda *_a: True)
        monkeypatch.setattr(mod, "_policy", lambda *_a: None)
        monkeypatch.setattr(mod.entities, "list_version_membership_refs", AsyncMock(return_value=[]))
        monkeypatch.setattr(mod, "response_content", lambda _r: ([], "m"))
        monkeypatch.setattr(target, "publish_extracted_events", m.publish)
    else:
        monkeypatch.setattr(mod, "_policy_allows_extraction", lambda *_a: True)
        monkeypatch.setattr(mod, "may_send", lambda *_a: True)
        monkeypatch.setattr(mod, "RequestPolicy", lambda **_k: None)
        monkeypatch.setattr(mod, "response_content",
                            lambda _r: (SimpleNamespace(entities=[], relationships=[]), "m", None))
        monkeypatch.setattr(target, "get_document_correction_decisions", AsyncMock(return_value={}))
        monkeypatch.setattr(target, "finish_extraction_work", m.publish)
    entry = (timeline_worker.process_timeline_extraction_work if timeline
             else entities_worker.process_entity_extraction_work)
    entry = getattr(entry, "__wrapped__", entry)
    ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=False), "redis": MagicMock()}

    async def run() -> None:
        await entry(ctx, str(uuid4()))

    m.run = run
    m.outer = lambda: factory.sessions[1]  # sessions[0] is the admission session
    return m


NAMES = ["timeline", "entities"]


@pytest.mark.parametrize("name", NAMES)
async def test_rollback_precedes_inference_and_before_send_uses_own_session(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    m = _harness(monkeypatch, name)
    rollbacks_at_inference: list[int] = []

    async def structured(*_a: Any, **_k: Any) -> dict[str, Any]:
        rollbacks_at_inference.append(m.outer().rollback.await_count)
        return {}

    monkeypatch.setattr(_Gateway, "structured", structured)
    await m.run()
    assert rollbacks_at_inference
    assert rollbacks_at_inference[0] >= 1
    seen = len(m.ready_sessions)
    assert _Gateway.instance is not None
    await _Gateway.instance.before_send()
    assert all(s is not m.outer() for s in m.ready_sessions[seen:])
    assert len(m.ready_sessions) > seen


@pytest.mark.parametrize("name", NAMES)
async def test_relock_generation_change_aborts_without_publish(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    m = _harness(monkeypatch, name)
    stale = SimpleNamespace(id=SRC, status="active", generation=2, local_only=False)
    m.lock_source.side_effect = [m.lock_source.return_value, stale]
    await m.run()
    m.publish.assert_not_awaited()
    m.commit_replay.assert_not_awaited()
    code = m.set_error.await_args.args[3]
    assert code == ("valueerror" if name == "timeline" else "document_unavailable")


@pytest.mark.parametrize("name", NAMES)
async def test_before_send_access_fence_change_is_409(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    m = _harness(monkeypatch, name)
    await m.run()
    m.fence[0] = "f2"
    assert _Gateway.instance is not None
    with pytest.raises(HTTPException) as exc:
        await _Gateway.instance.before_send()
    assert exc.value.status_code == 409


@pytest.mark.parametrize("name", NAMES)
async def test_before_send_local_only_is_denied(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    m = _harness(monkeypatch, name)
    await m.run()
    m.ready.local_only = True
    assert _Gateway.instance is not None
    with pytest.raises(PrivacyPolicyDenied):
        await _Gateway.instance.before_send()


@pytest.mark.parametrize("name", NAMES)
async def test_lost_lease_rolls_back_without_commit(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    m = _harness(monkeypatch, name)
    await m.run()
    m.publish.assert_awaited_once()
    m.commit_replay.assert_not_awaited()
    assert m.outer().rollback.await_count >= 2  # pre-inference release plus the lost-lease rollback
