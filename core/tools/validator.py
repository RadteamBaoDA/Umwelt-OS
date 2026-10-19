"""Bounded JSON Schema validation for registered tool contracts and payloads."""

import json
from typing import Any

from jsonschema import (  # type: ignore[import-untyped]  # no stubs
    Draft202012Validator,
    FormatChecker,
)
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]  # no stubs

MAX_SCHEMA_BYTES = 64_000
MAX_INSTANCE_BYTES = 256_000
MAX_ERRORS = 20
_MAX_VALIDATOR_CACHE_ENTRIES = 512
_VALIDATOR_CACHE: dict[str, Draft202012Validator] = {}


def _reject_remote_references(value: Any, depth: int = 0) -> None:
    """Reject remote or deeply nested references before validator construction."""
    if depth > 32:
        raise ValueError("Schema nesting exceeds the supported bound")
    if isinstance(value, dict):
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            ref = value.get(keyword)
            if isinstance(ref, str) and not ref.startswith("#"):
                raise ValueError("Remote schema references are not permitted")
        for child in value.values():
            _reject_remote_references(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _reject_remote_references(child, depth + 1)


def _get_or_compile_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Return a cached or freshly compiled Draft 2020-12 validator for a verified bounded schema.

    Serializes ``schema`` with sorted keys so repeated invocations with equivalent contracts reuse
    the compiled validator and skip redundant meta-schema traversal while capping memory at 512 entries.
    """
    if not isinstance(schema, dict):
        raise TypeError("Tool schema must be a JSON object")
    serialized = json.dumps(schema, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError("Schema exceeds the configured size limit")
    cached = _VALIDATOR_CACHE.pop(serialized, None)
    if cached is not None:
        # Re-insert on hit to maintain LRU eviction order using standard dict insertion ordering.
        _VALIDATOR_CACHE[serialized] = cached
        return cached
    _reject_remote_references(schema)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    if len(_VALIDATOR_CACHE) >= _MAX_VALIDATOR_CACHE_ENTRIES:
        _VALIDATOR_CACHE.pop(next(iter(_VALIDATOR_CACHE)))
    _VALIDATOR_CACHE[serialized] = validator
    return validator


def validate_json_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate bounded JSON data against Draft 2020-12 without resolving remote references.

    Invalid schemas and non-JSON or oversized instances return generic errors; payload values
    are never copied into diagnostics, logs, or caller-visible error text.
    """
    try:
        instance_bytes = json.dumps(instance, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(instance_bytes) > MAX_INSTANCE_BYTES:
            return ["Schema or data exceeds the configured size limit"]
        validator = _get_or_compile_validator(schema)
        errors = []
        for error in validator.iter_errors(instance):
            location = ".".join(str(part) for part in error.absolute_path)
            errors.append(f"Invalid value at {path}{'.' + location if location else ''}")
            if len(errors) >= MAX_ERRORS:
                break
        return errors
    except ValueError as exc:
        if str(exc) == "Schema exceeds the configured size limit":
            return ["Schema or data exceeds the configured size limit"]
        return ["Invalid JSON value or tool schema"]
    except (TypeError, SchemaError, RecursionError):
        return ["Invalid JSON value or tool schema"]


def check_json_schema(schema: dict[str, Any]) -> None:
    """Check a bounded Draft 2020-12 schema, reject remotely resolved references, and warm the validator cache."""
    _get_or_compile_validator(schema)
