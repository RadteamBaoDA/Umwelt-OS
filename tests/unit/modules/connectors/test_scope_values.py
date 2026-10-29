from modules.connectors.public import _scope_values

FIELDS = ("mastodon_instance_url", "mastodon_account", "mastodon_hashtag")


def test_alternative_scope_accepts_one_of_account_or_hashtag() -> None:
    """A Mastodon source with only an account (or only a hashtag) still has a verifiable scope."""
    account = _scope_values("mastodon", FIELDS, {"mastodon_instance_url": "https://m.example", "mastodon_account": "a"})
    assert account == {"mastodon_instance_url": "https://m.example", "mastodon_account": "a"}
    assert _scope_values("mastodon", FIELDS, {"mastodon_instance_url": "https://m.example", "mastodon_hashtag": "t"})


def test_scope_is_rejected_when_required_or_all_alternatives_are_missing() -> None:
    """No instance, or neither account nor hashtag, fails closed; plain providers still need every field."""
    assert _scope_values("mastodon", FIELDS, {"mastodon_account": "a"}) is None
    assert _scope_values("mastodon", FIELDS, {"mastodon_instance_url": "https://m.example"}) is None
    assert _scope_values("bluesky", ("bluesky_actor",), {}) is None
    assert _scope_values("bluesky", ("bluesky_actor",), {"bluesky_actor": "bsky.app"}) == {"bluesky_actor": "bsky.app"}
