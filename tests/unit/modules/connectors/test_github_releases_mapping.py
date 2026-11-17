"""GitHub Releases provider: pagination pinning, canonical-link policy and release record mapping."""

from datetime import UTC, datetime
from typing import Any

import pytest

from modules.connectors.providers import social as gh

NOW = datetime(2026, 6, 1, tzinfo=UTC)
BASE = "https://api.github.com/repos/Octo/Repo/releases"


def test_api_url_quotes_owner_and_repository() -> None:
    assert (
        gh._api_url("a b", "c/d", 2)
        == "https://api.github.com/repos/a%20b/c%2Fd/releases?per_page=100&page=2"
    )


@pytest.mark.parametrize(
    "link,page",
    [
        (f"{BASE}?per_page=100&page=2", 2),
        (f"{BASE.lower()}?page=3&per_page=100", 3),
        ("https://api.github.com/repositories/42/releases?per_page=100&page=4", 4),
        ("/repos/Octo/Repo/releases?per_page=100&page=5", 5),
    ],
)
def test_validated_next_rebuilds_url_from_configured_repository(link: str, page: int) -> None:
    url, parsed_page = gh._validated_next(link, "Octo", "Repo")
    assert parsed_page == page
    assert url == gh._api_url("Octo", "Repo", page)


@pytest.mark.parametrize(
    "link",
    [
        "http://api.github.com/repos/Octo/Repo/releases?per_page=100&page=2",
        "https://evil.example/repos/Octo/Repo/releases?per_page=100&page=2",
        "https://user@api.github.com/repos/Octo/Repo/releases?per_page=100&page=2",
        "https://api.github.com/repos/Other/Repo/releases?per_page=100&page=2",
        f"{BASE}?per_page=100&page=2#frag",
        f"{BASE}?per_page=50&page=2",
        f"{BASE}?per_page=100&page=2&x=1",
        f"{BASE}?per_page=100&page=2&page=3",
        f"{BASE}?per_page=100&page=x",
        f"{BASE}?per_page=100&page=0",
        f"{BASE}?per_page=100&page={gh._MAX_PAGES + 1}",
        f"{BASE}?per_page=100",
    ],
)
def test_validated_next_rejects_off_scope_or_unbounded_links(link: str) -> None:
    with pytest.raises(ValueError, match="github_pagination_invalid"):
        gh._validated_next(link, "Octo", "Repo")


def test_next_release_page_decisions() -> None:
    link = f"{BASE}?per_page=100&page=2"
    assert gh._next_release_page(None, 1, 10, "Octo", "Repo", {1}) == (None, False)
    assert gh._next_release_page(link, gh._MAX_PAGES, 10, "Octo", "Repo", {1}) == (None, True)
    assert gh._next_release_page(link, 1, 500, "Octo", "Repo", {1}) == (None, True)
    assert gh._next_release_page(link, 1, 10, "Octo", "Repo", {1}) == (
        gh._api_url("Octo", "Repo", 2),
        False,
    )
    with pytest.raises(ValueError, match="github_pagination_invalid"):
        gh._next_release_page(link, 1, 10, "Octo", "Repo", {1, 2})


@pytest.mark.parametrize(
    "url,tag,kept",
    [
        ("https://github.com/Octo/Repo/releases/tag/v1.0", "v1.0", True),
        ("https://github.com/octo/repo/releases/tag/v1.0", "v1.0", True),
        ("https://github.com/Octo/Repo/releases/tag/v1%2E0", "v1.0", True),
        ("https://github.com/Octo/Repo/releases/tag/v2", "v1.0", False),
        ("http://github.com/Octo/Repo/releases/tag/v1.0", "v1.0", False),
        ("https://github.com/Octo/Repo/releases/tag/v1.0?x=1", "v1.0", False),
        ("https://github.com/Octo/Repo/releases/latest", "v1.0", False),
        ("https://github.com/Octo/Repo/releases/tag/v1.0", None, False),
        ("https://u@github.com/Octo/Repo/releases/tag/v1.0", "v1.0", False),
        ("", "v1.0", False),
    ],
)
def test_release_canonical_requires_matching_repository_and_tag(
    url: str, tag: str | None, kept: bool
) -> None:
    assert gh._release_canonical(url, "Octo", "Repo", tag) == (url if kept else None)


def test_release_flags_default_false_and_require_booleans() -> None:
    assert gh._release_flags({}) == (False, False)
    assert gh._release_flags({"draft": True, "prerelease": True}) == (True, True)
    with pytest.raises(ValueError, match="github_response_invalid"):
        gh._release_flags({"draft": "yes"})


def test_str_field_returns_only_strings() -> None:
    assert gh._str_field({"a": "x"}, "a") == "x"
    assert gh._str_field({"a": 1}, "a") is None
    assert gh._str_field({}, "a") is None


def _release(**over: Any) -> dict[str, Any]:
    release: dict[str, Any] = {
        "id": 77,
        "node_id": "RE_1",
        "name": "First",
        "body": "<p>Notes <b>here</b></p>",
        "tag_name": "v1.0",
        "html_url": "https://github.com/Octo/Repo/releases/tag/v1.0",
        "created_at": "2026-05-01T00:00:00Z",
        "published_at": "2026-05-02T00:00:00Z",
        "author": {"login": "octocat"},
    }
    release.update(over)
    return release


def test_release_record_maps_identity_content_and_version() -> None:
    record = gh._release_record(_release(), "Octo", "Repo", NOW)
    assert record is not None
    assert record.provider_id == "github_releases:octo/repo:77"
    assert record.content == "First\nTag: v1.0\nNotes here"
    assert record.observed_at == NOW and record.collected_at == NOW
    assert record.metadata["provider_record"]["provider"] == "github_releases"
    changed = gh._release_record(_release(body="changed"), "Octo", "Repo", NOW)
    assert changed is not None and changed.version != record.version


def test_release_record_title_falls_back_to_tag_then_id() -> None:
    by_tag = gh._release_record(_release(name=None), "Octo", "Repo", NOW)
    assert by_tag is not None and by_tag.metadata["title"] == "v1.0"
    by_id = gh._release_record(
        _release(name=None, tag_name=None, html_url=None), "Octo", "Repo", NOW
    )
    assert by_id is not None and by_id.metadata["title"] == "Release 77"


def test_draft_releases_are_never_ingested() -> None:
    assert gh._release_record(_release(draft=True), "Octo", "Repo", NOW) is None


@pytest.mark.parametrize(
    "release",
    [
        "x",
        _release(id=True),
        _release(id=0),
        _release(id="7"),
        _release(created_at="x" * 65),
        _release(html_url="https://evil.example/Octo/Repo/releases/tag/v1.0"),
        _release(prerelease="no"),
    ],
)
def test_release_record_rejects_malformed_releases(release: object) -> None:
    with pytest.raises(ValueError, match="github_response_invalid"):
        gh._release_record(release, "Octo", "Repo", NOW)


def test_long_release_content_is_truncated_and_flagged() -> None:
    record = gh._release_record(_release(body="x" * 5000), "Octo", "Repo", NOW)
    assert record is not None
    assert len(record.content) == 4000
    assert record.metadata["provider_record"]["content_truncated"] is True


def test_mark_records_truncated_rewrites_coverage_in_place() -> None:
    record = gh._release_record(_release(), "Octo", "Repo", NOW)
    assert record is not None
    records = [record]
    gh._mark_records_truncated(records)
    assert records[0].metadata["provider_record"]["coverage"] == "truncated"
    assert record.metadata["provider_record"]["coverage"] == "returned_snapshot"
