"""Unit tests for core module registry and dependency validation.

Tests central module registration, duplicate ID detection, missing dependency
enforcement, and default production descriptor composition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import pytest

from core.modules import register_modules


@dataclass(frozen=True)
class MockDescriptor:
    """Mock module descriptor for dependency graph validation."""

    id: str
    dependencies: list[str] = field(default_factory=list)


class TestRegisterModules:
    """Test suite for register_modules."""

    def test_default_production_modules_registration(self) -> None:
        """Calling register_modules without arguments registers all production modules cleanly."""
        registry = register_modules()
        assert isinstance(registry, dict)
        assert len(registry) == 20

        # Verify all canonical module IDs are present
        expected_modules = {
            "sources", "knowledge.documents", "knowledge.entities", "knowledge.relationships",
            "knowledge.timeline", "search", "knowledge.temporal", "dashboard", "tasks",
            "goals", "news", "notifications", "chat", "memory",
            "tools", "agents", "automations", "observability", "ingestion", "connectors",
        }
        assert set(registry.keys()) == expected_modules

        # Verify all dependencies are satisfied across the registered descriptors
        for mod_id, descriptor in registry.items():
            for dep in descriptor.dependencies:
                assert dep in registry, f"Module {mod_id} has unmet dependency {dep}"

    def test_empty_descriptors(self) -> None:
        """register_modules with an empty sequence returns an empty registry."""
        registry = register_modules([])
        assert registry == {}

    def test_valid_independent_and_dependent_descriptors(self) -> None:
        """register_modules successfully registers valid dependency graphs."""
        descriptors = [
            MockDescriptor(id="base"),
            MockDescriptor(id="service", dependencies=["base"]),
            MockDescriptor(id="client", dependencies=["service", "base"]),
        ]
        registry = register_modules(descriptors)
        assert len(registry) == 3
        assert set(registry.keys()) == {"base", "service", "client"}

    def test_duplicate_module_id_rejected(self) -> None:
        """register_modules raises ValueError when duplicate module IDs are encountered."""
        descriptors = [
            MockDescriptor(id="users"),
            MockDescriptor(id="auth"),
            MockDescriptor(id="users"),
        ]
        with pytest.raises(ValueError, match="Duplicate module id: users"):
            register_modules(descriptors)

    def test_missing_single_dependency_rejected(self) -> None:
        """register_modules raises ValueError when a declared dependency is not registered."""
        descriptors = [
            MockDescriptor(id="auth", dependencies=["database"]),
        ]
        with pytest.raises(ValueError, match="Module auth has missing dependencies: database"):
            register_modules(descriptors)

    def test_missing_multiple_dependencies_sorted_in_message(self) -> None:
        """register_modules formats multiple missing dependencies in sorted order."""
        descriptors = [
            MockDescriptor(id="app", dependencies=["redis", "postgres", "kafka"]),
        ]
        with pytest.raises(
            ValueError, match="Module app has missing dependencies: kafka, postgres, redis"
        ):
            register_modules(descriptors)

    def test_transitive_missing_dependency(self) -> None:
        """register_modules fails when a transitive dependency in a chain is missing."""
        descriptors = [
            MockDescriptor(id="ui", dependencies=["api"]),
            MockDescriptor(id="api", dependencies=["cache"]),
            # 'cache' is missing
        ]
        with pytest.raises(ValueError, match="Module api has missing dependencies: cache"):
            register_modules(descriptors)

    def test_closed_cycle_satisfied_in_registry(self) -> None:
        """register_modules allows mutually referring modules as long as all are present."""
        # Note: core.modules validates presence in registry.keys(), not strict DAG acyclicity.
        descriptors = [
            MockDescriptor(id="mod_a", dependencies=["mod_b"]),
            MockDescriptor(id="mod_b", dependencies=["mod_a"]),
        ]
        registry = register_modules(descriptors)
        assert "mod_a" in registry and "mod_b" in registry
