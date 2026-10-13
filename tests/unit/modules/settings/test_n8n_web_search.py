"""n8n resolver (database over environment) and the web-search / n8n save paths."""

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
from modules.settings.schemas import N8nUpdate, WebSearchUpdate

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
KEY = Fernet.generate_key().decode()


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.ai_credential_encryption_key = SecretStr(KEY)
    settings.ai_credential_encryption_key_secondary = SecretStr("")
    settings.n8n_service_url = "http://n8n:5678/"
    settings.n8n_api_key = SecretStr("env-key")
    settings.n8n_webhook_token = SecretStr("")
    settings.multi_workspace_enabled = False
    settings.ai_allowed_endpoint_hosts = {"searx:8080"}
    settings.ai_allow_public_providers = False
    return settings


def _enc(value: str) -> str:
    return Fernet(KEY.encode()).encrypt(value.encode()).decode()


def _session(stored: dict | None) -> MagicMock:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=stored)
    return session


@pytest.mark.asyncio
async def test_resolver_falls_back_to_environment_per_field() -> None:
    resolved = await public.resolve_n8n(_session(None), _settings(), workspace_id=WS)
    assert (resolved.service_url, resolved.api_key, resolved.webhook_token) == ("http://n8n:5678/", "env-key", "")
    assert (resolved.service_url_source, resolved.api_key_source, resolved.webhook_token_source) == (
        "environment", "environment", "none")


@pytest.mark.asyncio
async def test_resolver_prefers_database_values_and_keeps_secrets_out_of_repr() -> None:
    stored = {"service_url": "http://db-n8n:5678", "api_key_ciphertext": _enc("db-key"),
              "webhook_token_ciphertext": _enc("db-token")}
    resolved = await public.resolve_n8n(_session(stored), _settings(), workspace_id=None)
    assert (resolved.service_url, resolved.api_key, resolved.webhook_token) == (
        "http://db-n8n:5678", "db-key", "db-token")
    assert resolved.api_key_source == "database" and "db-key" not in repr(resolved)
    partial = await public.resolve_n8n(_session({"webhook_token_ciphertext": _enc("db-token")}), _settings(), workspace_id=WS)
    assert (partial.api_key, partial.webhook_token) == ("env-key", "db-token")


@pytest.mark.asyncio
async def test_undecryptable_ciphertext_counts_as_unset() -> None:
    resolved = await public.resolve_n8n(_session({"api_key_ciphertext": "garbage"}), _settings(), workspace_id=WS)
    assert resolved.api_key == "env-key" and resolved.api_key_source == "environment"


async def _save(fn, row: SimpleNamespace, update, settings: MagicMock) -> None:
    fence = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
    with patch.object(public, "require_settings_scope", AsyncMock(return_value=fence)), \
         patch.object(public, "_row", AsyncMock(return_value=row)), \
         patch.object(public, "read_n8n", AsyncMock()), patch.object(public, "read_web_search", AsyncMock()):
        await fn(AsyncMock(), update, settings, scope=SCOPE)


@pytest.mark.asyncio
async def test_save_n8n_encrypts_secrets_and_applies_actions() -> None:
    row = SimpleNamespace(configuration_revision=7, n8n={"api_key_ciphertext": _enc("old"), "service_url": "http://x"})
    await _save(public.save_n8n, row, N8nUpdate(
        expected_revision=1, service_url="http://n8n:5678", webhook_token_action="replaced", webhook_token="s3cret-tok",
        api_key_action="removed"), _settings())
    # n8n keeps its own revision; the AI configuration revision is untouched.
    assert row.n8n["revision"] == 2 and row.configuration_revision == 7 and row.n8n["service_url"] == "http://n8n:5678"
    assert "api_key_ciphertext" not in row.n8n and "s3cret-tok" not in str(row.n8n)
    assert Fernet(KEY.encode()).decrypt(row.n8n["webhook_token_ciphertext"].encode()).decode() == "s3cret-tok"
    with pytest.raises(HTTPException) as exc:  # stale revision
        await _save(public.save_n8n, row, N8nUpdate(expected_revision=1), _settings())
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:  # replace without a value
        await _save(public.save_n8n, row, N8nUpdate(expected_revision=2, api_key_action="replaced"), _settings())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_save_n8n_rejects_disallowed_host() -> None:
    row = SimpleNamespace(configuration_revision=1, n8n={})
    with pytest.raises(HTTPException) as exc:
        await _save(public.save_n8n, row, N8nUpdate(expected_revision=1, service_url="http://169.254.169.254"), _settings())
    assert exc.value.status_code == 422 and row.n8n == {}
    await _save(public.save_n8n, row, N8nUpdate(expected_revision=1, service_url="http://searx:8080"), _settings())
    assert row.n8n["service_url"] == "http://searx:8080"


@pytest.mark.asyncio
async def test_env_secrets_not_sent_to_a_different_stored_url() -> None:
    settings = _settings()
    settings.n8n_webhook_token = SecretStr("env-token")
    resolved = await public.resolve_n8n(_session({"service_url": "http://elsewhere:5678"}), settings, workspace_id=WS)
    assert (resolved.api_key, resolved.webhook_token) == ("", "")
    assert (resolved.api_key_source, resolved.webhook_token_source) == ("none", "none")


def _web_row(**over: object) -> SimpleNamespace:
    base = {"configuration_revision": 2, "privacy": {}, "web_search_provider": "none", "web_search_endpoint": None,
            "web_search_api_key_ciphertext": None}
    return SimpleNamespace(**{**base, **over})


@pytest.mark.asyncio
async def test_save_web_search_searxng_is_keyless_and_binds_consent_to_the_endpoint() -> None:
    row = _web_row()
    await _save(public.save_web_search, row, WebSearchUpdate(
        expected_revision=2, provider="searxng", endpoint="http://searx:8080", allow_remote_web_search=True), _settings())
    destination = public._web_destination("http://searx:8080")
    assert row.web_search_provider == "searxng" and row.web_search_api_key_ciphertext is None
    assert row.privacy == {"allow_remote_web_search": True, "web_search_destinations": [destination]}
    assert row.configuration_revision == 3


@pytest.mark.asyncio
async def test_save_web_search_validation_and_key_handling() -> None:
    settings = _settings()
    settings.ai_allowed_endpoint_hosts = {"searx:8080", "api.tavily.com"}
    with pytest.raises(HTTPException) as exc:  # keyed provider without a key
        await _save(public.save_web_search, _web_row(), WebSearchUpdate(
            expected_revision=2, provider="tavily", endpoint="http://api.tavily.com"), settings)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):  # provider without endpoint
        await _save(public.save_web_search, _web_row(), WebSearchUpdate(expected_revision=2, provider="searxng"), settings)
    row = _web_row(web_search_provider="tavily", web_search_endpoint="http://api.tavily.com",
                   web_search_api_key_ciphertext=_enc("k"))
    await _save(public.save_web_search, row, WebSearchUpdate(
        expected_revision=2, provider="searxng", endpoint="http://searx:8080"), settings)
    assert row.web_search_api_key_ciphertext is None  # endpoint changed -> key dropped, consent off
    assert row.privacy["allow_remote_web_search"] is False
