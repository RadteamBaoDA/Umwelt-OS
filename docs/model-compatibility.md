# Model compatibility record

This record separates repository configuration and source-verified safeguards from facts that require a live connector/provider check. No live provider probe or prompt was sent for this record.

Gateway transports use the locked HTTPX 0.28.1 public transport API. Each request resolves and checks all returned IP addresses against `AI_ALLOWED_ENDPOINT_CIDRS`, pins the connection to an approved numeric address, preserves the configured host for Host/TLS verification, and disables redirects and environment proxies. Host and CIDR deployment allowlists are empty by default. This is source-verified policy; runtime DNS/TLS/provider behavior remains unverified.

## Current record

| Field | Value | Evidence |
|---|---|---|
| Gateway/provider | User-configured OpenAI-compatible connectors (Settings > Models) | `modules/search/indexing.py`; `.env.example` |
| Configured endpoint URL | Unknown at runtime; set per connector in Settings > Models (no `OMNIROUTE_*` env vars) | Database settings; no value is recorded here |
| Model types | Per-model type (chat, embedding, reranker, decision, background; legacy vision/asr/tts read-only) plus a vision flag; role defaults accept only matching types | Settings > Models |
| Configured embedding model ID/version | Unknown in this checkout's runtime | The owner settings row is canonical after first save; before it exists, deployment defaults and legacy Redis mappings are bootstrap inputs |
| Provider-returned embedding model identity | Unknown until a successful embedding response supplies the identity and indexing persists it; if the response omits it, it remains unknown | Search indexing stores a supplied response identity on the index generation and pins the gateway identity separately |
| Embedding dimensions | Unknown until a successful embedding response supplies a vector and indexing persists its dimensions | Search indexing pins response dimensions on the index generation |
| Remote embedding consent | Disabled by default; owner opt-in is required | PostgreSQL owner settings bind separate grants to the configured destination; missing legacy consent remains denied |
| Source privacy routing | Search indexing excludes `local_only` sources and only sends through the configured connector destination | `modules/search/indexing.py`, `core/model_gateway/policy.py` |
| Endpoint data handling / residency / retention guarantees | Unknown | A configured gateway URL does not establish provider-side guarantees |
| Embeddings capability | Unknown until a successful capability probe is recorded for the configured role/model/version and gateway identity | Model gateway requires supported Redis TTL evidence bound to endpoint/credential identity before requests |
| Other capabilities (chat, streaming, structured output, tools, reranking) | Unknown until separately probed | Capabilities are recorded independently and expire after 24 hours |

## Compatibility rules

- Pin embedding model identity and dimensions to each index generation. Reindex after model changes; do not mix models or silently fall back.
- Treat configured model names and stub responses as configuration/evidence only. They do not prove endpoint release, privacy routing, data handling, or capability support.
- Keep unverified values `unknown`. Record live verification separately with the check time and the exact gateway/model configuration it covered.
- If the endpoint's destination guarantees cannot be established, do not route local-only data to it.

## Update procedure

After an authorized live compatibility check, record the gateway release, endpoint identity, configured and returned model IDs, dimensions, privacy/data-handling evidence, and each capability result with its check time. Do not record credentials, prompts, source text, or personal data here.
