"""The ``modules.connectors.public`` facade keeps exporting everything the service package implements."""

import importlib

import pytest

from modules.connectors import public

SERVICE_MODULES = (
    "access", "schemas", "config", "scope", "github_binding", "github_hints", "packaged", "collection_requests",
)


@pytest.mark.parametrize("module_name", SERVICE_MODULES)
def test_every_service_definition_is_reexported_by_identity(module_name: str) -> None:
    module = importlib.import_module(f"modules.connectors.service.{module_name}")
    defined = [
        name for name, value in vars(module).items()
        if getattr(value, "__module__", None) == module.__name__ and not name.startswith("_")
        and callable(value)
    ]
    assert defined, module_name
    for name in defined:
        assert getattr(public, name) is getattr(module, name), f"{module_name}.{name}"


def test_previously_exposed_private_helpers_remain_importable() -> None:
    for name in ("_connector_access", "_connector_actor", "_read_scoped_source", "_GitHubFanoutBinding",
                 "_GitHubFanoutPage", "_validated_github_fanout_page", "_github_target_digest", "_agent_browser_scope_url",
                 "_scope_hash", "_UNSET"):
        assert hasattr(public, name), name


def test_schema_dtos_used_by_other_modules_are_reexported_lazily() -> None:
    from modules.connectors import collection_schemas
    from modules.connectors.github import schemas as github_schemas

    assert public.CollectionRequestRef is collection_schemas.CollectionRequestRef
    assert public.CollectionAdmissionRead is collection_schemas.CollectionAdmissionRead
    assert public.GitHubBindingFence is github_schemas.GitHubBindingFence
    assert public.GitHubSegmentProof is github_schemas.GitHubSegmentProof
    with pytest.raises(AttributeError):
        public.NoSuchPublicName  # noqa: B018
