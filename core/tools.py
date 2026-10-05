"""Agent tool definitions, validation contracts, and dynamic tool registry.

Provides the shared ToolDefinition and ToolRegistry interfaces specified in
the canonical modular architecture (Section 138.17 and Section 149).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolDefinition:
    """Agent tool contract declaring identity, purpose, input/output schemas, and risk.

    Attributes:
        name: Unique dot-separated or snake_case tool name (e.g. 'tasks.create').
        purpose: Concise human-readable description of what the tool accomplishes.
        input_schema: JSON Schema dictionary describing accepted tool arguments.
        output_schema: JSON Schema dictionary describing returned payload structure.
        risk_level: Security risk classification ('low', 'medium', 'high').
        confirmation_policy: User confirmation gate ('auto', 'ask', 'explicit').
        handler: Optional async callable executing the tool with session and parameters.
    """

    name: str
    purpose: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    confirmation_policy: str = "auto"
    handler: Callable[..., Awaitable[Any]] | None = None


class ToolRegistry:
    """In-memory agent tool registry allowing capability modules to register tools dynamically."""

    def __init__(self) -> None:
        """Initialize an empty dictionary of registered tool definitions."""
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition by its unique name, rejecting duplicate conflicting names.

        Args:
            tool: ToolDefinition to register.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        if tool.name in self._tools and self._tools[tool.name] != tool:
            raise ValueError(f"Tool {tool.name} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        """Lookup a registered tool definition by name.

        Args:
            name: Unique name of the tool.

        Returns:
            The matching ToolDefinition, or None if not found.
        """
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """Return all currently registered tool definitions.

        Returns:
            List of ToolDefinition instances.
        """
        return list(self._tools.values())


tool_registry = ToolRegistry()
