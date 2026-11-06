import pytest

from core.errors import DomainError
from core.model_gateway.schemas import ProviderConfig
from modules.settings import public
from tests.unit.modules.settings.test_providers import _derived_config, _settings

settings = _settings(public_ok=True)
config = _derived_config().model_copy(
    update={
        "providers": {
            "oa": ProviderConfig(
                kind="openai",
                base_url="https://api.openai.com",
                api_key="sk-saved",
                credential_configured=True,
                destination_id="remote:oa",
            )
        }
    }
)


def test_hosted_draft_ignores_client_url():
    endpoint, kind = public.resolve_draft_endpoint(
        config, "https://evil.example/v1", "groq", None, settings
    )
    assert (endpoint, kind) == ("https://api.groq.com/openai/v1", "groq")


def test_draft_for_saved_hosted_provider_reuses_stored_verbatim_url_and_key():
    endpoint, kind = public.resolve_draft_endpoint(config, None, "openai", "oa", settings)
    assert endpoint == "https://api.openai.com"
    assert public.resolve_draft_provider(config, endpoint, "", "oa", kind)[0] == "sk-saved"


def test_stored_key_never_follows_a_kind_switch():
    endpoint, kind = public.resolve_draft_endpoint(config, None, "anthropic", "oa", settings)
    assert endpoint == "https://api.anthropic.com/v1"
    assert public.resolve_draft_provider(config, endpoint, "", "oa", kind)[0] == ""


def test_self_hosted_draft_requires_url():
    with pytest.raises(DomainError) as exc:
        public.resolve_draft_endpoint(config, None, "ollama", None, settings)
    assert exc.value.status_code == 422
