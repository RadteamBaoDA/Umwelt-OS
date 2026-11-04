"""S6 hardening: pinned n8n egress, XML DTD guard, MultiFernet rotation, webhook HMAC signatures."""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr

from core.model_gateway.transport import EndpointNetworkPolicyError
from modules.automations.routes import _verify_webhook_signature
from modules.connectors.credentials import decrypt_credential_input, encrypt_credential_input
from modules.connectors.providers.feed_catalog import _records_from_feed
from modules.connectors.providers.news import safe_xml_root
from modules.settings import public

BOMB = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><feed><entry>&x;</entry></feed>'


def _settings(**kw: object) -> SimpleNamespace:
    base = {
        "n8n_service_url": "http://n8n:5678/", "ai_allowed_endpoint_cidrs": [], "ai_allow_public_providers": False,
        "ai_credential_encryption_key": SecretStr(""), "ai_credential_encryption_key_secondary": SecretStr(""),
    }
    return SimpleNamespace(**(base | kw))


def test_n8n_owner_host_without_address_policy_fails_closed() -> None:
    with pytest.raises(EndpointNetworkPolicyError):
        public.n8n_http_client("http://rebind.example:5678", _settings(), timeout=5)


def test_n8n_env_host_stays_plain_and_cidr_host_is_pinned() -> None:
    assert public.n8n_http_client("http://n8n:5678", _settings(), timeout=5)._transport.__class__.__name__ != "ApprovedEndpointTransport"
    client = public.n8n_http_client("http://other:5678", _settings(ai_allowed_endpoint_cidrs=["10.0.0.0/8"]), timeout=5)
    assert client._transport.__class__.__name__ == "ApprovedEndpointTransport"


def test_xml_guard_rejects_doctype_in_feed_helpers() -> None:
    with pytest.raises(ValueError):
        safe_xml_root(BOMB)
    with pytest.raises(ValueError):
        _records_from_feed(SimpleNamespace(configuration={}), BOMB, None)  # type: ignore[arg-type]


def test_connector_key_ring_decrypts_with_secondary_and_encrypts_with_primary() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    ids = {"source_id": uuid4(), "slot": "s", "operation_id": uuid4()}
    blob = encrypt_credential_input(old, request={"a": 1}, binding={}, **ids)
    assert decrypt_credential_input(f"{new},{old}", blob, **ids) == ({"a": 1}, {})
    fresh = encrypt_credential_input(f"{new},{old}", request={"a": 2}, binding={}, **ids)
    assert decrypt_credential_input(new, fresh, **ids) == ({"a": 2}, {})


def test_ai_cipher_rotation() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    legacy = public._cipher(_settings(ai_credential_encryption_key=SecretStr(old))).encrypt(b"k")
    rotated = _settings(ai_credential_encryption_key=SecretStr(new), ai_credential_encryption_key_secondary=SecretStr(old))
    assert public._cipher(rotated).decrypt(legacy) == b"k"
    assert Fernet(new.encode()).decrypt(public._cipher(rotated).encrypt(b"k")) == b"k"


def _sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def test_webhook_signature() -> None:
    secrets = {"hook": SecretStr("s3")}
    body, ts = b'{"event":"x"}', str(int(time.time()))
    _verify_webhook_signature(secrets, "hook", body, ts, _sign("s3", ts, body))
    _verify_webhook_signature(secrets, "other", body, None, None)  # unconfigured alias: bearer only
    old = str(int(time.time()) - 3600)
    for args in ((ts, "sha256=bad"), (None, None), (old, _sign("s3", old, body)), (ts, _sign("zz", ts, body))):
        with pytest.raises(HTTPException) as exc:
            _verify_webhook_signature(secrets, "hook", body, *args)
        assert exc.value.status_code == 401
