"""Characterization tests for the conn-collect refactor: native registry, webhook table, helper contracts."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from modules.connectors import collection
from modules.connectors.github import webhooks
from modules.connectors.providers import feed_catalog, local_services, telegram
from modules.connectors.providers.rest import UnsafeDestination


def _run(provider: str, rollback: AsyncMock) -> SimpleNamespace:
    """Build a minimal fake ``Run`` whose session records rollbacks."""

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(rollback=rollback)

    source = SimpleNamespace(id=uuid4(), provider=provider, configuration={})
    return SimpleNamespace(
        attempt=SimpleNamespace(source=source, connector_revision=1),
        gate=SimpleNamespace(captured_operation=None),
        extras={},
        factory=factory,
        next_eligible_at=None,
    )


async def test_new_native_provider_plugs_in_without_editing_the_dispatcher(monkeypatch):
    """A handler returning a page goes through rollback then the generic accept path."""
    page = SimpleNamespace(records=("r",), coverage="returned_snapshot", next_eligible_at=None)
    seen = []

    async def handler(call):
        seen.append(call.session)
        return page

    rollback = AsyncMock()
    accept = AsyncMock()
    monkeypatch.setattr(
        collection, "_lease", AsyncMock(return_value=SimpleNamespace(cursor_before=None))
    )
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(collection, "_NATIVE_HANDLERS", dict(collection._NATIVE_HANDLERS))
    collection.register_native_provider(("brand_new",), handler)

    await collection._run_native(_run("brand_new", rollback))

    assert len(seen) == 1
    rollback.assert_awaited_once()
    assert accept.await_args.args[2:4] == (("r",), "returned_snapshot")


async def test_handler_returning_none_skips_generic_accept_and_rollback(monkeypatch):
    """Handlers that accept their own receipt return ``None``: the dispatcher stops immediately."""
    rollback = AsyncMock()
    accept = AsyncMock()
    monkeypatch.setattr(
        collection, "_lease", AsyncMock(return_value=SimpleNamespace(cursor_before=None))
    )
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(collection, "_NATIVE_HANDLERS", dict(collection._NATIVE_HANDLERS))
    collection.register_native_provider(("self_accepting",), AsyncMock(return_value=None))

    await collection._run_native(_run("self_accepting", rollback))

    rollback.assert_not_awaited()
    accept.assert_not_awaited()


async def test_provider_backoff_page_sets_next_eligible_and_rate_limits(monkeypatch):
    """A page carrying ``next_eligible_at`` raises ``ProviderRateLimited`` before any accept."""
    when = datetime.now(UTC) + timedelta(minutes=5)
    page = SimpleNamespace(records=(), coverage="returned_snapshot", next_eligible_at=when)
    accept = AsyncMock()
    monkeypatch.setattr(
        collection, "_lease", AsyncMock(return_value=SimpleNamespace(cursor_before=None))
    )
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(collection, "_NATIVE_HANDLERS", dict(collection._NATIVE_HANDLERS))
    collection.register_native_provider(("slow",), AsyncMock(return_value=page))
    run = _run("slow", AsyncMock())

    with pytest.raises(collection.connectors.ProviderRateLimited):
        await collection._run_native(run)

    assert run.next_eligible_at == when
    accept.assert_not_awaited()


def test_builtin_native_providers_are_registered():
    """Every historical dispatcher branch has a registry entry."""
    expected = {
        "youtube",
        "arxiv",
        "huggingface",
        "bluesky",
        "reddit",
        "mastodon",
        "x",
        "google_mail",
        "google_calendar",
        "google_drive",
        "notion",
        "slack",
        "health",
        "home_assistant",
        "github_releases",
    }
    assert expected <= set(collection._NATIVE_HANDLERS)


@pytest.mark.parametrize(
    ("reason", "expected_type"),
    [
        ("authorization_rejected", "ProviderHttpError"),
        ("rate_limited", "ProviderRateLimited"),
        ("request_failed", TimeoutError),
        ("destination_not_local", UnsafeDestination),
        ("http_503", "ProviderHttpError"),
    ],
)
def test_local_service_failure_mapping(reason, expected_type):
    """LocalServiceError reasons keep their historical collection error mapping."""
    mapped = collection._local_service_failure(reason)
    assert mapped is not None
    if isinstance(expected_type, str):
        assert type(mapped).__name__ == expected_type
    else:
        assert isinstance(mapped, expected_type)


def test_local_service_failure_mapping_passes_unknown_reasons_through():
    """Unrecognised reasons (including malformed http_) are re-raised unchanged by the caller."""
    assert collection._local_service_failure("something_else") is None
    assert collection._local_service_failure("http_abc") is None


def _delivery(event: str, body: dict) -> webhooks.VerifiedGitHubDelivery:
    return webhooks.parse_github_delivery(
        raw_body=json.dumps(body).encode(),
        receiver_revision="r1",
        delivery_id="d1",
        event=event,
        app_id="7",
    )


_ENVELOPE = {"installation": {"id": 11, "app_id": 7}, "repository": {"id": 22}}


@pytest.mark.parametrize(
    ("event", "extra", "resource", "kind", "locator", "intent"),
    [
        ("issues", {"action": "opened", "issue": {"number": 5}}, "issue", "number", "5", "refresh"),
        (
            "pull_request",
            {"action": "closed", "pull_request": {"number": 6}},
            "pull",
            "number",
            "6",
            "refresh",
        ),
        (
            "release",
            {"action": "published", "release": {"id": 9}},
            "release",
            "release_id",
            "9",
            "refresh",
        ),
        (
            "release",
            {"action": "deleted", "release": {"id": 9}},
            "release",
            "release_id",
            "9",
            "delete_candidate",
        ),
        ("push", {"ref": "refs/heads/main"}, "commit", "ref", "refs/heads/main", "reconcile"),
        ("repository", {"action": "privatized"}, "issue", "repository", "22", "visibility_check"),
        ("public", {}, "issue", "repository", "22", "visibility_check"),
        ("installation", {"action": "suspend"}, "issue", "installation", "11", "visibility_lost"),
        (
            "installation",
            {"action": "unsuspend"},
            "issue",
            "installation",
            "11",
            "visibility_check",
        ),
        (
            "installation_repositories",
            {"action": "added"},
            "issue",
            "installation",
            "11",
            "visibility_check",
        ),
    ],
)
def test_webhook_event_table_maps_hints(event, extra, resource, kind, locator, intent):
    """Each supported event keeps its exact target hint."""
    delivery = _delivery(event, {**_ENVELOPE, **extra})
    assert delivery.disposition == "received"
    hint = delivery.targets[0]
    assert (hint.resource, hint.locator_kind, hint.locator, hint.intent) == (
        resource,
        kind,
        locator,
        intent,
    )


@pytest.mark.parametrize(
    ("event", "extra"),
    [
        ("issues", {"action": "nonsense", "issue": {"number": 5}}),
        ("release", {"action": "weird", "release": {"id": 9}}),
        ("push", {"ref": "refs/pull/1/head"}),
        ("repository", {"action": "created"}),
        ("installation", {"action": "created"}),
        ("installation_repositories", {"action": "other"}),
        ("star", {"action": "created"}),
    ],
)
def test_webhook_unsupported_shapes_are_ignored(event, extra):
    """Unsupported actions, refs and events are ignored with no targets."""
    delivery = _delivery(event, {**_ENVELOPE, **extra})
    assert delivery.disposition == "ignored" and delivery.targets == ()


def test_webhook_removed_repositories_become_visibility_lost_hints_and_validate_identity():
    """Removed repositories map one hint each; a bad identity fails the whole delivery."""
    ok = _delivery(
        "installation_repositories",
        {
            **_ENVELOPE,
            "action": "removed",
            "repositories_removed": [{"id": 31}, {"id": 32}],
        },
    )
    assert [(h.locator, h.intent) for h in ok.targets] == [
        ("31", "visibility_lost"),
        ("32", "visibility_lost"),
    ]
    with pytest.raises(ValueError, match="Removed repository identity is invalid"):
        _delivery("installation_repositories", {**_ENVELOPE, "repositories_removed": [{"id": "x"}]})


def test_webhook_ping_and_missing_installation():
    """Ping is accepted; targets are dropped when no installation id is present."""
    assert _delivery("ping", _ENVELOPE).disposition == "ping"
    delivery = _delivery("push", {"repository": {"id": 22}, "ref": "refs/heads/main"})
    assert delivery.disposition == "ignored" and delivery.targets == ()


def test_retry_deadline_takes_the_latest_header_and_rejects_bad_evidence():
    """The retry deadline helpers keep max-of-headers and fail-closed behaviour."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    headers = httpx.Headers({"retry-after": "30", "ratelimit-reset": "90"})
    assert feed_catalog._retry_deadline(headers, now) == now + timedelta(seconds=90)
    assert feed_catalog._retry_deadline(httpx.Headers({}), now) == now + timedelta(minutes=1)
    for bad in (
        {"retry-after": "0"},
        {"x-ratelimit-reset": "1"},
        {"ratelimit-reset": "-1"},
        {"ratelimit": "garbage"},
    ):
        with pytest.raises(ValueError, match="provider_rate_deadline_invalid"):
            feed_catalog._retry_deadline(httpx.Headers(bad), now)


def test_telegram_rate_limit_deadline_defaults_and_rejects():
    """429 body parsing: missing parameters default to 60 s, invalid values fail closed."""
    before = datetime.now(UTC)
    assert telegram._rate_limit_deadline(
        b'{"parameters": {"retry_after": 5}}'
    ) >= before + timedelta(seconds=5)
    assert telegram._rate_limit_deadline(b"{}") >= before + timedelta(seconds=59)
    for body in (
        b'{"parameters": {"retry_after": 0}}',
        b'{"parameters": {"retry_after": true}}',
        b"not json",
    ):
        with pytest.raises(ValueError, match="telegram_rate_deadline_invalid"):
            telegram._rate_limit_deadline(body)


def test_home_assistant_resume_index_only_for_matching_scope():
    """Saved indexes are honoured only for the exact provider/version/scope."""
    good = json.dumps({"v": 1, "provider": "home_assistant", "scope_hash": "h", "index": 2})
    assert local_services._ha_resume_index(good, "h", 5) == 2
    assert local_services._ha_resume_index(good, "other", 5) == 0
    assert local_services._ha_resume_index(good, "h", 2) == 0
    assert local_services._ha_resume_index("not json", "h", 5) == 0
    assert local_services._ha_resume_index(None, "h", 5) == 0
