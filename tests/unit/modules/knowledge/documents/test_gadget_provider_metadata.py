"""Dashboard projection metadata must accept every registered provider id."""

from modules.knowledge.documents.schemas import PROVIDER_IDS, GadgetProviderMetadataRead


def test_every_registered_provider_is_projectable() -> None:
    """A provider that can store typed record metadata must not 422 the dashboard projection page."""
    for provider in PROVIDER_IDS:
        assert GadgetProviderMetadataRead(provider=provider, source_fields={}).provider == provider
