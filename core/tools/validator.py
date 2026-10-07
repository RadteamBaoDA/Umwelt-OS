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


def validate_json_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate bounded JSON data against Draft 2020-12 without resolving remote references.

    Invalid schemas and non-JSON or oversized instances return generic errors; payload values
    are never copied into diagnostics, logs, or caller-visible error text.
    """
    try:
        schema_bytes = json.dumps(schema, allow_nan=False, separators=(",", ":")).encode()
        instance_bytes = json.dumps(instance, allow_nan=False, separators=(",", ":")).encode()
        if len(schema_bytes) > MAX_SCHEMA_BYTES or len(instance_bytes) > MAX_INSTANCE_BYTES:
            return ["Schema or data exceeds the configured size limit"]
        _reject_remote_references(schema)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = []
        for error in validator.iter_errors(instance):
            location = ".".join(str(part) for part in error.absolute_path)
            errors.append(f"Invalid value at {path}{'.' + location if location else ''}")
            if len(errors) >= MAX_ERRORS:
                break
        return errors
    except (TypeError, ValueError, SchemaError, RecursionError):
        return ["Invalid JSON value or tool schema"]


def check_json_schema(schema: dict[str, Any]) -> None:
    """Check a bounded Draft 2020-12 schema and reject remotely resolved references."""
    encoded = json.dumps(schema, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ValueError("Schema exceeds the configured size limit")
    _reject_remote_references(schema)
    Draft202012Validator.check_schema(schema)
