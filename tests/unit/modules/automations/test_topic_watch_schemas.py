"""Topic watch rule contracts: classifier, consent hash, schedule floor and one-watch-per-rule."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from modules.automations.schemas import (
    AutomationDefinition,
    CollectSourceAction,
    scheduled_task_kind,
    topic_consent_hash,
)

TRIGGER = {"type": "schedule", "cron": "0 8 * * *", "timezone": "UTC"}
WATCH = {"topic": "ai chips", "max_items": 5}


def _definition(cron: str = "0 8 * * *", actions: list[dict[str, Any]] | None = None) -> dict:
    return {
        "trigger": {"type": "schedule", "cron": cron, "timezone": "UTC"},
        "actions": actions or [{"type": "collect_source", "watch": WATCH}],
    }


def test_kind_classifier_cases() -> None:
    watch = {"type": "collect_source", "watch": WATCH}
    brief = {"type": "generate_brief", "deliver_chat": True}
    assert scheduled_task_kind(TRIGGER, [watch]) == "topic_watch"
    assert scheduled_task_kind(TRIGGER, [brief]) == "daily_brief"
    assert scheduled_task_kind({"type": "webhook", "hook": "x"}, [watch]) is None
    assert scheduled_task_kind(TRIGGER, [watch, brief]) is None
    assert scheduled_task_kind(TRIGGER, [{"type": "generate_brief", "deliver_chat": False}]) is None


def test_consent_hash_is_stable_and_binds_every_consented_field() -> None:
    base = topic_consent_hash(WATCH, TRIGGER, "background")
    assert base == topic_consent_hash(dict(WATCH), dict(TRIGGER), "background")
    assert len(base) == 64
    assert base != topic_consent_hash({**WATCH, "topic": "other"}, TRIGGER, "background")
    assert base != topic_consent_hash({**WATCH, "max_items": 6}, TRIGGER, "background")
    assert base != topic_consent_hash(WATCH, {**TRIGGER, "cron": "0 9 * * *"}, "background")
    assert base != topic_consent_hash(
        WATCH, {**TRIGGER, "timezone": "Asia/Ho_Chi_Minh"}, "background"
    )
    assert base != topic_consent_hash(WATCH, TRIGGER, "chat")


def test_five_minute_floor_applies_to_model_spend() -> None:
    AutomationDefinition.model_validate(_definition("*/5 * * * *"))
    with pytest.raises(ValidationError):
        AutomationDefinition.model_validate(_definition("*/2 * * * *"))


def test_one_collect_source_per_rule() -> None:
    watch = {"type": "collect_source", "watch": WATCH}
    with pytest.raises(ValidationError):
        AutomationDefinition.model_validate(_definition(actions=[watch, watch]))


def test_spec_normalises_sites_and_bounds() -> None:
    action = CollectSourceAction.model_validate(
        {"type": "collect_source", "watch": {**WATCH, "include_sites": ["EXAMPLE.com"]}}
    )
    assert action.watch.include_sites == ["example.com"] and action.watch.languages == ["en"]
    assert action.model_role == "background"
    for bad in (
        {"max_items": 11},
        {"languages": ["eng"]},
        {"include_sites": ["a b"]},
        {"topic": ""},
    ):
        with pytest.raises(ValidationError):
            CollectSourceAction.model_validate(
                {"type": "collect_source", "watch": {**WATCH, **bad}}
            )
