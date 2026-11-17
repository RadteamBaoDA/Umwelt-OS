"""CoinGecko catalog search never merges distinct coin IDs that share one ticker."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import coin_catalog
from modules.connectors.providers import rest
from modules.connectors.provisioning import routes as provisioning_routes
from tests.unit.modules.connectors._routes_patch import patch_routes_mp


def test_catalog_keeps_duplicate_symbols_and_searches_by_name_id_or_symbol():
    coins = coin_catalog.parse_catalog(
        json.dumps(
            [
                {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
                {"id": "bitcoin-cash", "symbol": "bch", "name": "Bitcoin Cash"},
                {"id": "other-bitcoin", "symbol": "btc", "name": "Other Bitcoin"},
            ]
        ).encode()
    )
    assert coins[0]["symbol"] == "BTC"
    assert [item["id"] for item in coin_catalog.search_catalog(coins, "btc")] == [
        "bitcoin",
        "other-bitcoin",
    ]
    assert [item["id"] for item in coin_catalog.search_catalog(coins, "cash")] == ["bitcoin-cash"]


def test_catalog_preserves_printable_provider_symbols_and_bounds_names():
    coins = coin_catalog.parse_catalog(
        json.dumps(
            [
                {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
                {"id": "bad-symbol", "symbol": "bad.coin", "name": "Bad symbol"},
                {"id": "bad-name", "symbol": "bad", "name": "x" * 129},
            ]
        ).encode()
    )
    assert [coin["id"] for coin in coins] == ["bitcoin", "bad-symbol"]
    assert coins[1]["symbol"] == "BAD.COIN"


@pytest.mark.parametrize(
    "body", [b"{}", b"not-json", b'[{"id":"BTC","symbol":"btc","name":"Bitcoin"}]']
)
def test_catalog_rejects_bad_payloads(body):
    with pytest.raises(ValueError):
        coin_catalog.parse_catalog(body)


@pytest.mark.asyncio
async def test_catalog_cache_avoids_repeated_provider_send(monkeypatch):
    sends = []

    async def before_send():
        sends.append("debit")

    async def fetch(url, *, headers, before_send, **kwargs):
        assert url == coin_catalog.COINS_URL
        assert headers == {"x-cg-demo-api-key": "demo-key"}
        await before_send()
        return rest.Fetched(b'[{"id":"bitcoin","symbol":"btc","name":"Bitcoin"}]')

    monkeypatch.setattr(coin_catalog, "_catalog", ())
    monkeypatch.setattr(coin_catalog, "_expires_at", 0.0)
    monkeypatch.setattr(coin_catalog.rest, "fetch_bounded", fetch)
    assert len(await coin_catalog.load_catalog("demo-key", before_send)) == 1
    assert len(await coin_catalog.load_catalog("demo-key", before_send)) == 1
    assert sends == ["debit"]


@pytest.mark.asyncio
async def test_search_requires_saved_current_key_before_catalog_fetch(monkeypatch):
    source_id = uuid4()
    session = SimpleNamespace(get=AsyncMock(return_value=None))
    scope = SimpleNamespace(role="owner", workspace_id=uuid4())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))
        )
    )
    patch_routes_mp(monkeypatch, "_owner_access", AsyncMock(return_value=object()))
    patch_routes_mp(
        monkeypatch,
        "_source",
        AsyncMock(
            return_value=SimpleNamespace(provider="coingecko", status="active", generation=1)
        ),
    )
    monkeypatch.setattr(
        provisioning_routes.provisioning,
        "activation_status",
        AsyncMock(return_value=SimpleNamespace(desired_revision=2)),
    )
    fetch = AsyncMock()
    monkeypatch.setattr(coin_catalog, "load_catalog", fetch)
    with pytest.raises(HTTPException) as exc:
        await provisioning_routes.search_coingecko_coins(source_id, session, request, scope, q="bt")
    assert exc.value.status_code == 409
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_source_with_saved_key_can_search_catalog(monkeypatch):
    source_id = uuid4()
    credential = SimpleNamespace(
        state="ready",
        encrypted_secret="ciphertext",
        header_name="x-cg-demo-api-key",
        source_generation=1,
        configuration_revision=2,
        operation_id=uuid4(),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=credential), rollback=AsyncMock())
    scope = SimpleNamespace(role="owner", workspace_id=uuid4())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    multi_workspace_enabled=False,
                    connector_credential_encryption_key=SimpleNamespace(
                        get_secret_value=lambda: "deployment-key"
                    ),
                )
            )
        )
    )
    patch_routes_mp(monkeypatch, "_owner_access", AsyncMock(return_value=object()))
    patch_routes_mp(
        monkeypatch,
        "_source",
        AsyncMock(
            return_value=SimpleNamespace(provider="coingecko", status="paused", generation=1)
        ),
    )
    monkeypatch.setattr(
        provisioning_routes.provisioning,
        "activation_status",
        AsyncMock(return_value=SimpleNamespace(desired_revision=2, credential_revision=3)),
    )
    monkeypatch.setattr(
        provisioning_routes.provider_terms, "require_terms_eligible", AsyncMock(return_value=1)
    )
    patch_routes_mp(monkeypatch, "decrypt_rest_secret", lambda *args, **kwargs: "demo-key")
    fetch = AsyncMock(return_value=({"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin"},))
    monkeypatch.setattr(coin_catalog, "load_catalog", fetch)
    result = await provisioning_routes.search_coingecko_coins(
        source_id, session, request, scope, q="bitcoin"
    )
    assert [item["id"] for item in result.items] == ["bitcoin"]
    fetch.assert_awaited_once()
