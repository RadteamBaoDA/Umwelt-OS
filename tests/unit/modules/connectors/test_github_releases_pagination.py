"""GitHub Link headers name repositories by numeric id; the next page must still be accepted."""

import pytest

from modules.connectors.providers.social import _validated_next


def test_repository_id_link_yields_rebuilt_owner_repo_url() -> None:
    url, page = _validated_next("https://api.github.com/repositories/70107786/releases?per_page=100&page=2", "vercel", "next.js")
    assert page == 2
    assert url == "https://api.github.com/repos/vercel/next.js/releases?per_page=100&page=2"


@pytest.mark.parametrize("link", [
    "https://evil.example/repositories/1/releases?per_page=100&page=2",
    "https://api.github.com/repositories/abc/releases?per_page=100&page=2",
    "https://api.github.com/repositories/1/issues?per_page=100&page=2",
    "https://api.github.com/repositories/1/releases?per_page=100&page=9",
])
def test_unrelated_links_are_rejected(link: str) -> None:
    with pytest.raises(ValueError, match="github_pagination_invalid"):
        _validated_next(link, "vercel", "next.js")
