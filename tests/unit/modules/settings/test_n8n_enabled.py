"""n8n kill switch persistence: absent means enabled, round trip, revision-checked, callback base."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.settings import public
from modules.settings.schemas import N8nUpdate

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.ai_credential_encryption_key = SecretStr(Fernet.generate_key().decode())
    settings.n8n_service_url = "http://n8n:5678/"
    settings.n8n_api_key = SecretStr("")
    settings.n8n_webhook_token = SecretStr("")
    settings.multi_workspace_enabled = False
    return settings


def _session(stored: dict | None) -> MagicMock:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=stored)
    return session


async def _save(row: SimpleNamespace, update: N8nUpdate) -> None:
    fence = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
    with patch.object(public, "require_settings_scope", AsyncMock(return_value=fence)), \
         patch.object(public, "_row", AsyncMock(return_value=row)), \
         patch.object(public, "read_n8n", AsyncMock()):
        await public.save_n8n(AsyncMock(), update, _settings(), scope=SCOPE)


@pytest.mark.asyncio
async def test_absent_flag_means_enabled() -> None:
    for stored in (None, {}, {"revision": 2}):
        assert (await public.resolve_n8n(_session(stored), _settings(), workspace_id=WS)).enabled is True
        assert await public.n8n_enabled(_session(stored), workspace_id=WS) is True
    assert (await public.resolve_n8n(_session({"enabled": False}), _settings(), workspace_id=WS)).enabled is False
    assert await public.n8n_enabled(_session({"enabled": False}), workspace_id=WS) is False


@pytest.mark.asyncio
async def test_enabled_round_trips_and_none_leaves_it_unchanged() -> None:
    row = SimpleNamespace(configuration_revision=1, n8n={})
    await _save(row, N8nUpdate(expected_revision=1, enabled=False))
    assert row.n8n["enabled"] is False and row.n8n["revision"] == 2
    await _save(row, N8nUpdate(expected_revision=2))
    assert row.n8n["enabled"] is False
    await _save(row, N8nUpdate(expected_revision=3, enabled=True))
    assert row.n8n["enabled"] is True


@pytest.mark.asyncio
async def test_enabled_change_is_revision_checked() -> None:
    row = SimpleNamespace(configuration_revision=1, n8n={"revision": 4})
    with pytest.raises(HTTPException) as exc:
        await _save(row, N8nUpdate(expected_revision=1, enabled=False))
    assert exc.value.status_code == 409 and "enabled" not in row.n8n


@pytest.mark.asyncio
async def test_read_exposes_enabled_and_fixed_callback_base() -> None:
    row = SimpleNamespace(n8n={"revision": 3, "enabled": False})
    with patch.object(public, "require_settings_scope", AsyncMock()), patch.object(public, "_row", AsyncMock(return_value=row)):
        read = await public.read_n8n(_session(row.n8n), _settings(), scope=SCOPE)
    assert read.enabled is False and read.callback_base_url == "http://api:8000" and read.configuration_revision == 3
