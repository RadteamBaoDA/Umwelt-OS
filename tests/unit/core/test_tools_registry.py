"""Unit tests for agent tool contracts, schemas, policy, and ToolRegistry.

Tests ToolRisk, ToolDestination, ToolDefinition, argument hashing, approval grants,
ToolPolicy evaluation, and bounded async ToolRegistry dispatch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.tools.policy import ToolPolicy
from core.tools.registry import ToolRegistry
from core.tools.schemas import (
    ToolApprovalGrant,
    ToolDefinition,
    ToolDestination,
    ToolExecutionPrincipal,
    ToolOutputFence,
    ToolRisk,
    compute_argument_hash,
)
from core.tools.validator import check_json_schema, validate_json_schema


class TestToolSchemas:
    """Test suite for tool enums, models, and helper functions."""

    def test_tool_risk_enum_and_normalization(self) -> None:
        """ToolRisk enum parses values and normalizes case-insensitively."""
        assert ToolRisk.from_str("read_only") == ToolRisk.READ_ONLY
        assert ToolRisk.from_str("READ_ONLY") == ToolRisk.READ_ONLY
        assert ToolRisk.from_str("internal_write") == ToolRisk.INTERNAL_WRITE
        assert ToolRisk.from_str("external_write") == ToolRisk.EXTERNAL_WRITE
        assert ToolRisk.from_str("destructive") == ToolRisk.DESTRUCTIVE

        with pytest.raises(ValueError, match="Unknown tool risk level"):
            ToolRisk.from_str("invalid_risk")

    def test_tool_destination_enum(self) -> None:
        """ToolDestination exposes LOCAL and REMOTE privacy classes."""
        assert ToolDestination.LOCAL.value == "local"
        assert ToolDestination.REMOTE.value == "remote"

    def test_compute_argument_hash_deterministic(self) -> None:
        """compute_argument_hash produces stable digests regardless of dict key ordering."""
        args1 = {"b": 2, "a": 1, "c": [3, 4]}
        args2 = {"a": 1, "c": [3, 4], "b": 2}

        hash1 = compute_argument_hash(args1)
        hash2 = compute_argument_hash(args2)
        assert hash1 == hash2
        assert len(hash1) == 64

    def test_tool_approval_grant_expiry_and_argument_matching(self) -> None:
        """ToolApprovalGrant verifies expiration and argument hash match."""
        now = datetime.now(UTC)
        past = now - timedelta(hours=1)
        future = now + timedelta(hours=1)

        args = {"query": "weather in Hanoi"}
        arg_hash = compute_argument_hash(args)

        grant = ToolApprovalGrant(
            grant_id="grant_123",
            tool_name="weather.lookup",
            tool_version="1.0.0",
            argument_hash=arg_hash,
            actor="user_1",
            approved=True,
            expires_at=future,
        )

        assert not grant.is_expired(now)
        assert grant.is_expired(future + timedelta(seconds=1))
        assert grant.matches_args(args)
        assert not grant.matches_args({"query": "different query"})

        expired_grant = ToolApprovalGrant(
            grant_id="grant_expired",
            tool_name="weather.lookup",
            tool_version="1.0.0",
            argument_hash=arg_hash,
            actor="user_1",
            approved=True,
            expires_at=past,
        )
        assert expired_grant.is_expired(now)

    def test_tool_definition_valid_and_schema_fingerprint(self) -> None:
        """ToolDefinition calculates a stable schema fingerprint."""
        defn = ToolDefinition(
            name="notes.search",
            version="1.0.0",
            description="Search user notes",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            output_schema={"type": "object"},
            risk=ToolRisk.READ_ONLY,
            timeout_seconds=15.0,
            module="notes",
        )

        assert defn.name == "notes.search"
        assert defn.timeout == 15.0
        assert not defn.confirmation
        assert len(defn.schema_fingerprint) == 64

    def test_tool_definition_invalid_names_and_bounds(self) -> None:
        """ToolDefinition rejects invalid names, non-positive timeouts, and oversized schemas."""
        with pytest.raises(ValidationError):
            # Invalid name: uppercase or invalid characters
            ToolDefinition(name="INVALID_NAME", module="test")

        with pytest.raises(ValidationError):
            # Timeout must be > 0
            ToolDefinition(name="valid.tool", module="test", timeout_seconds=0.0)

        with pytest.raises(ValidationError):
            # Timeout must be <= 120
            ToolDefinition(name="valid.tool", module="test", timeout_seconds=150.0)

    def test_tool_output_fence_validation(self) -> None:
        """ToolOutputFence requires strictly positive source_generation and UUIDs."""
        doc_id = uuid4()
        ver_id = uuid4()
        src_id = uuid4()

        fence = ToolOutputFence(
            document_id=doc_id,
            document_version_id=ver_id,
            source_id=src_id,
            source_generation=1,
        )
        assert fence.source_generation == 1

        with pytest.raises(ValidationError):
            ToolOutputFence(
                document_id=doc_id,
                document_version_id=ver_id,
                source_id=src_id,
                source_generation=0,
            )


class TestToolPolicy:
    """Test suite for ToolPolicy evaluation."""

    def test_evaluate_read_only_permitted(self) -> None:
        """ToolPolicy automatically permits read-only tools without confirmation requirements."""
        policy = ToolPolicy()
        defn = ToolDefinition(name="data.get", risk=ToolRisk.READ_ONLY, confirmation_required=False, module="data")
        decision = policy.evaluate("actor_1", defn, {})
        assert decision.allowed is True
        assert decision.requires_approval is False

    def test_evaluate_read_only_with_confirmation_requires_approval(self) -> None:
        """ToolPolicy requires approval for read-only tools if confirmation_required is set."""
        policy = ToolPolicy()
        defn = ToolDefinition(name="data.inspect", risk=ToolRisk.READ_ONLY, confirmation_required=True, module="data")
        decision = policy.evaluate("actor_1", defn, {})
        assert decision.allowed is False
        assert decision.requires_approval is True

    def test_evaluate_write_tools_require_trusted_approval(self) -> None:
        """ToolPolicy allows writes only when trusted_approval is verified."""
        policy = ToolPolicy()
        for risk in (ToolRisk.INTERNAL_WRITE, ToolRisk.EXTERNAL_WRITE):
            defn = ToolDefinition(name="data.modify", risk=risk, module="data")
            decision_without = policy.evaluate("actor_1", defn, {}, trusted_approval=False)
            assert decision_without.allowed is False
            assert decision_without.requires_approval is True

            decision_with = policy.evaluate("actor_1", defn, {}, trusted_approval=True)
            assert decision_with.allowed is True
            assert decision_with.requires_approval is False

    def test_evaluate_destructive_always_rejected_without_special_path(self) -> None:
        """ToolPolicy rejects DESTRUCTIVE actions even if trusted_approval is True."""
        policy = ToolPolicy()
        defn = ToolDefinition(name="data.purge", risk=ToolRisk.DESTRUCTIVE, module="data")
        decision = policy.evaluate("actor_1", defn, {}, trusted_approval=True)
        assert decision.allowed is False
        assert decision.requires_approval is True


class TestToolRegistryAsync:
    """Test suite for core.tools.registry.ToolRegistry."""

    @pytest.fixture
    def test_module_registry(self) -> dict[str, Any]:
        """Provide a descriptor registry enabling tools and sample test modules."""
        class DummyModule:
            def __init__(self, mod_id: str, dependencies: tuple[str, ...] = (), enabled: bool = True):
                self.id = mod_id
                self.dependencies = dependencies
                self.enabled = enabled

        return {
            "tools": DummyModule("tools", enabled=True),
            "sample_module": DummyModule("sample_module", enabled=True),
            "disabled_module": DummyModule("disabled_module", enabled=False),
        }

    def test_registry_concurrency_bounds(self) -> None:
        """ToolRegistry enforces max_concurrency between 1 and 32."""
        ToolRegistry(max_concurrency=1)
        ToolRegistry(max_concurrency=32)

        with pytest.raises(ValueError, match="Tool concurrency must be between 1 and 32"):
            ToolRegistry(max_concurrency=0)

        with pytest.raises(ValueError, match="Tool concurrency must be between 1 and 32"):
            ToolRegistry(max_concurrency=33)

    def test_register_and_unregister_tool(self, test_module_registry: dict[str, Any]) -> None:
        """ToolRegistry registers, retrieves, and unregisters tools."""
        registry = ToolRegistry(module_registry=test_module_registry)

        async def dummy_handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            return {"echo": args.get("val")}

        defn = ToolDefinition(
            name="sample.echo",
            version="1.0.0",
            input_schema={"type": "object", "properties": {"val": {"type": "string"}}},
            output_schema={"type": "object"},
            risk=ToolRisk.READ_ONLY,
            module="sample_module",
        )

        registry.register_tool(defn, dummy_handler)
        assert registry.get_tool("sample.echo") is not None
        assert registry.get_tool("sample.echo", "1.0.0") is not None
        assert registry.get_tool("sample.echo", "2.0.0") is None

        # Re-registering duplicate tool raises ValueError
        with pytest.raises(ValueError, match="Duplicate tool name"):
            registry.register_tool(defn, dummy_handler)

        # Unregistering tool
        assert registry.unregister_tool("sample.echo") is True
        assert registry.unregister_tool("sample.echo") is False
        assert registry.get_tool("sample.echo") is None

    def test_register_rejects_sync_handler(self, test_module_registry: dict[str, Any]) -> None:
        """ToolRegistry rejects synchronous handler functions."""
        registry = ToolRegistry(module_registry=test_module_registry)

        def sync_handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            return {}

        defn = ToolDefinition(name="sync.tool", module="sample_module")
        with pytest.raises(TypeError, match="Tool handlers must be async functions"):
            registry.register_tool(defn, sync_handler)  # type: ignore[arg-type]

    def test_list_tools_filtering(self, test_module_registry: dict[str, Any]) -> None:
        """list_tools respects module enablement and allowed_tools permissions."""
        registry = ToolRegistry(module_registry=test_module_registry)

        async def dummy_handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            return {}

        defn_enabled = ToolDefinition(name="mod.enabled", module="sample_module")
        defn_disabled = ToolDefinition(name="mod.disabled", module="disabled_module")

        registry.register_tool(defn_enabled, dummy_handler)
        registry.register_tool(defn_disabled, dummy_handler)

        all_tools = registry.list_tools()
        names = [t.name for t in all_tools]
        assert "mod.enabled" in names
        assert "mod.disabled" not in names  # Disabled module filtered out

        # Filtering with allowed_tools subset
        filtered = registry.list_tools(allowed_tools=frozenset(["other.tool"]))
        assert len(filtered) == 0

    @pytest.fixture(autouse=True)
    def mock_refresh_module_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock _refresh_module_registry to return True for unit testing without live db session."""
        from unittest.mock import AsyncMock
        monkeypatch.setattr(ToolRegistry, "_refresh_module_registry", AsyncMock(return_value=True))

    @pytest.mark.asyncio
    async def test_invoke_tool_success_read_only(self, test_module_registry: dict[str, Any]) -> None:
        """invoke_tool successfully executes an authorized read-only tool."""
        registry = ToolRegistry(module_registry=test_module_registry)

        async def add_handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            return {"sum": args["x"] + args["y"]}

        defn = ToolDefinition(
            name="math.add",
            input_schema={
                "type": "object",
                "required": ["x", "y"],
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            },
            output_schema={
                "type": "object",
                "required": ["sum"],
                "properties": {"sum": {"type": "integer"}},
            },
            risk=ToolRisk.READ_ONLY,
            module="sample_module",
        )
        registry.register_tool(defn, add_handler)

        principal = ToolExecutionPrincipal(
            actor_id="user_owner",
            is_owner=True,
            allowed_tools=frozenset(["math.add"]),
            destinations=frozenset(["local_dest"]),
        )

        async def revalidator(p: ToolExecutionPrincipal) -> bool:
            return True

        context = {
            "destination_id": "local_dest",
            "principal_revalidator": revalidator,
        }

        result = await registry.invoke_tool("math.add", {"x": 5, "y": 7}, principal, context=context)
        assert result.success is True
        assert result.data == {"sum": 12}
        assert result.error is None

    @pytest.mark.asyncio
    async def test_invoke_tool_unknown_tool(self, test_module_registry: dict[str, Any]) -> None:
        """invoke_tool returns tool_unavailable error for nonexistent tools."""
        registry = ToolRegistry(module_registry=test_module_registry)
        principal = ToolExecutionPrincipal(actor_id="user_1", is_owner=True)

        result = await registry.invoke_tool("nonexistent", {}, principal)
        assert result.success is False
        assert result.error_code == "tool_unavailable"

    @pytest.mark.asyncio
    async def test_invoke_tool_not_allowed_for_principal(self, test_module_registry: dict[str, Any]) -> None:
        """invoke_tool returns forbidden if tool is not in principal.allowed_tools."""
        registry = ToolRegistry(module_registry=test_module_registry)

        async def noop(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            return {}

        defn = ToolDefinition(name="restricted.tool", module="sample_module")
        registry.register_tool(defn, noop)

        principal = ToolExecutionPrincipal(actor_id="user_1", is_owner=True, allowed_tools=frozenset())
        result = await registry.invoke_tool("restricted.tool", {}, principal)
        assert result.success is False
        assert result.error_code == "forbidden"

    @pytest.mark.asyncio
    async def test_invoke_tool_invalid_argument_schema(self, test_module_registry: dict[str, Any]) -> None:
        """invoke_tool returns invalid_arguments when input fails JSON schema validation."""
        registry = ToolRegistry(module_registry=test_module_registry)

        async def noop(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            return {}

        defn = ToolDefinition(
            name="strict.input",
            input_schema={"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}},
            module="sample_module",
        )
        registry.register_tool(defn, noop)

        principal = ToolExecutionPrincipal(
            actor_id="user_1", is_owner=True,
            allowed_tools=frozenset(["strict.input"]),
            destinations=frozenset(["dest"]),
        )
        context = {"destination_id": "dest"}

        # Passing string instead of integer
        result = await registry.invoke_tool("strict.input", {"count": "not-an-int"}, principal, context=context)
        assert result.success is False
        assert result.error_code == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_invoke_tool_approval_required_rejection(self, test_module_registry: dict[str, Any]) -> None:
        """invoke_tool returns approval_required for write actions without approval verifier."""
        registry = ToolRegistry(module_registry=test_module_registry)

        async def write_handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
            return {"status": "written"}

        defn = ToolDefinition(name="db.write", risk=ToolRisk.INTERNAL_WRITE, module="sample_module")
        registry.register_tool(defn, write_handler)

        principal = ToolExecutionPrincipal(
            actor_id="user_1", is_owner=True,
            allowed_tools=frozenset(["db.write"]),
            destinations=frozenset(["dest"]),
        )
        context = {"destination_id": "dest"}

        result = await registry.invoke_tool("db.write", {}, principal, context=context)
        assert result.success is False
        assert result.error_code == "approval_required"


class TestJsonSchemaValidators:
    """Test suite for check_json_schema and validate_json_schema."""

    def test_check_json_schema_valid(self) -> None:
        """check_json_schema accepts valid Draft 2020-12 schemas."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        check_json_schema(schema)

    def test_check_json_schema_rejects_remote_ref(self) -> None:
        """check_json_schema rejects remote $ref URLs."""
        schema = {
            "type": "object",
            "properties": {"user": {"$ref": "https://example.com/schema.json"}},
        }
        with pytest.raises(ValueError, match="Remote schema references are not permitted"):
            check_json_schema(schema)

    def test_validate_json_schema_returns_clean_errors(self) -> None:
        """validate_json_schema validates instances without echoing sensitive payload values."""
        schema = {
            "type": "object",
            "properties": {
                "age": {"type": "integer", "minimum": 0},
            },
            "required": ["age"],
        }
        # Missing required field
        errors = validate_json_schema({}, schema)
        assert len(errors) > 0
        assert "Invalid value at $" in errors[0]

        # Valid payload
        assert validate_json_schema({"age": 25}, schema) == []
