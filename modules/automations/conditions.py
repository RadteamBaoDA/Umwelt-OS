"""Deterministic whitelist condition language: field/operator/value only, no eval or expressions."""

from __future__ import annotations

from typing import Any, Literal

Operator = Literal["eq", "ne", "in", "gt", "gte", "lt", "lte"]
FieldType = Literal["string", "number", "boolean"]

# Declared fields per trigger type. Conditions may only reference these.
TRIGGER_FIELDS: dict[str, dict[str, FieldType]] = {
    "schedule": {"weekday": "number", "hour": "number"},
    "new_event": {"event_type": "string", "source_id": "string", "importance": "number"},
    "new_document": {"source_id": "string", "source_type": "string", "mime_type": "string", "title": "string"},
    "entity_changed": {"entity_id": "string", "entity_type": "string", "change": "string"},
    "task_due": {
        "status": "string", "goal_id": "string", "hours_until_due": "number", "created_by_automation": "boolean",
    },
    "goal_deadline": {"goal_id": "string", "status": "string", "days_until_deadline": "number", "progress": "number"},
    "webhook": {"event": "string"},
    "connector_sync_result": {"source_id": "string", "status": "string", "new_items": "number"},
}
_ORDERED = {"gt", "gte", "lt", "lte"}
MAX_IN_VALUES = 50


def _is_type(value: Any, kind: FieldType) -> bool:
    """Check a JSON scalar against a declared type; bool is never a number."""
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str) and len(value) <= 500


def check_condition(trigger_type: str, field: str, operator: str, value: Any) -> None:
    """Validate one condition against the trigger's declared fields and operator typing.

    Raises:
        ValueError: Unknown field, ordered operator on a non-number, or mistyped value.
    """
    kind = TRIGGER_FIELDS[trigger_type].get(field)
    if kind is None:
        raise ValueError(f"field '{field}' is not declared by trigger '{trigger_type}'")
    if operator in _ORDERED and kind != "number":
        raise ValueError(f"operator '{operator}' requires a numeric field")
    if operator == "in":
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES or not all(_is_type(v, kind) for v in value):
            raise ValueError(f"'in' needs 1-{MAX_IN_VALUES} {kind} values")
    elif not _is_type(value, kind):
        raise ValueError(f"value for '{field}' must be {kind}")


def validate_sample(trigger_type: str, sample: dict[str, Any]) -> None:
    """Require preview sample keys to be declared trigger fields with matching types."""
    for key, value in sample.items():
        kind = TRIGGER_FIELDS[trigger_type].get(key)
        if kind is None or not _is_type(value, kind):
            raise ValueError(f"sample field '{key}' is not a valid {trigger_type} field")


def evaluate(conditions: list[dict[str, Any]], sample: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate all conditions (AND) against a flat payload.

    Returns ``(matched, reasons)``; reasons carry index/field/operator/outcome codes only,
    never payload values, so previews cannot echo content.
    """
    reasons: list[dict[str, Any]] = []
    for index, cond in enumerate(conditions):
        field, op, expected = cond["field"], cond["operator"], cond["value"]
        if field not in sample:
            outcome = "missing_field"
        else:
            actual = sample[field]
            try:
                if op == "eq": ok = actual == expected
                elif op == "ne": ok = actual != expected
                elif op == "in": ok = actual in expected
                elif op == "gt": ok = actual > expected
                elif op == "gte": ok = actual >= expected
                elif op == "lt": ok = actual < expected
                else: ok = actual <= expected
            except TypeError:
                ok = False  # mistyped runtime payload never matches an ordered comparison
            outcome = "matched" if ok else "not_matched"
        reasons.append({"index": index, "field": field, "operator": op, "outcome": outcome})
    return all(r["outcome"] == "matched" for r in reasons), reasons
