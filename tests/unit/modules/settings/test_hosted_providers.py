import pytest

from core.errors import DomainError
from modules.settings import public
from tests.unit.modules.settings.test_providers import (
    GW,
    _config,
    _enc,
    _provider,
    _row,
    _save,
    _settings,
    _update,
)


async def test_hosted_save_ignores_client_url_and_stores_fixed_endpoint():
    row = await _save(_row(), _update(providers=[_provider("default", kind="groq", url="https://evil.example/v1",
                      credential_action="replaced", api_key="k")]), _settings(public_ok=True))
    assert row.providers["default"]["base_url"] == "https://api.groq.com/openai/v1"
    assert row.providers["default"]["kind"] == "groq"


async def test_equivalent_hosted_url_is_kept_verbatim_on_save():
    stored = {"default": {"kind": "openai", "base_url": "https://api.openai.com", "key_ciphertext": _enc("sk"), "models": []}}
    row = await _save(_row(providers=stored), _update(providers=[_provider("default", kind="openai", url=None)]),
                      _settings(public_ok=True))
    assert row.providers["default"]["base_url"] == "https://api.openai.com"
    assert row.providers["default"]["key_ciphertext"] == stored["default"]["key_ciphertext"]


async def test_legacy_hosted_row_with_custom_url_reads_as_custom_and_keeps_key():
    stored = {"default": {"kind": "openai", "base_url": GW, "key_ciphertext": _enc("sk"),
                          "models": [{"id": "m", "type": "llm"}]}}
    config = await _config(_row(providers=stored))
    p = config.providers["default"]
    assert (p.kind, p.base_url, p.api_key, [m.id for m in p.models]) == ("custom", GW, "sk", ["m"])
    assert p.destination_id == public._identity(GW, "")[1]   # destination unchanged


async def test_legacy_row_resaved_with_hosted_kind_and_new_key_uses_fixed_endpoint():
    # The UI shows legacy rows as `custom`, so a client sending the hosted kind is a deliberate switch.
    stored = {"default": {"kind": "openai", "base_url": GW, "key_ciphertext": _enc("sk"), "models": []}}
    row = await _save(_row(providers=stored), _update(providers=[_provider(
        "default", kind="openai", url=None, credential_action="replaced", api_key="new")]), _settings(public_ok=True))
    assert row.providers["default"]["kind"] == "openai"
    assert row.providers["default"]["base_url"] == "https://api.openai.com/v1"


async def test_legacy_row_resaved_as_custom_keeps_proxy_url_and_key():
    stored = {"default": {"kind": "openai", "base_url": GW, "key_ciphertext": _enc("sk"), "models": []}}
    row = await _save(_row(providers=stored), _update(providers=[_provider("default", kind="custom", url=GW)]))
    assert row.providers["default"]["kind"] == "custom" and row.providers["default"]["base_url"] == GW
    assert row.providers["default"]["key_ciphertext"] == stored["default"]["key_ciphertext"]


async def test_switching_hosted_kind_moves_to_new_fixed_endpoint_and_drops_key():
    stored = {"default": {"kind": "openai", "base_url": "https://api.openai.com/v1", "key_ciphertext": _enc("sk"), "models": []}}
    row = await _save(_row(providers=stored), _update(providers=[_provider("default", kind="anthropic", url=None)]),
                      _settings(public_ok=True))
    assert row.providers["default"]["base_url"] == "https://api.anthropic.com/v1"
    assert row.providers["default"]["key_ciphertext"] is None


async def test_hosted_fixed_endpoint_still_obeys_deployment_policy():
    with pytest.raises(DomainError) as exc:
        await _save(_row(), _update(providers=[_provider("default", kind="openai", url=None)]), _settings(public_ok=False))
    assert exc.value.status_code == 422
