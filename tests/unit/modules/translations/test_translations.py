"""Mock-level T1 contracts: DTO exactness, deny-by-default admission, scope predicates, route deps."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from core.auth.dependencies import require_account_write
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.settings.schemas import TranslationSettingsRead, TranslationSettingsUpdate
from modules.translations import public
from modules.translations.models import ContentTranslation
from modules.translations.routes import router
from modules.translations.schemas import TranslationBatchRequest, TranslationItemRequest

WS = uuid4()


def _scope(role: str = "member", user_id: int = 7) -> WorkspaceContext:
    return WorkspaceContext(user_id=user_id, workspace_id=WS, role=role, membership_revision=1)  # type: ignore[arg-type]


def _item(**kw):
    return {"resource_type": "news_story", "resource_id": str(uuid4()), "resource_revision": "r1", **kw}


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.fixture(autouse=True)
def fences(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(public, "lock_access_fence", AsyncMock(side_effect=lambda *a, **k: calls.append("lock")))
    monkeypatch.setattr(public, "read_access_fence", AsyncMock(side_effect=lambda *a, **k: calls.append("read")))
    monkeypatch.setattr(public, "_AUTHORIZERS", {})
    return calls


def _session(*, scalar=None):
    session = MagicMock()
    session.executed = []

    async def scalar_(stmt, *a, **k):
        session.executed.append(stmt)
        return scalar

    session.scalar = scalar_
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    return session


def test_translation_is_disabled_and_vietnamese_by_default():
    value = TranslationSettingsRead()
    assert (value.enabled, value.target_language, value.configuration_revision) == (False, "vi", 1)


def test_update_dto_is_exact():
    with pytest.raises(ValidationError):
        TranslationSettingsUpdate(enabled=True, target_language="vi", expected_revision=1, configuration_revision=5)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        TranslationSettingsUpdate(enabled=True, target_language="fr", expected_revision=1)  # type: ignore[arg-type]


def test_batch_dto_rejects_unknown_duplicate_empty_and_oversize():
    one = _item()
    with pytest.raises(ValidationError):
        TranslationBatchRequest.model_validate({"items": [one, dict(one)]})
    with pytest.raises(ValidationError):
        TranslationBatchRequest.model_validate({"items": []})
    with pytest.raises(ValidationError):
        TranslationBatchRequest.model_validate({"items": [_item() for _ in range(26)]})
    with pytest.raises(ValidationError):
        TranslationItemRequest.model_validate(_item(text="client supplied"))
    assert len(TranslationBatchRequest.model_validate({"items": [_item() for _ in range(25)]}).items) == 25


async def test_member_cannot_save_settings(fences):
    with pytest.raises(HTTPException) as exc:
        await public.save_translation_settings(
            _session(), TranslationSettingsUpdate(enabled=True, target_language="vi", expected_revision=1),
            scope=_scope("member"), multi_workspace_enabled=True)
    assert exc.value.status_code == 403 and fences == []


async def test_stale_revision_is_409():
    row = SimpleNamespace(enabled=False, target_language="vi", configuration_revision=3)
    with pytest.raises(HTTPException) as exc:
        await public.save_translation_settings(
            _session(scalar=row), TranslationSettingsUpdate(enabled=True, target_language="vi", expected_revision=1),
            scope=_scope("owner"), multi_workspace_enabled=True)
    assert exc.value.status_code == 409


async def test_disable_bumps_revision_and_blocks_pending():
    row = SimpleNamespace(enabled=True, target_language="vi", configuration_revision=2, updated_by_user_id=None)
    session = _session(scalar=row)
    out = await public.save_translation_settings(
        session, TranslationSettingsUpdate(enabled=False, target_language="vi", expected_revision=2),
        scope=_scope("owner"), multi_workspace_enabled=True)
    assert (out.enabled, out.configuration_revision) == (False, 3)
    sql = _sql(session.execute.await_args_list[-1].args[0])
    assert "UPDATE content_translations" in sql and "'blocked'" in sql and str(WS) in sql and "'pending'" in sql


async def test_unchanged_save_keeps_revision():
    row = SimpleNamespace(enabled=True, target_language="vi", configuration_revision=2)
    out = await public.save_translation_settings(
        _session(scalar=row), TranslationSettingsUpdate(enabled=True, target_language="vi", expected_revision=2),
        scope=_scope("owner"), multi_workspace_enabled=True)
    assert out.configuration_revision == 2


async def test_disabled_returns_blocked_without_enqueue(fences):
    session = _session(scalar=None)
    request = TranslationBatchRequest.model_validate({"items": [_item(), _item()]})
    out = await public.submit_batch(session, request, scope=_scope(), multi_workspace_enabled=True)
    assert out.batch_id is None and {i.status for i in out.items} == {"blocked"}
    assert out.settings == TranslationSettingsRead()
    session.add.assert_not_called()
    assert fences == ["lock"]


async def test_no_registered_authorizer_is_uniform_404():
    row = SimpleNamespace(enabled=True, target_language="vi", configuration_revision=1)
    request = TranslationBatchRequest.model_validate({"items": [_item()]})
    with pytest.raises(HTTPException) as exc:
        await public.submit_batch(_session(scalar=row), request, scope=_scope(), multi_workspace_enabled=True)
    assert exc.value.status_code == 404


async def test_stale_visible_revision_is_409_after_all_authorized():
    row = SimpleNamespace(enabled=True, target_language="vi", configuration_revision=1)

    async def ok(session, *, scope, resource_id, multi_workspace_enabled):
        return public.ResourceAuthorization("server-rev", "c" * 64, "v" * 64)

    public.register_resource_authorizer("news_story", ok)
    request = TranslationBatchRequest.model_validate({"items": [_item()]})
    with pytest.raises(HTTPException) as exc:
        await public.submit_batch(_session(scalar=row), request, scope=_scope(), multi_workspace_enabled=True)
    assert exc.value.status_code == 409


async def test_batch_read_is_actor_and_workspace_bound(fences):
    session = _session(scalar=None)
    with pytest.raises(HTTPException) as exc:
        await public.read_batch(session, uuid4(), scope=_scope(user_id=7), multi_workspace_enabled=True)
    assert exc.value.status_code == 404 and fences == ["read"]
    sql = _sql(session.executed[0])
    assert f"workspace_id = '{WS}'" in sql and "actor_user_id = 7" in sql


def test_post_requires_csrf_admission_without_owner_role():
    post = next(r for r in router.routes if r.path.endswith("/batches") and "POST" in r.methods)
    deps = {d.call for d in post.dependant.dependencies}
    assert require_account_write in deps and require_workspace_read in deps
    assert require_workspace_write not in deps and post.status_code == 202


def test_get_is_workspace_bound_dependency():
    get = next(r for r in router.routes if "{batch_id}" in r.path)
    assert require_workspace_read in {d.call for d in get.dependant.dependencies}


def test_schema_reserves_t3_columns_and_full_fingerprint():
    cols = set(ContentTranslation.__table__.c.keys())
    assert {"lease_token", "lease_expires_at", "slot_token", "slot_expires_at", "attempt_count",
            "next_attempt_at", "expires_at", "result", "error_code"} <= cols
    unique = next(c for c in ContentTranslation.__table__.constraints if c.name == "uq_content_translations_fingerprint")
    assert {"actor_user_id", "content_hash", "visibility_hash", "config_hash", "prompt_version"} <= {c.name for c in unique.columns}
