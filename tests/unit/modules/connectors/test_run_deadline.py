"""The longer collection deadline applies to the topic_watch provider only."""

from modules.connectors import selection
from modules.connectors._collection import topic_watch


def test_override_applies_only_to_topic_watch() -> None:
    assert selection.run_deadline_seconds("topic_watch") == 240
    assert selection.run_deadline_seconds("github") == selection.RUN_DEADLINE_SECONDS == 90
    assert selection.run_deadline_seconds(None) == 90


def test_soft_deadline_leaves_time_to_accept_before_the_hard_cut() -> None:
    assert topic_watch.SOFT_DEADLINE_SECONDS == 220
    assert topic_watch.SOFT_DEADLINE_SECONDS < selection.run_deadline_seconds("topic_watch")
