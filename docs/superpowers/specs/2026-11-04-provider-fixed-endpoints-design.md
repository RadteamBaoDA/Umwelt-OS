# Fixed-endpoint AI providers and the OpenRouter model browser

Date: 2026-11-04 · Branch: `feature/fix-ui` · Status: draft for owner approval

## Why

Right now every provider card in Settings → AI shows a free-text Base URL field, even for hosted vendors whose endpoint never
changes. Owners mistype it, paste the wrong one (e.g. Gemini without `/v1beta/openai`), or leave a stale value after switching
the kind. Because every call goes through the OpenAI SDK, the result is that some vendor features work and others fail
quietly. On top of that, picking an OpenRouter model means scrolling a flat list of hundreds of ids, with no way to find
image, speech, embedding, rerank or decision models.

## Goals

1. **Hosted kinds need only a key.** For `openai`, `openrouter`, `groq`, `gemini` and `anthropic`, the UI shows no URL field.
   The server fixes the endpoint per kind and ignores or rejects any client-sent `base_url`.
2. **Self-hosted kinds keep the URL.** `ollama`, `lmstudio` and `custom` keep the Base URL field exactly as today, including the
   Docker host hint.
3. **Per-vendor calls follow each vendor's current docs.** The builder checks each vendor's docs at build time; the table
   below is a snapshot from 2026-11-04. That covers chat (incl. streaming, tools, structured output), embeddings where the
   vendor offers them, and model discovery.
4. **The OpenRouter model browser supports every filter the OpenRouter models page and API offer.** It is used when adding a
   model for an OpenRouter provider in the Models section.
5. **Models of any OpenRouter output type can be added** (image, video, speech, audio, transcription, …), even though the app
   cannot use them yet. They are stored and listed, and later features will consume them.

## Non-goals

- New app features that *use* image/video/speech/audio/transcription models (generation, TTS, ASR). Later work.
- A filter browser for non-OpenRouter providers. They keep the existing searchable select.
- Changing the visualization router's own OpenRouter/Cloudflare card (`viz-router-card.tsx`), which already has a fixed
  endpoint and the Decisions filter.
- Exposing a URL override for hosted kinds, even under an "advanced" toggle.

## Anti-goals (would be a failure even if it "works")

- Silent loss of an owner's existing provider configuration (URL, key or models) during migration.
- Sending a stored key to a different host than the one it was saved for (the current `resolve_draft_provider` invariant).
- Weakening endpoint network policy (`_endpoint`, `ai_allowed_endpoint_cidrs`, `ai_allow_public_providers`) for fixed
  endpoints. They still pass the same policy check.
- Calling OpenRouter `/models` from the browser with the owner's key. All vendor calls stay server-side.

## Current state (verified in code)

- Kinds: `core/model_gateway/schemas.py:7` `ProviderKind`; local kinds `LOCAL_PROVIDER_KINDS = {ollama, lmstudio}`.
- Model types: `ModelType = llm | embedding | reranker | decision | background | vision | asr | tts` (vision/asr/tts legacy,
  read-only in UI). `ProviderModel{id,type,vision}`.
- Frontend presets with default URLs: `apps/web/src/modules/settings/provider-editor.tsx:50` (`providerPresets`); URL input at
  line 133; switching kind rewrites the URL only if it still equals the old preset.
- Discovery: `POST /api/v1/settings/ai/discover` (`modules/settings/routes.py:156`) takes `ConnectionDraft{base_url, api_key,
  provider_id, kind}` and calls `ModelGateway.discover_models()` (`core/model_gateway/client.py:645`), which runs
  `AsyncOpenAI.models.list()` and returns only ids.
- OpenRouter Decisions discovery already exists (`discover_decision_models`, `client.py:677`, with `output_modalities` param);
  reuse its pattern.
- All chat/embeddings/rerank go through `AsyncOpenAI` against `_api_base(base_url)`.
- Model picker: `apps/web/src/modules/settings/models-section.tsx` (`ModelsPanel`), backed by `discovered: Record<providerId, string[]>`.

## Vendor contract snapshot (re-verify when building)

| Kind | Fixed endpoint | Auth | Chat | Model list | Notes |
|---|---|---|---|---|---|
| openai | `https://api.openai.com/v1` | Bearer | `/chat/completions` | `GET /models` | embeddings native |
| openrouter | `https://openrouter.ai/api/v1` | Bearer | `/chat/completions` | `GET /models` with filter params (below) | embeddings `/embeddings`, rerank `/rerank` |
| groq | `https://api.groq.com/openai/v1` | Bearer | `/chat/completions` | `GET /models` | no embeddings |
| gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | Bearer | OpenAI-compat `/chat/completions` | OpenAI-compat `GET /models` (or native `v1beta/models` for `supportedGenerationMethods`) | embeddings via compat |
| anthropic | `https://api.anthropic.com/v1` | `x-api-key` + `anthropic-version` | native `POST /v1/messages` | native `GET /v1/models` (`x-api-key`, `anthropic-version: 2023-06-01`) | no embeddings |

Anthropic's own docs (platform.claude.com, OpenAI SDK compatibility page) say the OpenAI-compat layer is "primarily intended
to test and compare … not considered a long-term or production-ready solution". Known limits that hit this app:
`response_format` only accepts `json_schema` + `strict:true` (`json_object` → 400); `tool_choice` forcing → 400 on the newest
models; `temperature < 1` → 400 on Claude 4.7+; `reasoning_effort` ignored; no prompt caching.

### OpenRouter `GET /api/v1/models` filters (from openrouter.ai docs, 2026-11-04)

`output_modalities` (text, image, embeddings, audio, video, rerank, decisions, speech, transcription, all; default text),
`input_modalities` (text, image, audio, file), `category`, `supported_parameters`, `sort` (most-popular, newest, top-weekly,
pricing low→high / high→low, context high→low, throughput, latency, intelligence, coding, agentic, design-arena), `endpoints`,
`q` (search), `context` (min), `min_price`/`max_price`, `min_output_price`/`max_output_price`, `min_age_days`/`max_age_days`,
`arch`, `model_authors`, `providers`, `distillable`, `zdr`, `region` (eu/us), intelligence/coding/agentic index ranges,
tool-success-rate range, `offset`/`limit` (≤1000). Response: `data[]` with `id, name, description, created, context_length,
architecture{input_modalities, output_modalities}, pricing, supported_parameters, endpoints, reasoning, benchmarks`, plus
`total_count` and `links.next`.

## Design decisions (owner-confirmed)

- Hosted kinds: no URL field, API key only. Self-hosted kinds: URL kept.
- OpenRouter browser: **all** filters above are supported, modelled on the OpenRouter models page (owner screenshot): tab row
  All / Text / Image / Video / Speech / Decisions / Embeddings / Rerank / Transcription / Audio; search box; sort select (default
  Newest); plus the remaining filters.
- Models of any OpenRouter output type may be added now; using them comes later.

## Design

### Backend

1. **Fixed endpoint map** in `core/model_gateway/schemas.py` (or settings public), e.g. `HOSTED_PROVIDER_ENDPOINTS: dict[kind, url]`.
   This becomes the single source of truth. The frontend `providerPresets.baseUrl` for hosted kinds becomes display-only or is
   removed.
2. **Save path** (`modules/settings/public.py` provider save, ~line 620): for hosted kinds, the stored `base_url` is always the
   fixed endpoint. A client-sent `base_url` is ignored (builder's call: ignore vs 422 on mismatch; ignoring is friendlier to
   the existing UI drafts). It still passes `_endpoint()` policy validation.
3. **Draft/discover/probe** (`ConnectionDraft`, `DraftProbeRequest`): `base_url` becomes optional; for hosted kinds the server
   derives it from `kind`. The stored-key fallback in `resolve_draft_provider` keeps its "same endpoint only" rule.
4. **Per-vendor discovery** behind `discover_models()`: dispatch on kind where the vendor differs (Anthropic native headers;
   OpenRouter filtered call). Others keep `models.list()`.
5. **OpenRouter browse endpoint**, new: e.g. `POST /api/v1/settings/ai/openrouter/models` with an allowlisted filter object
   (each param validated against the enumerations above; unknown keys rejected) plus `provider_id` and an optional draft key.
   It returns a bounded, validated projection per model: `id, name, created, context_length, input_modalities,
   output_modalities, pricing.prompt/completion, supported_parameters`, plus `total_count` and paging. Reuse the
   `discover_decision_models` pattern (slot, before_send recheck, static error reasons) and `parse_decision_models`-style
   validation.
6. **Model types**: extend `ModelType` with the OpenRouter output types the app can't use yet (proposal: `image`, `video`,
   `speech`, `audio`, `transcription`, `rerank` → map to existing `reranker`, `embeddings` → `embedding`, `decisions` →
   `decision`, `text` → `llm`). The existing legacy `asr`/`tts` either map to transcription/speech or stay as-is (builder's
   call; must keep reading old rows). New types are **not selectable** for any role default and have no "Test" action.
7. **Migration of existing rows** (my call). Equivalence is judged on the effective API base (`_api_base(url)`), not on an
   exact string match. Two cases:
   - **Equivalent rows** (e.g. `https://api.openai.com` vs `https://api.openai.com/v1`) stay on their hosted kind. The stored
     `base_url` is **kept verbatim**, including on later saves while it stays equivalent. Rewriting it would change the
     `destination_id` fingerprint (`_identity`), which would clear privacy consent and make `_kept_ciphertext` drop the key.
     Only new hosted providers store the canonical fixed endpoint.
   - **Non-equivalent rows** (e.g. an `openai` row pointing at OmniRoute) are converted to `kind: custom`. URL, key, models and
     destination stay untouched.

   Either a read-time normalisation or a one-off data migration is fine (builder's call), with tests for both cases.

### Frontend

1. `provider-editor.tsx`: hide the Base URL field for hosted kinds and show the fixed endpoint as read-only muted text (my
   call, for transparency). The Discover button is no longer gated on `base_url` for hosted kinds. Switching kind from a
   self-hosted to a hosted kind drops the typed URL.
2. `models-section.tsx`: when the selected provider is `openrouter`, replace the plain `SearchableSelect` with an
   **OpenRouter model browser**:
   - Tab row of output modalities (All, Text, Image, Video, Speech, Decisions, Embeddings, Rerank, Transcription, Audio).
   - Search box (`q`, debounced), sort select (default Newest).
   - "More filters" area for the remaining params (input modalities, category, supported parameters, context min, price
     ranges, age, authors, providers, arch, region, ZDR, distillable, benchmark index ranges, endpoints).
   - Result list: name/id, context, input→output modalities, prompt/completion price; selecting one fills the add row. Type
     is pre-set from the active tab / the model's output modality and can still be changed.
   - Paging via `offset`/`limit`.
   - The "Pinned" toggle and the List/Table view switch from the screenshot are OpenRouter-site UI features, not API filters.
     Proposal: omit them (builder notes this; owner can ask later).
3. i18n: all new strings in `en-us` and `vi-vi`; shadcn primitives and semantic tokens only (`umwelt-os-ui-system` skill).
4. Type select in the add row shows the new types only for OpenRouter models. Role-default selects never list them.

### Tests

- Backend: fixed endpoint enforced on save/draft/discover; stored key never sent to another host; migration of
  hosted-kind rows with custom URL → `custom`; OpenRouter filter allowlist validation and response bounding; Anthropic
  discovery headers.
- Frontend (Vitest): URL field hidden/shown per kind; browser tabs/search/sort produce the right request; adding an
  image-type model; role defaults exclude new types.

## Anthropic chat path (owner decision 2026-11-04: native adapter)

Add a native Claude Messages API transport to the gateway for `kind == "anthropic"`. Do **not** use the OpenAI-compat layer.

- `ModelGateway` must learn the provider kind (passed alongside `base_url`/`api_key`/`destination_id`). Chat, stream, tools,
  structured output, probe and discovery route to the native transport when the kind is `anthropic`. All existing fences
  still apply to both transports: privacy, before_send recheck, slot/lease, breaker, retry/Retry-After, endpoint policy and
  static error reasons.
- Translate the gateway's internal OpenAI-shaped requests/responses at the transport boundary, so callers (chat worker,
  harness, briefs, translations, …) do not change:
  - system messages → top-level `system`;
  - tools/tool_calls ↔ `tool_use`/`tool_result` blocks;
  - `max_tokens` is required;
  - structured output → native structured outputs;
  - `reasoning_effort` → the model's thinking/effort setting;
  - stream SSE events → the gateway's existing stream chunk shape;
  - usage → existing metrics.
- Respect the current model constraints from Anthropic docs (e.g. no forced `tool_choice` on newest models, sampling-param
  limits). Re-read the docs at build time; use the `claude-api` skill as the reference.
- Anthropic has no embeddings or rerank. Those capabilities report `unsupported` for this kind.
- Use the official `anthropic` Python SDK if it fits the gateway's injected `http_client`/network-policy transport; otherwise use
  raw `httpx` (builder's call, but it must keep the endpoint-policy transport).
- Tests: request translation (system, tools, structured output, effort), stream translation incl. tool-use deltas, error
  mapping (429/Retry-After, 5xx, auth), and that callers' existing gateway tests still pass unchanged.

## Left to the builder

- Exact endpoint/route names, file split, and whether filter state lives in URL or component state.
- Debounce timing, page size (≤ 1000 per API), caching of browse results (short TTL is fine).
- Whether Gemini discovery uses the compat or native list.
