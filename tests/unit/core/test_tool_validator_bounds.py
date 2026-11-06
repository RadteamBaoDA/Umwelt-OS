"""core.tools.validator: remote-ref rejection, size bounds, error caps, no payload leakage, LRU cache."""

from __future__ import annotations

from typing import Any

import pytest

from core.tools import validator

SCHEMA = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}


def test_valid_instance_has_no_errors_and_invalid_reports_path_without_value() -> None:
    assert validator.validate_json_schema({"n": 1}, SCHEMA) == []
    errors = validator.validate_json_schema({"n": "secret-value"}, SCHEMA)
    assert errors == ["Invalid value at $.n"]
    assert "secret-value" not in " ".join(errors)


def test_errors_are_capped() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    errors = validator.validate_json_schema(["x"] * 100, schema)
    assert len(errors) == validator.MAX_ERRORS


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_remote_references_rejected_but_local_allowed(keyword: str) -> None:
    assert validator.validate_json_schema(1, {keyword: "https://evil.example/s.json"}) == [
        "Invalid JSON value or tool schema"
    ]
    validator.check_json_schema({"$defs": {"a": {"type": "integer"}}, "$ref": "#/$defs/a"})


def test_deep_nesting_and_non_object_schema_rejected() -> None:
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(40):
        node["properties"] = {"a": {}}
        node = node["properties"]["a"]
    assert validator.validate_json_schema(1, deep) == ["Invalid JSON value or tool schema"]
    assert validator.validate_json_schema(1, ["not", "a", "dict"]) == [
        "Invalid JSON value or tool schema"
    ]  # type: ignore[arg-type]


def test_invalid_schema_and_non_json_instances_are_generic_errors() -> None:
    assert validator.validate_json_schema(1, {"type": "nope"}) == [
        "Invalid JSON value or tool schema"
    ]
    assert validator.validate_json_schema(float("nan"), SCHEMA) == [
        "Invalid JSON value or tool schema"
    ]
    assert validator.validate_json_schema(object(), SCHEMA) == ["Invalid JSON value or tool schema"]


def test_size_limits_for_instance_and_schema() -> None:
    too_big = "x" * (validator.MAX_INSTANCE_BYTES + 1)
    assert validator.validate_json_schema(too_big, {}) == [
        "Schema or data exceeds the configured size limit"
    ]
    big_schema = {"description": "y" * (validator.MAX_SCHEMA_BYTES + 1)}
    assert validator.validate_json_schema(1, big_schema) == [
        "Schema or data exceeds the configured size limit"
    ]


def test_cache_reuses_validator_and_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validator, "_VALIDATOR_CACHE", {})
    monkeypatch.setattr(validator, "_MAX_VALIDATOR_CACHE_ENTRIES", 2)
    first = validator._get_or_compile_validator({"const": 1})
    assert validator._get_or_compile_validator({"const": 1}) is first
    validator._get_or_compile_validator({"const": 2})
    validator._get_or_compile_validator({"const": 1})  # hit refreshes recency of const=1
    validator._get_or_compile_validator({"const": 3})  # evicts const=2, the least recently used
    keys = list(validator._VALIDATOR_CACHE)
    assert len(keys) == 2 and '{"const":2}' not in keys
