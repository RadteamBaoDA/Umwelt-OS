from core.model_gateway.client import effective_provider_kind, hosted_endpoint_equivalent
from core.model_gateway.schemas import (
    HOSTED_PROVIDER_ENDPOINTS,
    ConnectionDraft,
    DraftProbeRequest,
    ProviderModel,
)
from modules.settings.public import role_accepts


def test_fixed_endpoint_map_is_exact():
    assert HOSTED_PROVIDER_ENDPOINTS == {
        "openai": "https://api.openai.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "anthropic": "https://api.anthropic.com/v1",
    }


def test_equivalence_uses_effective_api_base():
    assert hosted_endpoint_equivalent("openai", "https://api.openai.com")
    assert hosted_endpoint_equivalent("openai", "https://API.openai.com/v1/")
    assert not hosted_endpoint_equivalent("openai", "https://omniroute.example/v1")
    assert not hosted_endpoint_equivalent(
        "gemini", "https://generativelanguage.googleapis.com/v1beta"
    )
    assert not hosted_endpoint_equivalent("custom", "https://api.openai.com/v1")


def test_effective_kind_converts_only_non_equivalent_hosted_rows():
    assert effective_provider_kind("openai", "https://omniroute.example/v1") == "custom"
    assert effective_provider_kind("openai", "https://api.openai.com") == "openai"
    assert effective_provider_kind("ollama", "http://host.docker.internal:11434") == "ollama"
    assert effective_provider_kind("anthropic", None) == "anthropic"


def test_drafts_accept_missing_base_url():
    assert ConnectionDraft(kind="openai").base_url is None
    assert DraftProbeRequest(kind="groq", model="m", capability="chat").base_url is None


def test_new_output_types_serve_no_role():
    for t in ("image", "video", "speech", "audio", "transcription"):
        model = ProviderModel(id="x/y", type=t)
        assert not any(
            role_accepts(r, model)
            for r in ("chat", "brief", "background", "embedding", "reranker", "vision")
        )
