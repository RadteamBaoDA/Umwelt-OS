from core.model_gateway.schemas import ModelMapping, RequestPolicy

LOCAL_DESTINATION_PREFIX = "local:"


def may_send(
    policy: RequestPolicy,
    alias: str,
    mapping: ModelMapping | None,
    destination_id: str,
    credential_configured: bool,
    capability: str,
) -> bool:
    """Decide whether a configured capability may be sent under destination, credential, and privacy policy.

    ``local:`` destinations (Ollama/LM Studio) stay on the owner's network: no credential, remote marker or
    consent is required, but they must still be permitted and never serve ``local_only`` sources.
    """
    if mapping is None or not mapping.model.strip():
        return False
    if policy.local_only or destination_id not in policy.permitted_destinations:
        return False
    if destination_id.startswith(LOCAL_DESTINATION_PREFIX):
        return capability not in {"web_search", "search"}
    if (
        not credential_configured
        or alias == "local-private"
        or mapping.destination != "remote"
    ):
        return False
    if capability in {"embeddings", "reranking"}:
        destinations = policy.embedding_destinations or policy.permitted_destinations
        return policy.embeddings_allowed and destination_id in destinations
    if capability in {"web_search", "search"}:
        return policy.web_search_allowed and destination_id in policy.web_search_destinations
    destinations = policy.reasoning_destinations or policy.permitted_destinations
    return policy.reasoning_allowed and destination_id in destinations
