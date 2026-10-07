"""Unit tests for automations condition language and deterministic evaluation.

Covers:
- `_is_type`: strict scalar type enforcement (bool is not number, string length bounds).
- `check_condition`: valid and invalid field/operator/value combinations across all trigger types.
- `validate_sample`: sample payload type checking against trigger specifications.
- `evaluate`: deterministic AND evaluation across eq, ne, in, gt, gte, lt, lte, missing fields, and type errors.
"""

import pytest

from modules.automations.conditions import (
    _is_type,
    check_condition,
    evaluate,
    validate_sample,
)


class TestConditionTyping:
    """Tests for _is_type scalar validation."""

    def test_boolean_check(self) -> None:
        assert _is_type(True, "boolean") is True
        assert _is_type(False, "boolean") is True
        assert _is_type(1, "boolean") is False
        assert _is_type("true", "boolean") is False

    def test_number_check(self) -> None:
        assert _is_type(42, "number") is True
        assert _is_type(3.14, "number") is True
        assert _is_type(True, "number") is False  # In Python, bool is subclass of int; must reject
        assert _is_type("42", "number") is False

    def test_string_check(self) -> None:
        assert _is_type("hello", "string") is True
        assert _is_type("", "string") is True
        assert _is_type("a" * 500, "string") is True
        assert _is_type("a" * 501, "string") is False
        assert _is_type(123, "string") is False


class TestCheckCondition:
    """Tests for condition schema and operator validation."""

    def test_valid_conditions(self) -> None:
        check_condition("schedule", "hour", "eq", 9)
        check_condition("schedule", "hour", "gte", 0)
        check_condition("schedule", "hour", "lt", 24)
        check_condition("new_event", "event_type", "eq", "meeting")
        check_condition("new_event", "event_type", "in", ["meeting", "call"])
        check_condition("task_due", "created_by_automation", "eq", True)

    def test_unknown_field_raises_error(self) -> None:
        with pytest.raises(ValueError, match="is not declared by trigger"):
            check_condition("schedule", "non_existent", "eq", 1)

    def test_ordered_operator_on_non_number_raises_error(self) -> None:
        with pytest.raises(ValueError, match="requires a numeric field"):
            check_condition("new_event", "event_type", "gt", "meeting")

    def test_invalid_in_values_raises_error(self) -> None:
        with pytest.raises(ValueError, match="'in' needs 1-50 string values"):
            check_condition("new_event", "event_type", "in", [])
        with pytest.raises(ValueError, match="'in' needs 1-50 string values"):
            check_condition("new_event", "event_type", "in", ["valid", 123])

    def test_mistyped_value_raises_error(self) -> None:
        with pytest.raises(ValueError, match="must be number"):
            check_condition("schedule", "hour", "eq", "nine")


class TestValidateSample:
    """Tests for validate_sample preview payload checking."""

    def test_valid_sample(self) -> None:
        validate_sample("schedule", {"hour": 14, "weekday": 2})

    def test_invalid_field_in_sample(self) -> None:
        with pytest.raises(ValueError, match="is not a valid schedule field"):
            validate_sample("schedule", {"hour": 14, "unknown": "value"})

    def test_invalid_type_in_sample(self) -> None:
        with pytest.raises(ValueError, match="is not a valid schedule field"):
            validate_sample("schedule", {"hour": "fourteen"})


class TestEvaluate:
    """Tests for deterministic condition evaluation."""

    def test_evaluate_all_operators(self) -> None:
        conditions = [
            {"field": "hour", "operator": "gte", "value": 9},
            {"field": "hour", "operator": "lt", "value": 18},
            {"field": "weekday", "operator": "in", "value": [1, 2, 3, 4, 5]},
            {"field": "status", "operator": "eq", "value": "active"},
            {"field": "status", "operator": "ne", "value": "paused"},
            {"field": "new_items", "operator": "lte", "value": 10},
            {"field": "new_items", "operator": "gt", "value": 0},
        ]
        sample = {
            "hour": 10,
            "weekday": 2,
            "status": "active",
            "new_items": 5,
        }
        matched, reasons = evaluate(conditions, sample)
        assert matched is True
        assert len(reasons) == len(conditions)
        assert all(r["outcome"] == "matched" for r in reasons)

    def test_evaluate_missing_field(self) -> None:
        conditions = [{"field": "missing_field", "operator": "eq", "value": 1}]
        sample = {"hour": 10}
        matched, reasons = evaluate(conditions, sample)
        assert matched is False
        assert reasons[0]["outcome"] == "missing_field"

    def test_evaluate_type_mismatch_fails_cleanly(self) -> None:
        conditions = [{"field": "hour", "operator": "gt", "value": 10}]
        sample = {"hour": "string_value"}
        matched, reasons = evaluate(conditions, sample)
        assert matched is False
        assert reasons[0]["outcome"] == "not_matched"
