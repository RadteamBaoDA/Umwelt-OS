"""P15-T4 acceptance: language/since params on dashboard projections (needs the Compose harness)."""

import os
from uuid import uuid4

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

PROJECTIONS = "/api/v1/documents/dashboard-projections"


@pytest.mark.asyncio
async def test_invalid_language_and_since_rejected(owner_client: AsyncClient) -> None:
    source = uuid4()
    for extra in ("language=xx", "language=EN", "since=2001-01-01T00:00:00Z", "since=2026-01-01T00:00:00"):
        response = await owner_client.get(f"{PROJECTIONS}?source_ids={source}&{extra}")
        assert response.status_code == 422, extra


@pytest.mark.asyncio
async def test_valid_filters_return_empty_page_for_unknown_source(owner_client: AsyncClient) -> None:
    response = await owner_client.get(f"{PROJECTIONS}?source_ids={uuid4()}&language=vi")
    assert response.status_code == 200
    assert response.json()["items"] == []
