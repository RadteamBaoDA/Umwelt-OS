"""P1 phase-review regressions: stale call shapes in Chat, model-gateway workers and entity export."""

import ast
import importlib
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.model_gateway.client import ModelGateway
from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelMapping,
    PrivacySettings,
    RequestPolicy,
)
from core.workspaces.schemas import InternalJobScope, WorkspaceContext

ROOT = Path(__file__).resolve().parents[2]
OWNER = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=2)
ALIASES = {
    "documents_public": "modules.knowledge.documents.public",
    "entities_public": "modules.knowledge.entities.public",
    "relationships_public": "modules.knowledge.relationships.public",
    "search_public": "modules.search.public",
    "sources_public": "modules.sources.public",
    "timeline_public": "modules.timeline.public",
    "settings_public": "modules.settings.public",
}


def _required_kwonly(fn: object) -> set[str]:
    return {
        n for n, p in inspect.signature(fn).parameters.items()  # type: ignore[arg-type]
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    }


@pytest.mark.parametrize("name", ["public", "retrieval", "worker"])
def test_chat_calls_pass_required_scope_keywords(name):
    """Every Chat call into a W2-converted owner passes its required keyword-only scope arguments."""
    path = ROOT / "modules" / "chat" / f"{name}.py"
    missing = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id in ALIASES
        ):
            continue
        fn = getattr(importlib.import_module(ALIASES[node.func.value.id]), node.func.attr)
        given = {k.arg for k in node.keywords}
        if None in given:  # **scope_kw
            given |= {"scope", "multi_workspace_enabled"}
        if _required_kwonly(fn) - given:
            missing.append((node.lineno, node.func.attr, sorted(_required_kwonly(fn) - given)))
    assert not missing


@pytest.mark.asyncio
async def test_filter_current_citations_passes_scope(monkeypatch):
    from modules.chat import public as chat_public
    from modules.chat import scope as chat_scope
    from modules.knowledge.documents import public as documents_public

    monkeypatch.setattr(chat_scope, "owner_default_scope", AsyncMock(return_value=OWNER))
    monkeypatch.setattr(chat_scope, "multi_workspace_enabled", lambda: False)
    seen = {}

    async def lock(session, refs, *, require_active_source=True, require_current_version=False,
                   selection_fences=None, scope, multi_workspace_enabled):
        seen.update(scope=scope, flag=multi_workspace_enabled)
        return []

    monkeypatch.setattr(documents_public, "lock_chat_evidence_chunks", lock)
    ids = {k: str(uuid4()) for k in ("sourceId", "documentId", "documentVersionId", "chunkId")}
    assert await chat_public.filter_current_citations(MagicMock(), [ids]) == []
    assert seen == {"scope": OWNER, "flag": False}


@pytest.mark.asyncio
async def test_retrieval_does_not_swallow_type_errors(monkeypatch):
    from modules.chat import retrieval
    from modules.chat.schemas import AnswerContextRequest

    monkeypatch.setattr(
        retrieval, "owner_scope_kwargs",
        AsyncMock(return_value={"scope": OWNER, "multi_workspace_enabled": False}),
    )
    monkeypatch.setattr(retrieval.search_public, "search", AsyncMock(side_effect=TypeError("bad shape")))
    request = AnswerContextRequest(query="q", source_scope=[], entity_ids=[])
    with pytest.raises(TypeError):
        await retrieval.build_context(MagicMock(), MagicMock(), MagicMock(), MagicMock(), request)


def _config() -> AIExecutionConfig:
    return AIExecutionConfig(
        workspace_id=OWNER.workspace_id, actor_user_id=1, membership_revision=2,
        access_configuration_revision=1, configuration_revision=3, gateway_identity="a" * 64,
        endpoint_destination_id="omniroute", omniroute_base_url="http://x", omniroute_api_key="k",
        omniroute_credential_configured=True,
        aliases={"reasoning-small": ModelMapping(model="m", destination="remote")},
        privacy=PrivacySettings(allow_remote_reasoning=True, reasoning_destinations=["omniroute"]),
        chat_alias="reasoning-large", brief_alias="reasoning-small", request_timeout_seconds=5,
        web_search_provider="none", web_search_endpoint=None, web_search_api_key="",
    )


def test_worker_policies_carry_identity():
    from modules.knowledge.entities import worker as entity_worker
    from modules.timeline import worker as timeline_worker

    cfg = _config()
    policy = timeline_worker._policy(cfg, cfg.aliases["reasoning-small"], "omniroute", False)
    assert (policy.workspace_id, policy.actor_user_id, policy.membership_revision, policy.gateway_identity) == (
        OWNER.workspace_id, 1, 2, "a" * 64)
    mapping = cfg.aliases["reasoning-small"]
    assert isinstance(entity_worker._policy_allows_extraction(cfg, mapping, "omniroute", False), bool)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["modules.timeline.worker", "modules.knowledge.entities.worker"])
async def test_dependency_snapshot_capability_key_has_actor(module):
    worker = importlib.import_module(module)
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    scope = InternalJobScope(workspace_id=OWNER.workspace_id, actor_user_id=1, membership_revision=2)
    fingerprint, supported = await worker._dependency_snapshot(_config(), redis, scope=scope)
    assert fingerprint
    assert supported is False


@pytest.mark.parametrize(
    "path", ["modules/timeline/worker.py", "modules/knowledge/entities/worker.py", "modules/dashboard/briefs.py"],
)
def test_gateway_and_policy_constructions_are_complete(path):
    """Every RequestPolicy/ModelGateway construction names the required identity/fence arguments."""
    required = {
        "RequestPolicy": {n for n, f in RequestPolicy.model_fields.items() if f.is_required()},
        "ModelGateway": _required_kwonly(ModelGateway.__init__),
    }
    bad = []
    for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in required:
            given = {k.arg for k in node.keywords}
            if None not in given and required[node.func.id] - given:
                bad.append((node.lineno, node.func.id, sorted(required[node.func.id] - given)))
    assert not bad


@pytest.mark.asyncio
async def test_entity_export_validation_builds_workspace_bound_source_fence(monkeypatch):
    from modules.knowledge.entities import public as entities_public
    from modules.knowledge.entities.schemas import EntityExportFence, EntitySourceExportFence

    now = datetime.now(UTC)
    fence = EntityExportFence(
        id=uuid4(), created_at=now, updated_at=now, revision=1, alias_ids=[], evidence_ids=[],
        source_fences=[EntitySourceExportFence(source_id=uuid4(), generation=1)],
        alias_digest="0" * 64, evidence_digest="0" * 64,
    )
    monkeypatch.setattr(entities_public, "_admit", AsyncMock())
    monkeypatch.setattr(entities_public, "_actor", lambda scope: 1)
    monkeypatch.setattr(entities_public, "_entity_export_count", AsyncMock(return_value=1))
    captured = {}

    async def eligible(session, fences, *, scope, multi_workspace_enabled):
        captured["fences"] = list(fences)
        return ()

    monkeypatch.setattr(entities_public.sources, "filter_export_eligible_sources", eligible)
    session = MagicMock()
    session.scalar = AsyncMock(return_value=SimpleNamespace(created_at=now, updated_at=now, revision=1))
    out = await entities_public.validate_export_fences(
        session, owner_id=1, record_kind="entities", snapshot_at=now, expected_snapshot_count=1,
        fences=[fence], scope=OWNER, multi_workspace_enabled=False,
    )
    assert out.valid is False
    assert captured["fences"][0].workspace_id == OWNER.workspace_id


# ---- rereview: before_send configuration fences -------------------------------------------------

def _bumped(cfg: AIExecutionConfig, **changes) -> AIExecutionConfig:
    return cfg.model_copy(update=changes)


def _chat_cfg() -> AIExecutionConfig:
    cfg = _config()
    return cfg.model_copy(update={
        "aliases": {**cfg.aliases, "reranker": ModelMapping(model="rr", destination="remote")},
    })


def _factory():
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"configuration_revision": 4},
    {"gateway_identity": "b" * 64},
    {"endpoint_destination_id": "other"},
    {"aliases": {"reasoning-small": ModelMapping(model="changed", destination="remote")}},
])
async def test_chat_config_fence_denies_drift(monkeypatch, change):
    from core.model_gateway.client import PrivacyPolicyDenied
    from modules.chat import scope as chat_scope
    from modules.settings import public as settings_public

    snap = _config()
    alias, mapping = "reasoning-small", snap.aliases["reasoning-small"]
    monkeypatch.setattr(settings_public, "get_ai_execution_config", AsyncMock(return_value=_bumped(snap, **change)))
    with pytest.raises(PrivacyPolicyDenied):
        await chat_scope.ensure_ai_config_unchanged(_factory(), MagicMock(), MagicMock(), OWNER, snap, alias, mapping)
    monkeypatch.setattr(settings_public, "get_ai_execution_config", AsyncMock(return_value=snap))
    await chat_scope.ensure_ai_config_unchanged(_factory(), MagicMock(), MagicMock(), OWNER, snap, alias, mapping)


@pytest.mark.asyncio
async def test_reranker_does_not_send_after_config_revision_bump(monkeypatch):
    from modules.chat import retrieval
    from modules.chat.schemas import EvidenceItem

    snap = _chat_cfg()
    configs = iter([snap, _bumped(snap, configuration_revision=snap.configuration_revision + 1)])
    monkeypatch.setattr(retrieval, "owner_scope_kwargs", AsyncMock(return_value={"scope": OWNER, "multi_workspace_enabled": False}))
    monkeypatch.setattr(retrieval.settings_public, "get_ai_execution_config", AsyncMock(side_effect=lambda *a, **k: next(configs)))
    monkeypatch.setattr(retrieval, "may_send", lambda *a, **k: True)
    sent = []

    class FakeGateway:
        def __init__(self, **kw):
            pass

        async def rerank(self, *, before_send, **kw):
            await before_send()
            sent.append(1)
            return {}

    monkeypatch.setattr(retrieval, "ModelGateway", FakeGateway)
    item = MagicMock(spec=EvidenceItem, local_only=False, content="c", document_version_id="v", chunk_id="c1", source_generation=1)
    monkeypatch.setattr(retrieval.documents_public, "lock_chat_evidence_chunks", AsyncMock(return_value=[item]))
    items, status, _ = await retrieval._apply_configured_reranking(MagicMock(), _factory(), MagicMock(), MagicMock(), "q", [item])
    assert sent == [] and status == "unavailable" and items == [item]


@pytest.mark.asyncio
async def test_reranker_reraises_type_error(monkeypatch):
    from modules.chat import retrieval

    monkeypatch.setattr(retrieval, "owner_scope_kwargs", AsyncMock(side_effect=TypeError("shape")))
    item = MagicMock(local_only=False, content="c")
    with pytest.raises(TypeError):
        await retrieval._apply_configured_reranking(MagicMock(), _factory(), MagicMock(), MagicMock(), "q", [item])


def _brief_env(monkeypatch, *, current_cfg, admit_results):
    from modules.dashboard import briefs, context

    snap = _config()
    configs = iter([snap, current_cfg(snap)])
    monkeypatch.setattr(briefs, "_admit", AsyncMock(side_effect=admit_results))
    monkeypatch.setattr(briefs.settings_public, "get_ai_execution_config", AsyncMock(side_effect=lambda *a, **k: next(configs)))
    monkeypatch.setattr(context, "relation_to_today", lambda *a, **k: None)
    monkeypatch.setattr(context, "build_daily_widgets", AsyncMock(return_value=[]))
    monkeypatch.setattr(briefs, "_facts", AsyncMock(return_value=[{"k": 1}]))
    monkeypatch.setattr(briefs, "_lock_fact_dependencies", AsyncMock())
    monkeypatch.setattr(briefs, "_messages", lambda *a, **k: [])
    sent = []

    class FakeGateway:
        def __init__(self, *, before_send, **kw):
            self.before_send = before_send

        async def chat(self, *a, **k):
            await self.before_send()
            sent.append(1)
            raise AssertionError("must not send")

    monkeypatch.setattr(briefs, "ModelGateway", FakeGateway)
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.rollback = AsyncMock()
    return briefs, session, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["revision", "fence"])
async def test_brief_before_send_drift_is_unavailable(monkeypatch, case):
    fence_a, fence_b = object(), object()
    briefs, session, sent = _brief_env(
        monkeypatch,
        current_cfg=(lambda s: _bumped(s, configuration_revision=s.configuration_revision + 1)) if case == "revision" else (lambda s: s),
        admit_results=[fence_a, fence_a] if case == "revision" else [fence_a, fence_b],
    )
    with pytest.raises(briefs.BriefUnavailable):
        await briefs.generate_brief(
            session, datetime.now(UTC).date(), "UTC", scope=OWNER, multi_workspace_enabled=False,
            settings=MagicMock(), redis=MagicMock(), force=True,
        )
    assert sent == []
