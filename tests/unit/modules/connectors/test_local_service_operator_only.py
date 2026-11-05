"""Local-service connectors (FHIR, Home Assistant) are restricted to the instance operator."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import collection, provisioning_routes
from modules.connectors.providers.rest import UnsafeDestination


def _call(actor_user_id: int) -> SimpleNamespace:
    source = SimpleNamespace(provider="health", configuration={})
    attempt = SimpleNamespace(source=source, scope=SimpleNamespace(actor_user_id=actor_user_id))
    return SimpleNamespace(run=SimpleNamespace(attempt=attempt, gate=None))


async def test_collection_refuses_non_operator(monkeypatch):
    """A workspace owner other than the operator never reaches the local fetch."""
    with pytest.raises(UnsafeDestination, match="destination_not_allowed"):
        await collection._native_local_service(_call(2))


async def test_collection_allows_operator_past_guard(monkeypatch):
    """The operator proceeds to the unchanged credential check."""
    async def no_secret(_run):
        return None, None

    monkeypatch.setattr(collection, "_rest_secret", no_secret)
    with pytest.raises(collection.CredentialMissing):
        await collection._native_local_service(_call(1))


async def test_put_configuration_403_for_non_operator(monkeypatch):
    """Saving a local-service connector fails clearly for a non-operator."""
    async def fence(*_a, **_k):
        return None

    async def source(*_a, **_k):
        return SimpleNamespace(status="active", type="api", provider="home_assistant")

    monkeypatch.setattr(provisioning_routes, "_owner_access", fence)
    monkeypatch.setattr(provisioning_routes, "_source", source)
    monkeypatch.setattr(provisioning_routes.registry, "SUPPORTED_TYPES", {"api"})
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=True))),
        state=SimpleNamespace(auth_session=SimpleNamespace(owner_id=2)),
    )
    with pytest.raises(HTTPException) as exc:
        await provisioning_routes.put_configuration(uuid4(), None, None, request, SimpleNamespace())  # type: ignore[arg-type]
    assert exc.value.status_code == 403


@pytest.mark.parametrize("provider", ["health", "home_assistant"])
async def test_create_source_403_for_non_operator(provider):
    """Creating a local-service source fails clearly for a non-operator, before any database access."""
    from modules.sources import routes
    from modules.sources.schemas import SourceCreate

    payload = SourceCreate(type="api", name="lan", provider=provider)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=True))))
    scope = SimpleNamespace(role="owner", user_id=2)
    with pytest.raises(HTTPException) as exc:
        await routes.create_source(payload, None, request, scope)  # type: ignore[arg-type]
    assert exc.value.status_code == 403
