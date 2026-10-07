"""Connector-owned capability identities, separate from runtime admission and trust."""

from types import MappingProxyType
from typing import Literal, TypeGuard

CollectionBackend = Literal["native", "n8n"]

PROVIDER_SOURCE_TYPES = MappingProxyType({
    "youtube": "rss", "arxiv": "rss", "huggingface": "api",
    "github_releases": "api", "github": "api", "telegram": "api",
    "alpha_vantage": "api", "open_meteo": "api",
})
NATIVE_PROVIDERS = frozenset(PROVIDER_SOURCE_TYPES)
GENERIC_CATALOG_SOURCE_TYPES = MappingProxyType({
    "rss": "rss", "web": "web", "rest": "api", "mcp": "mcp",
})
GENERIC_SOURCE_TYPES = frozenset(GENERIC_CATALOG_SOURCE_TYPES.values())


def is_native_provider(provider: str | None) -> TypeGuard[str]:
    """Identify exact registered providers eligible for typed provider provenance.

    Execution capability for a generic source never grants this identity; stored
    source configuration, collection fences and envelope checks remain required.
    """
    return provider in NATIVE_PROVIDERS


def native_dispatch_supported(source_type: str, provider: str | None) -> bool:
    """Report backend capability for a stored source type/provider pair.

    Reject unknown or mismatched providers. Generic capabilities describe the
    execution contract, not active native scheduling, runtime verification,
    credentials, eligibility or permission to submit trusted provider metadata.
    Callers must derive provider from their persisted source, not an override.
    """
    if provider is None:
        return source_type in GENERIC_SOURCE_TYPES
    return is_native_provider(provider) and PROVIDER_SOURCE_TYPES[provider] == source_type
