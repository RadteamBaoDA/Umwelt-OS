"""Mocked Hardcover GraphQL request, mapping, and receipt contracts."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.connectors import collection, provider_options
from modules.connectors.catalog import get_catalog_entry
from modules.connectors.provider_specs import get_quota_policies
from modules.connectors.providers import rest
from modules.connectors.providers.keyed import KEYED
from modules.connectors.service.scope import _scope_values
from modules.ingestion._native.verifiers import native_verifier_for
from tests.unit.modules.connectors._collection_patch import patch_collection

PROVIDER = "hardcover_books"
TODAY = datetime(2026, 11, 18, tzinfo=UTC)
TOKEN = "example-private-hardcover-token"
BOOK = {
    "id": 8,
    "title": "A book",
    "slug": "a-book",
    "release_date": "2026-12-01",
    "release_year": 2026,
    "rating": 4.1,
    "pages": 320,
    "audio_seconds": 11200,
    "cached_tags": {"Genre": [{"tag": "Science Fiction"}, {"tag": "Fantasy"}]},
    "cached_image": {"url": "https://assets.hardcover.app/cover.jpg"},
    "editions": [{"isbn_13": "978-0-441-17271-9", "isbn_10": "0441172717"}],
}


def fetched(rows, **extra):
    return SimpleNamespace(body=json.dumps({"data": {"books": rows}, **extra}).encode())


def run_with_pages(monkeypatch, pages, config=None, *, cursor=None, matching=True):
    config = config or {"hardcover_mode": "catalog"}
    lease = AsyncMock(
        return_value=SimpleNamespace(cursor_before=cursor),
    )
    accept = AsyncMock()
    gate = SimpleNamespace(fetch_bytes=AsyncMock(side_effect=pages), bind_credential=Mock())
    source = SimpleNamespace(provider=PROVIDER, type="api", configuration=config)
    run = SimpleNamespace(
        attempt=SimpleNamespace(source=source, credential_revision=4),
        extras={},
        gate=gate,
        settings=SimpleNamespace(
            connector_credential_encryption_key=SimpleNamespace(
                get_secret_value=lambda: "local-key"
            ),
        ),
    )
    patch_collection(monkeypatch, "_lease", lease)
    patch_collection(monkeypatch, "_rest_secret", AsyncMock(return_value=("Authorization", TOKEN)))
    patch_collection(monkeypatch, "_accept_native", accept)
    patch_collection(
        monkeypatch,
        "_state",
        AsyncMock(
            return_value=SimpleNamespace(
                continuation_state=provider_options.native_fingerprint(PROVIDER, config)
                if matching
                else "old",
            )
        ),
    )
    adapter = collection._adapter_for(source)
    assert adapter is not None
    return run, adapter, accept


def test_source_scope_and_proof_policy():
    row = get_catalog_entry(PROVIDER)
    assert row is not None and row.code_available and not row.runtime_verified
    assert row.scope_fields == ("hardcover_mode",)
    assert set(row.data_shapes) == {"media"}
    assert row.min_interval_minutes == 360
    assert {policy.window: policy.limit_units for policy in get_quota_policies(PROVIDER)} == {
        "second": 10,
        "minute": 60,
        "day": 5000,
    }
    assert PROVIDER not in KEYED
    assert native_verifier_for(PROVIDER).revision_policy == "required"
    first = {"hardcover_mode": "upcoming", "min_rating": 70}
    second = {**first, "min_rating": 80}
    assert _scope_values(PROVIDER, row.scope_fields, first) == {"hardcover_mode": "upcoming"}
    assert _scope_values(PROVIDER, row.scope_fields, first) == _scope_values(
        PROVIDER, row.scope_fields, second
    )
    assert provider_options.native_fingerprint(
        PROVIDER, first
    ) != provider_options.native_fingerprint(PROVIDER, second)


async def test_post_and_normalize_with_canonical_filters(monkeypatch):
    config = {
        "hardcover_mode": "upcoming",
        "release_year": 2026,
        "min_rating": 80,
        "media_genres": ["sci_fi"],
    }
    mismatch = {**BOOK, "id": 9, "cached_tags": {"Genre": [{"tag": "Romance"}]}}
    run, adapter, accept = run_with_pages(
        monkeypatch, [fetched([BOOK, mismatch])], config, cursor="7"
    )
    await adapter(run)
    call = run.gate.fetch_bytes.await_args
    assert call.args == ("https://api.hardcover.app/v1/graphql",)
    assert call.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(call.kwargs["json_body"])
    assert "books_bool_exp!" in body["query"]
    assert "order_by: {id: asc}" in body["query"]
    where = body["variables"]["where"]
    assert where["id"] == {"_gt": 7}
    assert where["release_year"] == {"_eq": 2026}
    assert where["rating"] == {"_gte": 4.0}
    assert where["release_date"]["_gte"]
    assert TOKEN not in str(body)
    run.gate.bind_credential.assert_called_once()
    assert TOKEN not in repr(run.gate.bind_credential.call_args)
    receipt = accept.await_args
    assert receipt.kwargs == {"cursor_after": "0", "provider_credential_revision": 4}
    assert run.extras["native_fp"] == provider_options.native_fingerprint(PROVIDER, config)
    [record] = receipt.args[2]
    fields = record.metadata["provider_record"]["source_fields"]
    assert record.provider_id == "hardcover:8"
    assert fields["release_date"] == "2026-12-01"
    assert fields["release_year"] == 2026
    assert fields["rating"] == pytest.approx(82)
    assert fields["genres"] == ["sci_fi", "fantasy"]
    assert fields["image_url"] == "https://assets.hardcover.app/cover.jpg"
    assert (fields["page_count"], fields["duration_seconds"]) == (320, 11200)
    assert fields["external_ids"] == {
        "hardcover": "8",
        "isbn13": "9780441172719",
        "isbn10": "0441172717",
    }


@pytest.mark.parametrize(
    "broken", ["http", "graphql", "truncated", "oversized", "repeated", "rotated"]
)
async def test_second_page_failure_leaves_previous_receipt_unadvanced(monkeypatch, broken):
    from modules.connectors.providers import hardcover

    full = [{**BOOK, "id": i} for i in range(1, hardcover.PAGE_SIZE + 1)]
    failure = {
        "http": rest.ProviderHttpError(503),
        "graphql": fetched([{**BOOK, "id": hardcover.PAGE_SIZE + 1}], errors=[{"message": TOKEN}]),
        "truncated": fetched([], extensions={"truncated": True}),
        "oversized": fetched([BOOK] * (hardcover.PAGE_SIZE + 1)),
        "repeated": fetched([{**BOOK, "id": hardcover.PAGE_SIZE}]),
        "rotated": collection.CollectionFenceLost("credential_changed", "revision_changed"),
    }[broken]
    run, adapter, accept = run_with_pages(
        monkeypatch, [fetched(full), failure], cursor="91", matching=False
    )
    with pytest.raises(
        (
            rest.ProviderHttpError,
            rest.CollectionIncomplete,
            collection.CollectionFenceLost,
            ValueError,
        )
    ) as caught:
        await adapter(run)
    assert TOKEN not in str(caught.value)
    assert run.gate.fetch_bytes.await_count == 2
    queries = [
        json.loads(call.kwargs["json_body"]) for call in run.gate.fetch_bytes.await_args_list
    ]
    assert [q["variables"]["where"]["id"]["_gt"] for q in queries] == [0, hardcover.PAGE_SIZE]
    assert not run.extras
    accept.assert_not_awaited()


async def test_full_pages_commit_bounded_continuation(monkeypatch):
    from modules.connectors.providers import hardcover

    full = [
        fetched([{**BOOK, "id": offset + i} for i in range(1, hardcover.PAGE_SIZE + 1)])
        for offset in range(0, hardcover.PAGE_SIZE * hardcover.MAX_PAGES, hardcover.PAGE_SIZE)
    ]
    run, adapter, accept = run_with_pages(monkeypatch, full)
    await adapter(run)
    assert run.gate.fetch_bytes.await_count == hardcover.MAX_PAGES
    assert accept.await_args.kwargs["cursor_after"] == str(
        hardcover.PAGE_SIZE * hardcover.MAX_PAGES
    )
    assert len(accept.await_args.args[2]) == hardcover.PAGE_SIZE * hardcover.MAX_PAGES


@pytest.mark.parametrize("credential", [rest.ProviderHttpError(401), ("X-Wrong-Header", TOKEN)])
async def test_missing_or_unbound_token_never_sends(monkeypatch, credential):
    run, adapter, accept = run_with_pages(monkeypatch, [])
    loader = (
        AsyncMock(side_effect=credential)
        if isinstance(credential, Exception)
        else AsyncMock(return_value=credential)
    )
    patch_collection(monkeypatch, "_rest_secret", loader)

    with pytest.raises(collection.CredentialMissing):
        await adapter(run)

    run.gate.fetch_bytes.assert_not_awaited()
    accept.assert_not_awaited()


def test_build_request_strips_a_pasted_bearer_prefix() -> None:
    """A token copied with its ``Bearer`` prefix must not produce ``Bearer Bearer``."""
    from datetime import UTC, datetime

    from modules.connectors.providers import hardcover

    request = hardcover.build_request(
        "Bearer abc.def", {"hardcover_mode": "catalog"}, datetime(2026, 1, 1, tzinfo=UTC), 0
    )
    assert request.headers["Authorization"] == "Bearer abc.def"
