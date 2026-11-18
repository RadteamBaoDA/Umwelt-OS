from modules.connectors.backends import PODCAST_INDEX_SCOPE_FIELDS
from modules.connectors.public import _scope_values

FIELDS = ("mastodon_instance_url", "mastodon_account", "mastodon_hashtag")


def test_alternative_scope_accepts_one_of_account_or_hashtag() -> None:
    """A Mastodon source with only an account (or only a hashtag) still has a verifiable scope."""
    account = _scope_values(
        "mastodon", FIELDS, {"mastodon_instance_url": "https://m.example", "mastodon_account": "a"}
    )
    assert account == {"mastodon_instance_url": "https://m.example", "mastodon_account": "a"}
    assert _scope_values(
        "mastodon", FIELDS, {"mastodon_instance_url": "https://m.example", "mastodon_hashtag": "t"}
    )


def test_scope_is_rejected_when_required_or_all_alternatives_are_missing() -> None:
    """No instance, or neither account nor hashtag, fails closed; plain providers still need every field."""
    assert _scope_values("mastodon", FIELDS, {"mastodon_account": "a"}) is None
    assert _scope_values("mastodon", FIELDS, {"mastodon_instance_url": "https://m.example"}) is None
    assert _scope_values("bluesky", ("bluesky_actor",), {}) is None
    assert _scope_values("bluesky", ("bluesky_actor",), {"bluesky_actor": "bsky.app"}) == {
        "bluesky_actor": "bsky.app"
    }


def test_coingecko_scope_is_verifiable_without_assets() -> None:
    """Legacy CoinGecko sources lack coingecko_assets; scope must still resolve (shared optional map)."""
    assert _scope_values("coingecko", ("coingecko_assets",), {}) == {}
    assert _scope_values("coingecko", ("coingecko_assets",), {"coingecko_assets": ["bitcoin"]}) == {
        "coingecko_assets": ["bitcoin"]
    }


def test_github_scope_follows_shared_optional_map() -> None:
    """github_history_days is optional per OPTIONAL_SCOPE_FIELDS; a missing required field still fails closed."""
    assert _scope_values("github", ("github_history_days",), {}) == {}
    assert (
        _scope_values("github", ("github_repo", "github_history_days"), {"github_history_days": 7})
        is None
    )


def test_omdb_selected_lookup_fields_are_optional_to_scope_projection() -> None:
    """OMDb mode and target fields may be absent individually; validate_options requires the active target."""
    fields = ("omdb_mode", "omdb_imdb_id", "omdb_title", "omdb_search")
    assert _scope_values("omdb_lookup", fields, {}) == {}
    title_scope = _scope_values("omdb_lookup", fields, {"omdb_mode": "title", "omdb_title": "Dune"})
    search_scope = _scope_values(
        "omdb_lookup", fields, {"omdb_mode": "search", "omdb_search": "Dune"}
    )
    assert title_scope == {"omdb_mode": "title", "omdb_title": "Dune"}
    assert search_scope == {"omdb_mode": "search", "omdb_search": "Dune"}
    assert title_scope != search_scope


def test_podcast_index_source_identity_is_only_its_search_query() -> None:
    """Podcast query defines source identity; fetch and display filters remain mutable options."""
    fields = tuple(sorted(PODCAST_INDEX_SCOPE_FIELDS))
    first = _scope_values(
        "podcast_index",
        fields,
        {"podcast_index_query": "history", "podcast_index_languages": ["en"]},
    )
    changed_filters = _scope_values(
        "podcast_index",
        fields,
        {"podcast_index_query": "history", "podcast_index_languages": ["vi"]},
    )
    assert first == changed_filters == {"podcast_index_query": "history"}
    assert _scope_values("podcast_index", fields, {}) == {}
