"""Canonical registration and bounded dispatch boundary for native tools."""

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import json
import logging
import time
from typing import Any
from fastapi.encoders import jsonable_encoder

from core.tools.policy import ToolPolicy
from core.tools.schemas import ToolDefinition, ToolExecutionPrincipal, ToolResult, ToolRisk
from core.tools.validator import check_json_schema, validate_json_schema

logger = logging.getLogger(__name__)
ToolHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]
PrincipalRevalidator = Callable[[ToolExecutionPrincipal], Awaitable[bool]]
MAX_CONCURRENCY = 4
ALLOWED_ERROR_CODES = frozenset({
    "tool_unavailable", "forbidden", "approval_required", "invalid_arguments",
    "invalid_result", "timeout", "execution_failed",
})
MAX_EVIDENCE_REFS = 100
MAX_EVIDENCE_BYTES = 8_192


class ToolRegistry:
    """Own immutable tool contracts, lifecycle checks and bounded async invocation."""

    def __init__(
        self,
        policy: ToolPolicy | None = None,
        module_registry: dict[str, Any] | None = None,
        *,
        max_concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        """Create an empty registry with trusted descriptors and a bounded dispatch semaphore.

        Args:
            policy: Optional risk policy; elevated effects still require the durable T3 owner.
            module_registry: Server-composed descriptor map used for enabled/dependency fences.
            max_concurrency: Simultaneous async handler limit, from 1 through 32.
        Raises:
            ValueError: If the concurrency bound is outside its supported range.
        """
        if not 1 <= max_concurrency <= 32:
            raise ValueError("Tool concurrency must be between 1 and 32")
        self.policy = policy or ToolPolicy()
        self._module_registry = module_registry if module_registry is not None else {}
        self._tools: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._fingerprints: dict[str, str] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def set_module_registry(self, module_registry: dict[str, Any]) -> None:
        """Replace trusted module lifecycle descriptors from server composition."""
        self._module_registry = module_registry

    def register_tool(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        """Register one unique immutable name/version contract and its async handler.

        Schema validation and the canonical definition fingerprint happen before publication. Handler
        execution is always async; replacement, signature guessing, and sync work are rejected.
        Raises ValueError for invalid schemas or duplicate names and TypeError for sync handlers.
        """
        if definition.name in self._tools:
            raise ValueError("Duplicate tool name")
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("Tool handlers must be async functions")
        for schema in (definition.input_schema, definition.output_schema):
            check_json_schema(schema)
        fingerprint = definition.schema_fingerprint
        definition = definition.model_copy(deep=True)
        self._tools[definition.name] = definition
        self._handlers[definition.name] = handler
        self._fingerprints[definition.name] = fingerprint

    def unregister_tool(self, name: str) -> bool:
        """Remove one definition, handler and fingerprint; return whether it was registered."""
        self._handlers.pop(name, None)
        self._fingerprints.pop(name, None)
        return self._tools.pop(name, None) is not None

    def get_tool(self, name: str, version: str | None = None) -> ToolDefinition | None:
        """Return a defensive copy of the registered contract for exact name and optional version."""
        definition = self._tools.get(name)
        return definition.model_copy(deep=True) if definition and (version is None or version == definition.version) else None

    def list_tools(self, allowed_tools: frozenset[str] | None = None) -> list[ToolDefinition]:
        """Return defensive copies of enabled contracts intersected with optional server grants."""
        return sorted(
            (item.model_copy(deep=True) for item in self._tools.values()
             if (allowed_tools is None or item.name in allowed_tools) and self._module_enabled(item.module)),
            key=lambda item: item.name,
        )

    def _module_enabled(self, module_id: str) -> bool:
        """Require a present enabled owner descriptor and all of its declared dependencies."""
        dispatcher = self._module_registry.get("tools")
        if dispatcher is None or not getattr(dispatcher, "enabled", False):
            return False
        if any(
            dependency not in self._module_registry
            or not getattr(self._module_registry[dependency], "enabled", False)
            for dependency in getattr(dispatcher, "dependencies", ())
        ):
            return False
        descriptor = self._module_registry.get(module_id)
        if descriptor is None or not getattr(descriptor, "enabled", False):
            return False
        return all(
            dependency in self._module_registry
            and getattr(self._module_registry[dependency], "enabled", False)
            for dependency in getattr(descriptor, "dependencies", ())
        )

    async def invoke_tool(
        self,
        name: str,
        args: dict[str, Any],
        principal: ToolExecutionPrincipal,
        *,
        version: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Authorize a registered call, validate both JSON boundaries, and dispatch within fixed bounds.

        The server supplies principal, a current-principal revalidator, optional session/services,
        and destination context. Caller
        arguments cannot supply identity, permissions, module state, or approval truth. The exact
        registered version controls policy, timeout and schemas; async handler concurrency is
        bounded globally and each call's queue wait plus execution shares the same deadline. A
        server revalidator and all identity/policy checks run after acquiring queue capacity.
        Errors are normalized, evidence references are bounded, and the serialized full envelope
        is size-checked before return. This is not an atomic permit for a later remote network send.
        """
        started = time.perf_counter()
        definition = self.get_tool(name, version)
        if definition is None or name not in self._handlers:
            return ToolResult(success=False, error="Unknown tool or version", error_code="tool_unavailable")
        if name not in principal.allowed_tools or not self._module_enabled(definition.module):
            return ToolResult(success=False, error="Tool is unavailable for this principal", error_code="forbidden")
        if not set(definition.permissions).issubset(principal.capabilities):
            return ToolResult(success=False, error="Required tool capability is not granted", error_code="forbidden")
        execution_context = dict(context or {})
        execution_context["principal"] = principal
        destination = execution_context.get("destination_id")
        if destination is None or destination not in principal.destinations:
            return ToolResult(success=False, error="Tool destination is not granted", error_code="forbidden")
        if (
            not principal.actor_id
            or (not principal.is_owner and not principal.source_ids)
            or (principal.owner_all_sources and not principal.is_owner)
        ):
            return ToolResult(success=False, error="Execution principal has no authorized scope", error_code="forbidden")
        if validate_json_schema(args, definition.input_schema):
            return ToolResult(success=False, error="Tool arguments do not match its registered schema", error_code="invalid_arguments")
        try:
            if len(json.dumps(args, allow_nan=False, separators=(",", ":")).encode()) > definition.max_arguments_bytes:
                return ToolResult(success=False, error="Tool arguments exceed their size limit", error_code="invalid_arguments")
        except (TypeError, ValueError, RecursionError):
            return ToolResult(success=False, error="Tool arguments must be JSON data", error_code="invalid_arguments")
        approval_required = definition.risk != ToolRisk.READ_ONLY or definition.confirmation_required
        approval_verifier = execution_context.get("approval_verifier")
        try:
            trusted_approval = bool(
                approval_required and callable(approval_verifier)
                and await approval_verifier(definition, args, principal, "admission")
            )
        except Exception:
            trusted_approval = False
        decision = self.policy.evaluate(
            principal.actor_id, definition, args, trusted_approval=trusted_approval,
        )
        if not decision.allowed:
            return ToolResult(success=False, error="Tool action is not authorized", error_code="approval_required")
        handler = self._handlers[name]
        try:
            async with asyncio.timeout(definition.timeout_seconds):
                async with self._semaphore:
                    # Queue wait can outlive a capability revocation or registry replacement.
                    principal_revalidator = execution_context.get("principal_revalidator")
                    principal_current = (
                        await principal_revalidator(principal)
                        if callable(principal_revalidator) else False
                    )
                    queued_definition = self._tools.get(name)
                    try:
                        queued_approval = bool(
                            approval_required and callable(approval_verifier) and queued_definition is not None
                            and await approval_verifier(queued_definition, args, principal, "dispatch")
                        ) if principal_current else False
                    except Exception:
                        queued_approval = False
                    # Resolve again after the awaited owner recheck; no await separates this from dispatch.
                    registered = self._tools.get(name)
                    current_handler = self._handlers.get(name)
                    if (
                        not principal_current
                        or registered is None
                        or registered.version != definition.version
                        or self._fingerprints.get(name) != definition.schema_fingerprint
                        or registered.schema_fingerprint != definition.schema_fingerprint
                        or current_handler is not handler
                        or not self._module_enabled(registered.module)
                        or name not in principal.allowed_tools
                        or not set(registered.permissions).issubset(principal.capabilities)
                        or destination not in principal.destinations
                        or not principal.actor_id
                        or (not principal.is_owner and not principal.source_ids)
                        or (principal.owner_all_sources and not principal.is_owner)
                        or not self.policy.evaluate(
                            principal.actor_id, registered, args,
                            trusted_approval=queued_approval,
                        ).allowed
                    ):
                        return ToolResult(success=False, error="Tool authorization changed while queued", error_code="forbidden")
                    raw = await handler(args, execution_context)
            if isinstance(raw, ToolResult):
                if not raw.success:
                    code = raw.error_code if raw.error_code in ALLOWED_ERROR_CODES else "execution_failed"
                    failure = ToolResult(success=False, error="Tool execution failed", error_code=code)
                    if len(failure.model_dump_json().encode()) > definition.max_result_bytes:
                        return ToolResult(success=False, error="Tool result is unavailable", error_code="invalid_result")
                    return failure
                raw_data = raw.data
                evidence_refs = raw.evidence_refs
            else:
                raw_data = raw
                evidence_refs = ()
            payload = jsonable_encoder(raw_data)
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            refs = tuple(evidence_refs)
            if (
                len(encoded) > definition.max_result_bytes
                or len(refs) > MAX_EVIDENCE_REFS
                or sum(len(value.encode("utf-8")) for value in refs if isinstance(value, str)) > MAX_EVIDENCE_BYTES
                or any(not isinstance(value, str) or not value or len(value) > 256 for value in refs)
                or validate_json_schema(payload, definition.output_schema)
            ):
                return ToolResult(success=False, error="Tool returned an invalid or oversized result", error_code="invalid_result")
            result = ToolResult(
                success=True, data=payload, evidence_refs=refs,
                execution_time_ms=(time.perf_counter()-started)*1000,
            )
            # Bound the actual public envelope, including evidence IDs and status metadata.
            if len(result.model_dump_json().encode()) > definition.max_result_bytes:
                return ToolResult(success=False, error="Tool returned an invalid or oversized result", error_code="invalid_result")
            return result
        except TimeoutError:
            return ToolResult(success=False, error="Tool execution timed out", error_code="timeout", execution_time_ms=(time.perf_counter()-started)*1000)
        except Exception as exc:
            logger.warning("Tool dispatch failed (%s)", type(exc).__name__)
            return ToolResult(success=False, error="Tool execution failed", error_code="execution_failed", execution_time_ms=(time.perf_counter()-started)*1000)
