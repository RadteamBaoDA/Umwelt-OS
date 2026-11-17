# Fixed-endpoint AI providers, Anthropic native transport and OpenRouter model browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hosted provider kinds (openai, openrouter, groq, gemini, anthropic) take only an API key and use a server-fixed endpoint, Anthropic runs on its native Messages API, and OpenRouter models can be browsed with every OpenRouter `/models` filter and added with any output type.

**Architecture:** One server-side map `HOSTED_PROVIDER_ENDPOINTS` is the single source of truth. Save, draft discover/probe and the read path derive or check the endpoint from `kind`. Legacy hosted rows with a non-equivalent URL become `custom` at read time and on save, so nothing is lost. `ModelGateway` learns the provider `kind`. For `anthropic`, a raw-`httpx` transport (`core/model_gateway/anthropic.py`) translates the gateway's internal OpenAI-shaped requests and responses at the boundary and reuses the same pinned `approved_http_client`, before_send, slot/lease, breaker and retry fences. A new allowlisted `POST /api/v1/settings/ai/openrouter/models` endpoint feeds a new frontend browser component.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, httpx (already a dependency, `httpx>=0.28,<1`), openai SDK (`openai>=2.20,<3`, used for its exception types and for non-Anthropic kinds), pytest. Next.js/React/TypeScript, shadcn/ui primitives (`Tabs`, `Select`, `Input`, `Checkbox`, `Table`, `Button`), TanStack Query, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-04-provider-fixed-endpoints-design.md`

## Global Constraints

- Repo root is `D:/Project/Umwelt-OS`. Pass this absolute path (forward slashes) on every shell call and ignore any injected `BBD-OS` cwd.
- Before editing any function, class or method, run GitNexus `impact({target, direction: "upstream"})` and report callers and risk. Pre-checked: `ModelGateway` is **HIGH** (17 direct, 68 total), `_resolve_provider` is **CRITICAL** (15 processes, 13 modules), `save_ai_settings` is LOW. Warn on HIGH/CRITICAL before editing. Treat `UNKNOWN` as unresolved and confirm with a text search. The index is 7 commits behind, so run `node .gitnexus/run.cjs analyze --index-only` first if results look stale.
- Before each commit, run `detect_changes({scope: "all"})`. `partial: true` or `truncated: true` does not count as a clean result.
- Every new or changed production function, method, component or hook needs a docstring (Python) or JSDoc (TS) that states its contract (inputs, output, side effects, errors, invariants). Add inline comments only at non-obvious branches. Test helpers need no docs.
- Python: 4-space indent, `snake_case`, Ruff + mypy clean. TS: 2-space indent, `camelCase` functions, `PascalCase` components/types, ESLint + `tsc` clean.
- Backend tests go under `tests/unit/...` (pytest). Frontend tests use Vitest beside the component (`*.test.tsx`). Do not run the Docker harness or make live provider calls.
- UI: follow the `umwelt-os-ui-system` skill. Use shadcn primitives from `apps/web/src/components/ui` and semantic tokens only, with no literal colours. Put every new string in both `en-us` and `vi-vi` in `apps/web/src/core/messages/ai-settings.ts`.
- Commits: one per task, Conventional style (`feat(settings): …`). The working tree already has many unrelated uncommitted changes, so `git add` **only the files listed in the task**. Never `git add -A` or `git add .`. Several listed files already carry unrelated uncommitted edits from other work (`core/model_gateway/client.py`, `schemas.py`, `modules/settings/*.py`, `modules/chat/worker.py`, `modules/agents/harness.py`, all gateway call-site files). For those, use `git add -p` and stage only this task's hunks.
- Endpoint network policy is never weakened. Every fixed endpoint still passes `public._endpoint()` (`ai_allowed_endpoint_hosts` / `ai_allow_public_providers`) and the gateway's `approved_http_client` (CIDR pinning).
- A stored key is only ever sent to the endpoint it was saved for (`resolve_draft_provider`, `_kept_ciphertext` invariants).
- Fixed endpoints (verbatim):
  `openai → https://api.openai.com/v1`, `openrouter → https://openrouter.ai/api/v1`, `groq → https://api.groq.com/openai/v1`, `gemini → https://generativelanguage.googleapis.com/v1beta/openai`, `anthropic → https://api.anthropic.com/v1`. Self-hosted kinds: `ollama`, `lmstudio`, `custom`.
- Anthropic: header `x-api-key`, `anthropic-version: 2026-10-21` (current per platform.claude.com/docs/en/api/versioning, 2026-11-04). This version **removes `temperature`/`top_p`/`top_k` on every model** (400 if sent). Never send `Authorization` to Anthropic.

## Review Focus

1. **Legacy hosted row with a custom URL** (for example `kind: openai`, URL = an OmniRoute gateway): after deploy it must still chat with the same URL, key, models and privacy consent, and appear as `custom` in the UI. Tests: Task 2 `test_legacy_hosted_row_with_custom_url_reads_as_custom_and_keeps_key` and `test_save_of_legacy_row_with_same_hosted_kind_keeps_url_as_custom`. The second covers a stale sessionStorage draft still sending `kind: openai`.
2. **Saved hosted provider with an equivalent but non-canonical URL** (`https://api.openai.com` without `/v1`): saving or discovering must not rotate `destination_id`, drop the key or clear consent. Tests: Task 2 `test_equivalent_hosted_url_is_kept_verbatim_on_save` and Task 3 `test_draft_for_saved_hosted_provider_reuses_stored_verbatim_url_and_key`.
3. **Claude tool loop with always-on thinking:** the second round must pass back the `thinking` blocks from the first round unmodified, or Anthropic returns 400. Tests: Task 7 `test_harness_keeps_reasoning_details_on_assistant_tool_turn` and `test_chat_worker_round_message_carries_stream_reasoning_details`.
4. **Forced `tool_choice` on Claude 5.x** (`any`/`tool` → 400): the translation must not send a forced choice. Test: Task 5 `test_forced_tool_choice_degrades_to_auto`.
5. **OpenRouter browse with hostile or odd input** (unknown filter key, `min > max`, oversized lists, upstream returning junk ids or a huge page): 422 before any I/O, with a bounded and validated projection. Tests: Task 8 `test_filters_reject_unknown_keys_and_inverted_ranges` and `test_parse_openrouter_models_bounds_and_skips_malformed`.

---

## File Structure

| File | Responsibility |
|---|---|
| `core/model_gateway/schemas.py` (modify) | `HOSTED_PROVIDER_ENDPOINTS`, `HOSTED_PROVIDER_KINDS`, extended `ModelType`, optional draft `base_url`, `AIExecutionConfig.gateway_kind` |
| `core/model_gateway/client.py` (modify) | `hosted_endpoint_equivalent`, `effective_provider_kind`, `ModelGateway.kind`, Anthropic dispatch, `list_openrouter_models` |
| `core/model_gateway/anthropic.py` (create) | Pure OpenAI↔Messages translation + raw-httpx send/stream/list with openai-typed error mapping |
| `modules/settings/public.py` (modify) | read-time migration, hosted save rule, `resolve_draft_endpoint`, kind threading in derived configs |
| `modules/settings/openrouter_models.py` (create) | Browse filter schema → query params, response projection |
| `modules/settings/schemas.py` (modify) | `OpenRouterBrowseRequest`, `OpenRouterModelRead`, `OpenRouterBrowseResult` |
| `modules/settings/routes.py`, `modules/model_gateway/routes.py` (modify) | draft endpoint/kind resolution, browse route |
| 16 `ModelGateway(...)` call sites (modify, one kwarg each) | pass `kind=config.gateway_kind` |
| `modules/chat/worker.py`, `modules/agents/harness.py` (modify) | carry `reasoning_details` on assistant tool turns |
| `apps/web/src/modules/settings/provider-editor.tsx` (modify) | hosted kinds: read-only endpoint, no URL input; new model types |
| `apps/web/src/modules/settings/openrouter-model-browser.tsx` (create) | tabs/search/sort/filters/results/paging |
| `apps/web/src/modules/settings/models-section.tsx`, `ai-settings.tsx` (modify) | use the browser for OpenRouter; wire the browse request |
| `apps/web/src/core/messages/ai-settings.ts` (modify) | en-us + vi-vi strings |

---

### Task 1: Hosted endpoint map, equivalence helpers, new model types, optional draft URL

**Files:**
- Modify: `core/model_gateway/schemas.py` (lines 7–18 constants; `ConnectionDraft` ~157; `DraftProbeRequest` ~166)
- Modify: `core/model_gateway/client.py` (beside `_api_base`, line 67)
- Test: `tests/unit/core/test_hosted_endpoints.py` (create)

**Interfaces:**
- Produces:
  - `HOSTED_PROVIDER_ENDPOINTS: dict[str, str]` (5 entries from Global Constraints) and `HOSTED_PROVIDER_KINDS: frozenset[str] = frozenset(HOSTED_PROVIDER_ENDPOINTS)` in `schemas.py`
  - `ModelType = Literal["llm","embedding","reranker","decision","background","vision","asr","tts","image","video","speech","audio","transcription"]`. `asr`/`tts` stay legacy and read-only, with no mapping.
  - `ConnectionDraft.base_url: AnyHttpUrl | None = None`, `DraftProbeRequest.base_url: AnyHttpUrl | None = None`
  - `hosted_endpoint_equivalent(kind: str, url: str | None) -> bool` in `client.py`: true iff `kind` is hosted, `url` is set and `_api_base(url.rstrip("/").lower()) == _api_base(HOSTED_PROVIDER_ENDPOINTS[kind])`. Lowercase only scheme and host, not the path.
  - `effective_provider_kind(kind: str, url: str | None) -> str` in `client.py`: returns `"custom"` when `kind` is hosted and `url` is set and not equivalent, else `kind`.

- [ ] **Step 1: Write the failing tests**

```python
from core.model_gateway.client import effective_provider_kind, hosted_endpoint_equivalent
from core.model_gateway.schemas import HOSTED_PROVIDER_ENDPOINTS, ConnectionDraft, DraftProbeRequest, ProviderModel
from modules.settings.public import role_accepts

def test_fixed_endpoint_map_is_exact():
    assert HOSTED_PROVIDER_ENDPOINTS == {
        "openai": "https://api.openai.com/v1", "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "anthropic": "https://api.anthropic.com/v1"}

def test_equivalence_uses_effective_api_base():
    assert hosted_endpoint_equivalent("openai", "https://api.openai.com")
    assert hosted_endpoint_equivalent("openai", "https://API.openai.com/v1/")
    assert not hosted_endpoint_equivalent("openai", "https://omniroute.example/v1")
    assert not hosted_endpoint_equivalent("gemini", "https://generativelanguage.googleapis.com/v1beta")
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
        assert not any(role_accepts(r, model) for r in ("chat", "brief", "background", "embedding", "reranker", "vision"))
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/test_hosted_endpoints.py -v`
Expected: FAIL with `ImportError: cannot import name 'HOSTED_PROVIDER_ENDPOINTS'`.

- [ ] **Step 3: Implement the constants, `ModelType` extension, optional draft `base_url`, `hosted_endpoint_equivalent` and `effective_provider_kind`** with the signatures above. Point `VIZ_OPENROUTER_URL` in `modules/settings/public.py:82` at `HOSTED_PROVIDER_ENDPOINTS["openrouter"]` so there is one source.

- [ ] **Step 4: Run to verify pass, plus the regression suites**

Run: `pytest tests/unit/core/test_hosted_endpoints.py tests/unit/core/test_model_gateway.py tests/unit/modules/settings -q`
Expected: new tests PASS. Existing tests still pass, because the draft routes still read `body.base_url` until Task 3 and a `None` there only fails the same 422 path as before.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/core/test_hosted_endpoints.py
git add -p core/model_gateway/schemas.py core/model_gateway/client.py modules/settings/public.py
git commit -m "feat(model-gateway): add fixed hosted endpoints and output model types"
```

---

### Task 2: Enforce fixed endpoints on save and migrate legacy hosted rows (read time and save time)

**Files:**
- Modify: `modules/settings/public.py`: `_resolve_provider` (~296), `save_ai_settings` provider loop (~620–635), new helper `_provider_endpoint`
- Modify: `tests/unit/modules/settings/test_providers.py`. The fixture `_provider(..., kind="openai", url=GW)` must default to `kind="custom"`, because hosted kinds now ignore `url`. Change any stored-row fixtures that pair a hosted kind with `GW` to `custom` unless the test is about migration.
- Test: `tests/unit/modules/settings/test_hosted_providers.py` (create; reuse `_settings/_row/_config/_save/_update/_provider` by importing them from `test_providers`)

**Interfaces:**
- Consumes: `HOSTED_PROVIDER_ENDPOINTS`, `HOSTED_PROVIDER_KINDS`, `hosted_endpoint_equivalent`, `effective_provider_kind` (Task 1)
- Produces: `_provider_endpoint(item: ProviderUpdate, previous: dict[str, Any] | None) -> tuple[str, str | None]`, returning `(kind_to_store, raw_url)` before `_endpoint()` validation:

```
if item.kind not in HOSTED_PROVIDER_KINDS:     return item.kind, str(item.base_url) if item.base_url else None
prev_url = previous.get("base_url") if previous else None
if prev_url and hosted_endpoint_equivalent(item.kind, prev_url):  return item.kind, prev_url   # verbatim: keeps destination/key/consent
if prev_url and previous.get("kind") == item.kind:                return "custom", prev_url    # legacy non-equivalent row: same as read-time migration
return item.kind, HOSTED_PROVIDER_ENDPOINTS[item.kind]                                        # client base_url ignored
```

  In `save_ai_settings`, `endpoint = _endpoint(raw_url, settings)` runs on that result, and the stored record's `"kind"` is `kind_to_store`. `_resolve_provider` sets `kind = effective_provider_kind(record kind, endpoint)` after `_endpoint()`. Use the raw stored URL when `_endpoint` denies (policy-denied rows keep their kind).

- [ ] **Step 1: Write the failing tests** (use `_settings(public_ok=True)` so the fixed hosts pass policy)

```python
async def test_hosted_save_ignores_client_url_and_stores_fixed_endpoint():
    row = await _save(_row(), _update(providers=[_provider("default", kind="groq", url="https://evil.example/v1",
                       credential_action="replaced", api_key="k")]), _settings(public_ok=True))
    assert row.providers["default"]["base_url"] == "https://api.groq.com/openai/v1"
    assert row.providers["default"]["kind"] == "groq"

async def test_equivalent_hosted_url_is_kept_verbatim_on_save():
    stored = {"default": {"kind": "openai", "base_url": "https://api.openai.com", "key_ciphertext": _enc("sk"), "models": []}}
    row = await _save(_row(providers=stored), _update(providers=[_provider("default", kind="openai", url=None)]),
                      _settings(public_ok=True))
    assert row.providers["default"]["base_url"] == "https://api.openai.com"
    assert row.providers["default"]["key_ciphertext"] == stored["default"]["key_ciphertext"]

async def test_legacy_hosted_row_with_custom_url_reads_as_custom_and_keeps_key():
    stored = {"default": {"kind": "openai", "base_url": GW, "key_ciphertext": _enc("sk"),
                          "models": [{"id": "m", "type": "llm"}]}}
    config = await _config(_row(providers=stored))
    p = config.providers["default"]
    assert (p.kind, p.base_url, p.api_key, [m.id for m in p.models]) == ("custom", GW, "sk", ["m"])
    assert p.destination_id == public._identity(GW, "")[1]   # destination unchanged

async def test_save_of_legacy_row_with_same_hosted_kind_keeps_url_as_custom():
    stored = {"default": {"kind": "openai", "base_url": GW, "key_ciphertext": _enc("sk"), "models": []}}
    row = await _save(_row(providers=stored), _update(providers=[_provider("default", kind="openai", url=None)]))
    assert row.providers["default"]["kind"] == "custom" and row.providers["default"]["base_url"] == GW
    assert row.providers["default"]["key_ciphertext"] == stored["default"]["key_ciphertext"]

async def test_switching_hosted_kind_moves_to_new_fixed_endpoint_and_drops_key():
    stored = {"default": {"kind": "openai", "base_url": "https://api.openai.com/v1", "key_ciphertext": _enc("sk"), "models": []}}
    row = await _save(_row(providers=stored), _update(providers=[_provider("default", kind="anthropic", url=None)]),
                      _settings(public_ok=True))
    assert row.providers["default"]["base_url"] == "https://api.anthropic.com/v1"
    assert row.providers["default"]["key_ciphertext"] is None

async def test_hosted_fixed_endpoint_still_obeys_deployment_policy():
    with pytest.raises(HTTPException) as exc:
        await _save(_row(), _update(providers=[_provider("default", kind="openai", url=None)]), _settings(public_ok=False))
    assert exc.value.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/modules/settings/test_hosted_providers.py -v`
Expected: FAIL (client URL stored; legacy row reads as `openai`).

- [ ] **Step 3: Implement `_provider_endpoint` and wire it into `save_ai_settings` and `_resolve_provider`** as specified in Interfaces. Update the `test_providers.py` fixture default kind to `custom`. Note in the docstring that a converted row's `kind` change rotates `gateway_identity`/`model_identity` once, which means capability evidence is re-verified once. URL, key, models and consent are untouched.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/modules/settings -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/modules/settings/test_hosted_providers.py
git add -p modules/settings/public.py tests/unit/modules/settings/test_providers.py
git commit -m "feat(settings): fix hosted provider endpoints and migrate legacy rows to custom"
```

---

### Task 3: Draft discover/probe derive the endpoint from kind

**Files:**
- Modify: `modules/settings/public.py` (new `resolve_draft_endpoint`, next to `resolve_draft_provider` ~314)
- Modify: `modules/settings/routes.py` `discover_models` (~156–167); `modules/model_gateway/routes.py` `probe_draft` (~128–133)
- Test: `tests/unit/modules/settings/test_hosted_drafts.py` (create)

**Interfaces:**
- Consumes: Task 1 helpers; `validate_endpoint`
- Produces: `resolve_draft_endpoint(config: AIExecutionConfig, base_url: str | None, kind: ProviderKind | None, provider_id: str | None, settings: Settings) -> tuple[str, ProviderKind]`:
  - `kind` falls back to the stored provider's kind (`config.providers[provider_id or "default"]`), then to `"custom"`.
  - For a hosted kind: if the stored provider has the same kind and its `base_url` is `hosted_endpoint_equivalent`, use the stored `base_url` verbatim. This keeps `resolve_draft_provider`'s "same endpoint" key fallback working. Otherwise use `HOSTED_PROVIDER_ENDPOINTS[kind]`. A client `base_url` is ignored.
  - For a self-hosted kind, use `base_url`. Missing → `HTTPException(422, "Gateway endpoint is required")`.
  - The result always passes `validate_endpoint` (422 on policy denial).
  - Both routes replace their `validate_endpoint(str(body.base_url))` call with `endpoint, kind = public.resolve_draft_endpoint(...)` and pass `kind` on to `resolve_draft_provider`. Task 4 also passes it to `ModelGateway`.

- [ ] **Step 1: Write the failing tests** (build the config with `_derived_config()` from `test_providers` plus a hosted provider; `settings=_settings(public_ok=True)`)

```python
def test_hosted_draft_ignores_client_url():
    endpoint, kind = public.resolve_draft_endpoint(config, "https://evil.example/v1", "groq", None, settings)
    assert (endpoint, kind) == ("https://api.groq.com/openai/v1", "groq")

def test_draft_for_saved_hosted_provider_reuses_stored_verbatim_url_and_key():
    # provider "oa": kind openai, base_url "https://api.openai.com", api_key "sk-saved"
    endpoint, kind = public.resolve_draft_endpoint(config, None, "openai", "oa", settings)
    assert endpoint == "https://api.openai.com"
    assert public.resolve_draft_provider(config, endpoint, "", "oa", kind)[0] == "sk-saved"

def test_stored_key_never_follows_a_kind_switch():
    endpoint, kind = public.resolve_draft_endpoint(config, None, "anthropic", "oa", settings)
    assert endpoint == "https://api.anthropic.com/v1"
    assert public.resolve_draft_provider(config, endpoint, "", "oa", kind)[0] == ""

def test_self_hosted_draft_requires_url():
    with pytest.raises(HTTPException) as exc:
        public.resolve_draft_endpoint(config, None, "ollama", None, settings)
    assert exc.value.status_code == 422
```

- [ ] **Step 2: Run to verify failure.** Run: `pytest tests/unit/modules/settings/test_hosted_drafts.py -v`. Expected: FAIL `AttributeError: resolve_draft_endpoint`.
- [ ] **Step 3: Implement `resolve_draft_endpoint` and use it in both routes.**
- [ ] **Step 4: Run to verify pass.** Run: `pytest tests/unit/modules/settings tests/unit/modules/model_gateway -q` (if the second directory does not exist, run the tests that import `modules.model_gateway.routes`). Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add tests/unit/modules/settings/test_hosted_drafts.py
git add -p modules/settings/public.py modules/settings/routes.py modules/model_gateway/routes.py
git commit -m "feat(settings): derive draft endpoints from hosted provider kind"
```

---

### Task 4: Thread provider kind into every `ModelGateway`

**Files:**
- Modify: `core/model_gateway/schemas.py` `AIExecutionConfig` (~194): add `gateway_kind: ProviderKind = "custom"`
- Modify: `core/model_gateway/client.py` `ModelGateway` (~296): add `kind: ProviderKind = field(default="custom", kw_only=True)`
- Modify: `modules/settings/public.py`: `get_ai_execution_config` (set `gateway_kind=default.kind`), `derive_for_mapping` (add `"gateway_kind": provider.kind` to the update), `viz_decision_discovery_config` (`"openrouter"`), `viz_router_gateway_config` keyed branch (`"openrouter"` when `resolved.kind == "openrouter"`, else `"custom"`)
- Modify (add `kind=config.gateway_kind`, or `kind=kind` for the draft routes): `modules/agents/harness.py:994`, `modules/chat/retrieval.py:188`, `modules/chat/worker.py:648,1185,2090`, `modules/dashboard/briefs.py:746`, `modules/knowledge/entities/worker.py:650`, `modules/knowledge/temporal/worker.py:943,1090`, `modules/model_gateway/routes.py:166 (draft kind),290`, `modules/search/indexing.py:183`, `modules/settings/routes.py:181 (draft kind),405,480`, `modules/timeline/worker.py:298`, `modules/translations/worker.py:314`. Re-grep `ModelGateway(` first; line numbers drift.
- Test: extend `tests/unit/modules/settings/test_providers.py`

**Interfaces:**
- Consumes: Task 3 `resolve_draft_endpoint` (kind for draft routes)
- Produces: `ModelGateway.kind`, `AIExecutionConfig.gateway_kind`. Task 6 dispatches on `self.kind == "anthropic"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_derive_carries_provider_kind():
    config = _derived_config()
    assert public.derive_for_role(config, "chat").gateway_kind == "ollama"     # chat → local1 (ollama)
    assert public.derive_for_role(config, "brief").gateway_kind == "custom"

def test_every_model_gateway_construction_passes_kind():
    root = Path(__file__).resolve().parents[4] / "modules"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(?<![A-Za-z_])ModelGateway\(", text):
            call = text[match.end(): match.end() + 1200]
            depth, end = 1, 0
            for end, ch in enumerate(call):
                depth += {"(": 1, ")": -1}.get(ch, 0)
                if depth == 0:
                    break
            if "kind=" not in call[:end]:
                offenders.append(f"{path.relative_to(root)}:{text[:match.start()].count(chr(10)) + 1}")
    assert not offenders, f"ModelGateway built without kind=: {offenders}"
```

- [ ] **Step 2: Run to verify failure.** Run: `pytest tests/unit/modules/settings/test_providers.py -k "kind" -v`. Expected: FAIL (`gateway_kind` missing; 16 offenders).
- [ ] **Step 3: Add the two fields and thread `kind=` through every listed constructor.** Run `impact` on each enclosing function first; `ModelGateway` is HIGH.
- [ ] **Step 4: Run to verify pass, then the full unit suite** (call sites span many modules). Run: `pytest tests/unit -q -x`. Expected: PASS. Constructors in existing tests keep working through the `"custom"` default.
- [ ] **Step 5: Commit**

```bash
git add -p core/model_gateway/schemas.py core/model_gateway/client.py modules/settings/public.py modules/settings/routes.py modules/model_gateway/routes.py modules/agents/harness.py modules/chat/retrieval.py modules/chat/worker.py modules/dashboard/briefs.py modules/knowledge/entities/worker.py modules/knowledge/temporal/worker.py modules/search/indexing.py modules/timeline/worker.py modules/translations/worker.py tests/unit/modules/settings/test_providers.py
git commit -m "feat(model-gateway): thread provider kind into every gateway client"
```

---

### Task 5: Anthropic Messages translation (pure functions)

**Files:**
- Create: `core/model_gateway/anthropic.py` (translation part only)
- Test: `tests/unit/core/test_anthropic_translation.py`

**Interfaces:**
- Produces (all pure, no I/O):
  - `ANTHROPIC_VERSION = "2026-10-21"`, `DEFAULT_MAX_TOKENS = 4096`
  - `to_messages_request(body: dict[str, Any], *, stream: bool) -> dict[str, Any]`
  - `from_messages_response(data: dict[str, Any]) -> dict[str, Any]` returns an OpenAI `chat.completion` dict
  - `class StreamTranslator` with `feed(event: dict[str, Any]) -> list[dict[str, Any]]` returning OpenAI `chat.completion.chunk` dicts. It raises `ModelGatewayError` on an `error` event. Import `ModelGatewayError` lazily inside the method, or move the exception classes to `core/model_gateway/errors.py`, to avoid a circular import with `client.py`.

**Request translation table (`to_messages_request`)**

| OpenAI-shaped input | Messages API output |
|---|---|
| `model` | `model` |
| `messages[role=system]` (string, or list of `{type:text}`) | joined with `"\n\n"` into top-level `system` (string); omitted when empty |
| `messages[role=user]` string | `{"role":"user","content": str}` |
| user part `{type:"text",text}` | `{"type":"text","text"}` |
| user part `{type:"image_url",image_url:{url:"data:<mime>;base64,<b64>"}}` | `{"type":"image","source":{"type":"base64","media_type":mime,"data":b64}}`; `http(s)` URL → `{"type":"image","source":{"type":"url","url":url}}` |
| `messages[role=assistant]` | content blocks in order: each item of `reasoning_details` whose `type in {"thinking","redacted_thinking"}` (verbatim), then `{"type":"text","text":content}` if content non-empty, then per `tool_calls[i]` → `{"type":"tool_use","id":id,"name":function.name,"input":json.loads(arguments or "{}")}` (invalid JSON → `{}`) |
| `messages[role=tool]` | `{"role":"user","content":[{"type":"tool_result","tool_use_id":tool_call_id,"content":str(content)}]}`. Consecutive same-role turns are merged by the API, so no client-side merge is needed. |
| `tools[{type:function,function:{name,description,parameters}}]` | `tools[{"name","description"?,"input_schema": parameters or {"type":"object","properties":{}}}]` |
| `tool_choice` `None`/`"auto"` | omitted |
| `tool_choice` `"none"` | `{"type":"none"}` |
| `tool_choice` `"required"` or `{type:function,...}` | **omitted (auto)**. Forced choice returns 400 on Claude Opus/Sonnet/Haiku 5.5. Add a `ponytail:` comment naming the ceiling: older models could honour forcing, add a per-model allowlist if a caller needs it. |
| `max_tokens` / `max_completion_tokens` | `max_tokens` (required; default `DEFAULT_MAX_TOKENS`) |
| `temperature`, `top_p`, `top_k`, `extra_body`, `stream_options` | dropped (sampling params return 400 on API version 2026-10-21) |
| `response_format {type:json_schema, json_schema:{schema,...}}` | `output_config.format = {"type":"json_schema","schema": json_schema["schema"]}` |
| `response_format {type:json_object}` | dropped (the prompted-JSON fallback covers it) |
| `reasoning_effort` ∈ low/medium/high | `output_config.effort = value` and `thinking = {"type":"adaptive","display":"summarized"}` (summarized so streamed reasoning stays visible) |
| `stream=True` arg | `"stream": true` |

**Response translation (`from_messages_response`)**: `content` blocks are read by `type`. Text blocks are joined into `message.content` (`None` if empty). `tool_use` → `message.tool_calls[{id,type:"function",function:{name,arguments:json.dumps(input)}}]`. `thinking` text is joined into `message.reasoning_content`. Raw `thinking`/`redacted_thinking` blocks go into `message.reasoning_details` (list, verbatim). `stop_reason` maps: `end_turn|stop_sequence|pause_turn → "stop"`, `max_tokens|model_context_window_exceeded → "length"`, `tool_use → "tool_calls"`, `refusal → "content_filter"`, anything else `"stop"`. `usage` maps: `prompt_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens` (missing = 0), `completion_tokens = output_tokens`, `total_tokens = sum`. Envelope: `{"id","object":"chat.completion","model","choices":[{"index":0,"message":{"role":"assistant",...},"finish_reason"}],"usage"}`.

**Stream translation (`StreamTranslator.feed`)**:

| Event | Emitted OpenAI chunk(s) (`{"id","object":"chat.completion.chunk","model","choices":[{"index":0,"delta":…}]}`) |
|---|---|
| `message_start` | store id, model, `usage.input_tokens` (+cache fields); emit `delta:{"role":"assistant"}` |
| `content_block_start` text | none |
| `content_block_start` tool_use | `delta.tool_calls=[{"index":n,"id","type":"function","function":{"name","arguments":""}}]`, with `n` = ordinal of this tool_use block (0-based) |
| `content_block_start` thinking / redacted_thinking | start buffering `{type, thinking:"", signature:""}` / store redacted block verbatim |
| `content_block_delta` `text_delta` | `delta.content=text` |
| `content_block_delta` `input_json_delta` | `delta.tool_calls=[{"index":n,"function":{"arguments":partial_json}}]` |
| `content_block_delta` `thinking_delta` | `delta.reasoning_content=thinking`; append to buffer |
| `content_block_delta` `signature_delta` | append to buffer signature; no chunk |
| `content_block_stop` | finalize a thinking buffer into `details`; no chunk |
| `message_delta` | store `usage.output_tokens`; emit `delta:{"reasoning_details": details}` (only if non-empty) plus `finish_reason` mapped as above |
| `message_stop` | emit `{"choices":[],"usage":{prompt_tokens,completion_tokens,total_tokens}}` |
| `ping`, unknown types | none |
| `error` | raise `ModelGatewayError(f"Model gateway error ({error.type})")` |

- [ ] **Step 1: Write the failing tests**

```python
def test_system_tools_and_tool_results_translate():
    req = to_messages_request({"model": "claude-sonnet-5-5", "messages": [
        {"role": "system", "content": "S1"}, {"role": "system", "content": "S2"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "reasoning_details": [{"type": "thinking", "thinking": "", "signature": "sig"}],
         "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "f", "arguments": "{\"a\":1}"}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "42"}],
        "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object", "properties": {"a": {"type": "integer"}}}}}],
        "temperature": 0.2}, stream=False)
    assert req["system"] == "S1\n\nS2" and req["max_tokens"] == 4096 and "temperature" not in req
    assert req["messages"][1]["content"] == [{"type": "thinking", "thinking": "", "signature": "sig"},
                                             {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}}]
    assert req["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"}]}
    assert req["tools"] == [{"name": "f", "description": "d", "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}]

def test_forced_tool_choice_degrades_to_auto():
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    assert "tool_choice" not in to_messages_request({**base, "tool_choice": {"type": "function", "function": {"name": "f"}}}, stream=False)
    assert "tool_choice" not in to_messages_request({**base, "tool_choice": "required"}, stream=False)
    assert to_messages_request({**base, "tool_choice": "none"}, stream=False)["tool_choice"] == {"type": "none"}

def test_structured_output_and_effort_map_to_output_config():
    req = to_messages_request({"model": "m", "messages": [{"role": "user", "content": "x"}], "reasoning_effort": "high",
        "response_format": {"type": "json_schema", "json_schema": {"name": "q", "strict": True, "schema": {"type": "object"}}}}, stream=True)
    assert req["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "high"}
    assert req["thinking"] == {"type": "adaptive", "display": "summarized"} and req["stream"] is True

def test_response_maps_text_tools_thinking_stop_and_usage():
    out = from_messages_response({"id": "msg_1", "model": "m", "stop_reason": "tool_use",
        "content": [{"type": "thinking", "thinking": "hm", "signature": "s"}, {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}}],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 5}})
    msg = out["choices"][0]["message"]
    assert msg["content"] == "ok" and msg["reasoning_content"] == "hm"
    assert msg["tool_calls"][0]["function"] == {"name": "f", "arguments": "{\"a\": 1}"}
    assert msg["reasoning_details"] == [{"type": "thinking", "thinking": "hm", "signature": "s"}]
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}

def test_stream_translates_text_tool_deltas_thinking_and_usage():
    tr, chunks = StreamTranslator(), []
    for ev in [{"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 3}}},
               {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "t"}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sg"}},
               {"type": "content_block_stop", "index": 0},
               {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}},
               {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_9", "name": "f", "input": {}}},
               {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"a\""}},
               {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
               {"type": "message_stop"}]:
        chunks += tr.feed(ev)
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert {"reasoning_content": "t"} in deltas and {"content": "Hi"} in deltas
    assert {"tool_calls": [{"index": 0, "id": "toolu_9", "type": "function", "function": {"name": "f", "arguments": ""}}]} in deltas
    assert {"tool_calls": [{"index": 0, "function": {"arguments": "{\"a\""}}]} in deltas
    assert any(d.get("reasoning_details") == [{"type": "thinking", "thinking": "t", "signature": "sg"}] for d in deltas)
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}

def test_stream_error_event_raises():
    with pytest.raises(ModelGatewayError, match="overloaded_error"):
        StreamTranslator().feed({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
```

- [ ] **Step 2: Run to verify failure.** Run: `pytest tests/unit/core/test_anthropic_translation.py -v`. Expected: FAIL `ModuleNotFoundError: core.model_gateway.anthropic`.
- [ ] **Step 3: Implement the three translators** exactly per the tables.
- [ ] **Step 4: Run to verify pass.** Same command. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add core/model_gateway/anthropic.py tests/unit/core/test_anthropic_translation.py
git commit -m "feat(model-gateway): add Anthropic Messages request/response translation"
```

---

### Task 6: Anthropic native transport wired into the gateway (chat, tools, structured, stream, discovery)

**Files:**
- Modify: `core/model_gateway/anthropic.py` (I/O part)
- Modify: `core/model_gateway/client.py`: `_request.send` (~560), `_stream_inner` (~834–905), `discover_models` (~645), `embed`/`rerank`/`decisions` guard
- Test: `tests/unit/core/test_anthropic_transport.py` (create). Patch `ModelGateway._http_client` to return `httpx.AsyncClient(transport=httpx.MockTransport(handler))`.

**Interfaces:**
- Consumes: Task 5 translators; `ModelGateway.kind` (Task 4); `_api_base`, `_retryable_status`, `_on_retryable_status`, `_raise_if_policy_denied`, `body_sent`
- Produces (in `anthropic.py`):
  - `headers(api_key: str) -> dict[str, str]` returns `{"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}`
  - `async def create_message(http: httpx.AsyncClient, base_url: str, api_key: str, body: dict[str, Any]) -> dict[str, Any]` does `POST {base_url}/messages` with `to_messages_request(body, stream=False)` and returns `from_messages_response(json)`
  - `async def open_message_stream(http: httpx.AsyncClient, base_url: str, api_key: str, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]`. It sends with `stream=True` and raises on a non-2xx status before returning, after reading the body. It then returns an async generator that parses SSE `data:` lines (JSON), feeds a `StreamTranslator`, yields chunks and closes the response in `finally`.
  - `async def list_models(http: httpx.AsyncClient, base_url: str, api_key: str) -> list[str]` does `GET {base_url}/models?limit=1000` and returns `[item["id"] for item in data if non-empty str]`
  - Error mapping, so `client.py`'s existing `except` clauses work unchanged:
    - non-2xx → `openai.APIStatusError(message=f"{error.type}: {error.message}", response=response, body=json)`, which keeps the `retry-after` header and `status_code`
    - `httpx.TimeoutException` → `openai.APITimeoutError(request=exc.request) from exc`
    - `httpx.TransportError` or `EndpointNetworkPolicyError` → `openai.APIConnectionError(message="connection failed", request=req) from exc`. `_raise_if_policy_denied` reads `__cause__`.
- Gateway wiring:
  - `_request`: for `self.kind == "anthropic"` and `path != "chat/completions"`, raise `CapabilityUnsupported("Anthropic does not offer <capability>")` before any I/O. This covers embeddings, rerank and decisions, so probes report `unsupported`. Inside `send()`, the anthropic branch uses `async with self._http_client(base_url) as http:` and `await anthropic.create_message(http, base_url, self.api_key, body)` in place of the `AsyncOpenAI` block. Keep before_send, policy recheck, `body_sent` hook, after_send, retry loop and status mapping exactly as they are.
  - `_stream_inner`: the anthropic branch does `stream = await anthropic.open_message_stream(http, base_url, self.api_key, {"model": mapping.model, "messages": messages, **extra})` inside the same `asyncio.timeout` and hook block. The loop body uses `payload = chunk if isinstance(chunk, dict) else chunk.model_dump(mode="json", exclude_none=True)`. Widen the reasoning-retry match to `any(w in msg for w in ("reasoning", "thinking", "effort"))`, because Anthropic's 400 for unsupported adaptive thinking names `thinking`.
  - `discover_models`: anthropic branch calls `anthropic.list_models` with the same static-reason error mapping.

- [ ] **Step 1: Write the failing tests** (each records the outgoing `httpx.Request`)

```python
async def test_chat_posts_native_messages_with_anthropic_headers():
    # handler returns 200 {"id":"msg_1","model":"claude-sonnet-5-5","stop_reason":"end_turn","content":[{"type":"text","text":"ready"}],"usage":{"input_tokens":1,"output_tokens":1}}
    out = await gw(kind="anthropic", base_url="https://api.anthropic.com/v1").chat("chat", MAPPING, POLICY, [{"role": "user", "content": "x"}], before_send=AsyncMock())
    req = seen[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-test" and req.headers["anthropic-version"] == "2026-10-21"
    assert "authorization" not in req.headers
    assert out["choices"][0]["message"]["content"] == "ready"

async def test_429_with_retry_after_is_retried_once_then_succeeds():  # first 429 {"type":"error","error":{"type":"rate_limit_error"}} + retry-after: 0, then 200
    ...; assert len(seen) == 2

async def test_auth_error_maps_to_gateway_error_without_retry():  # 401 authentication_error
    with pytest.raises(ModelGatewayError, match="401|authentication"): ...
    assert len(seen) == 1

async def test_400_maps_to_capability_unsupported_and_structured_falls_back_to_prompted_json():
    # first request (has output_config.format) → 400; second (no output_config.format) → 200 text "{\"query\":\"q\"}"
    out = await gateway.structured(...); assert json.loads(out["choices"][0]["message"]["content"]) == {"query": "q"}

async def test_stream_yields_openai_shaped_sse_lines_and_done():
    # handler streams the SSE example (message_start … message_stop) as text/event-stream
    lines = [l async for l in gateway.stream("chat", MAPPING, POLICY, [...], before_send=AsyncMock())]
    assert lines[-1] == "data: [DONE]" and any('"content": "Hello"' in l for l in lines)

async def test_embeddings_and_rerank_are_unsupported_without_io():
    with pytest.raises(CapabilityUnsupported): await gateway.embed("embedding", MAPPING, POLICY, ["x"], probe=True)
    assert seen == []

async def test_discover_lists_anthropic_models_with_native_headers():
    # GET /v1/models?limit=1000 → {"data":[{"id":"claude-opus-5-5","type":"model"},{"id":"claude-haiku-5-5"}],"has_more":false}
    assert await gateway.discover_models() == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert seen[0].headers["x-api-key"] == "sk-test" and seen[0].url.params["limit"] == "1000"

async def test_existing_openai_kind_still_uses_openai_sdk_path():  # kind="openai" → request to /chat/completions with Bearer
```

Also run unchanged: `tests/unit/core/test_model_gateway.py`, `test_gateway_stream_tools.py`, `test_gateway_reasoning.py`, `test_gateway_before_send.py`, `test_model_gateway_structured_fallback.py`.

- [ ] **Step 2: Run to verify failure.** Run: `pytest tests/unit/core/test_anthropic_transport.py -v`. Expected: FAIL (requests go to `/chat/completions` with Bearer).
- [ ] **Step 3: Implement the I/O functions and the gateway branches** per Interfaces. Run `impact` on `_request`, `_stream_inner` and `discover_models` first (all HIGH via `ModelGateway`).
- [ ] **Step 4: Run to verify pass, plus all gateway suites.** Run: `pytest tests/unit/core -q`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add tests/unit/core/test_anthropic_transport.py
git add -p core/model_gateway/anthropic.py core/model_gateway/client.py
git commit -m "feat(model-gateway): route anthropic kind through the native Messages API"
```

---

### Task 7: Keep Claude thinking blocks across tool rounds

Anthropic requires the `thinking` blocks of an assistant tool-use turn to be passed back unmodified with the tool results ("Required: within a tool-use turn, pass thinking blocks back", platform.claude.com thinking docs). Opus 5.5 thinking is always on. Two callers rebuild the assistant message and would drop `reasoning_details`.

**Files:**
- Modify: `modules/agents/harness.py` (~1085: `assistant = {"role": "assistant", "tool_calls": [...]}`)
- Modify: `modules/chat/worker.py` (stream loop ~1545 where `delta.get("tool_calls")` is fed; round message ~1654)
- Test: `tests/unit/modules/agents/test_harness_reasoning_details.py`, `tests/unit/modules/chat/test_stream_reasoning_details.py` (create; reuse existing harness/worker test fixtures)

**Interfaces:**
- Consumes: `message["reasoning_details"]` (non-stream, Task 5) and stream chunk `delta.reasoning_details` (Task 5)
- Produces: the assistant tool-turn dicts include `"reasoning_details": <list>` only when the provider sent a non-empty list. When it is absent, the dict shape stays byte-identical to today, so other kinds are unaffected. The harness state is JSON-persisted, and signatures are strings, so persistence is safe. Cap the list at 64 items and drop non-dict items.

- [ ] **Step 1: Write the failing tests**

```python
async def test_harness_keeps_reasoning_details_on_assistant_tool_turn():
    # call_model with a gateway.tools stub returning message {"tool_calls":[...], "reasoning_details":[{"type":"thinking","thinking":"","signature":"s"}]}
    assert state["messages"][-1]["reasoning_details"] == [{"type": "thinking", "thinking": "", "signature": "s"}]

async def test_harness_assistant_turn_unchanged_without_reasoning_details():
    assert "reasoning_details" not in state["messages"][-1]

async def test_chat_worker_round_message_carries_stream_reasoning_details():
    # stream stub yields delta {"reasoning_details":[{"type":"redacted_thinking","data":"x"}]} then tool_calls, finish_reason tool_calls
    assert messages_payload_round["reasoning_details"] == [{"type": "redacted_thinking", "data": "x"}]
```

- [ ] **Step 2: Run to verify failure.** Run: `pytest tests/unit/modules/agents/test_harness_reasoning_details.py tests/unit/modules/chat/test_stream_reasoning_details.py -v`. Expected: FAIL (key missing).
- [ ] **Step 3: Copy `reasoning_details` onto the rebuilt assistant dicts** (harness: from `message`; chat worker: keep the last non-empty `delta.reasoning_details` seen in the round). Run `impact` on `call_model` and `run_response_generation` first.
- [ ] **Step 4: Run to verify pass, plus existing harness/chat suites.** Run: `pytest tests/unit/modules/agents tests/unit/modules/chat -q`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add tests/unit/modules/agents/test_harness_reasoning_details.py tests/unit/modules/chat/test_stream_reasoning_details.py
git add -p modules/agents/harness.py modules/chat/worker.py
git commit -m "fix(chat): pass provider reasoning details back on tool rounds"
```

---

### Task 8: OpenRouter browse backend (allowlisted filters, bounded projection)

**Files:**
- Create: `modules/settings/openrouter_models.py`
- Modify: `modules/settings/schemas.py` (next to `VizDecisionModelsRequest` ~284)
- Modify: `core/model_gateway/client.py`: generalize `discover_decision_models` into `list_openrouter_models(params)` and keep `discover_decision_models` as a one-line wrapper
- Modify: `modules/settings/routes.py`: new route
- Test: `tests/unit/modules/settings/test_openrouter_browse.py` (create)

**Interfaces:**
- Produces:
  - `ModelGateway.list_openrouter_models(self, params: dict[str, str]) -> dict[str, Any]`. Same body as today's `discover_decision_models`, with `options={"params": params}`. `discover_decision_models()` returns `await self.list_openrouter_models({"output_modalities": "decisions"})`.
  - `OpenRouterModelFilters(BaseModel, extra="forbid")`. Every field is optional:
    - `output_modalities: Literal["all","text","image","embeddings","audio","video","rerank","decisions","speech","transcription"] = "all"`
    - `input_modalities: list[Literal["text","image","audio","file"]]` (max 4)
    - `category: Literal["programming","roleplay","marketing","marketing/seo","technology","science","translation","legal","finance","health","trivia","academia"] | None`
    - `supported_parameters: list[str pattern ^[a-z_]{1,40}$]` (max 20)
    - `sort: Literal["most-popular","newest","top-weekly","pricing-low-to-high","pricing-high-to-low","context-high-to-low","throughput-high-to-low","latency-low-to-high","intelligence-high-to-low","coding-high-to-low","agentic-high-to-low","design-arena-elo-high-to-low"] = "newest"`
    - `q: str` (max 100)
    - `endpoints: list[Literal["/api/v1/chat/completions","/api/v1/completions","/api/v1/messages","/api/v1/responses","/api/v1/embeddings","/api/v1/rerank","/api/alpha/decisions","/api/alpha/extractions","/api/v1/videos","/api/v1/audio/speech","/api/v1/audio/transcriptions","/api/v1/images","/api/v1/batches"]]` (max 13)
    - `context: int ≥1 ≤10_000_000`
    - `min_price, max_price, min_output_price, max_output_price: float ≥0 ≤10_000`
    - `min_age_days, max_age_days: int ≥0 ≤36_500`
    - `arch: str pattern ^[A-Za-z0-9 ._-]{1,40}$`
    - `model_authors: list[str pattern ^[a-z0-9][a-z0-9._-]{0,63}$]` (max 20)
    - `providers: list[str pattern ^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$]` (max 20)
    - `distillable: bool | None`, `zdr: bool = False`, `region: Literal["eu","us"] | None`
    - `min_/max_intelligence_index, min_/max_coding_index, min_/max_agentic_index: float ≥0 ≤1000`
    - `min_/max_tool_success_rate: float ≥0 ≤1`
    - a `model_validator` rejecting any `min_* > max_*` pair
  - `to_query_params(filters: OpenRouterModelFilters, offset: int, limit: int) -> dict[str, str]`: lists are comma-joined, `None`/empty/`False` are omitted (`zdr=True → "true"`, `distillable → "true"/"false"`), numbers use `str()`, and `offset`/`limit` are always present
  - `parse_openrouter_models(payload: Any, limit: int) -> tuple[list[dict[str, Any]], int, bool]`, returning `(models, total_count, has_next)`. Each model is `{id, name, created, context_length, input_modalities, output_modalities, pricing_prompt, pricing_completion, supported_parameters}`:
    - `id` matches `^[A-Za-z0-9@][A-Za-z0-9._:/@+-]{0,199}$`, unique
    - `name` is a str ≤200 (else `id`)
    - `created` is an int ≥0 or None; `context_length` is an int ≥0 or None
    - modalities are filtered to the known sets
    - prices are a str ≤32 that parses as a non-negative float, else None
    - `supported_parameters` match `^[a-z_]{1,40}$` (max 64)
    - at most `limit` models; `total_count` is an int clamped to `[0, 1_000_000]`, else `len(models)`; `has_next = bool(payload["links"]["next"])`
    - malformed entries are skipped, never repaired
  - `OpenRouterBrowseRequest{provider_id: ProviderId | None = None, api_key: str = Field("", max_length=4096, repr=False), filters: OpenRouterModelFilters = OpenRouterModelFilters(), offset: int = Field(0, ge=0, le=100_000), limit: int = Field(50, ge=1, le=200)}` with `extra="forbid"`
  - `OpenRouterModelRead` (the projection fields) and `OpenRouterBrowseResult{ok: bool, error: str | None = None, models: list[OpenRouterModelRead] = [], total_count: int = 0, next_offset: int | None = None}`
  - Route `POST /api/v1/settings/ai/openrouter/models` with `dependencies=[Depends(require_budget("model_probe", 10, 60))]`, `OwnerWrite`. Key = `body.api_key`, or else the stored key of `provider_id` **only if** that provider's `kind == "openrouter"` and `hosted_endpoint_equivalent("openrouter", provider.base_url)`; else `ok=False, error="credential_required"`. Config = `viz_decision_discovery_config(base, key, settings)`, i.e. the fixed endpoint and the policy check (`gateway_base_url is None → "network_denied"`). Same `recheck_send` / rollback / gateway construction as `discover_viz_decision_models` (`kind="openrouter"`). A `ModelGatewayError` maps to `viz.error_from_gateway(exc).code`. `next_offset = offset + len(models) if has_next else None`.
  - Caching: none. YAGNI; add a short TTL only if owners hit the budget.

- [ ] **Step 1: Write the failing tests**

```python
def test_filters_reject_unknown_keys_and_inverted_ranges():
    with pytest.raises(ValidationError): OpenRouterModelFilters.model_validate({"foo": 1})
    with pytest.raises(ValidationError): OpenRouterModelFilters(min_price=5, max_price=1)
    with pytest.raises(ValidationError): OpenRouterModelFilters(sort="cheapest")
    with pytest.raises(ValidationError): OpenRouterModelFilters(model_authors=["Bad Author!"])

def test_query_params_cover_every_filter():
    f = OpenRouterModelFilters(output_modalities="image", input_modalities=["text", "image"], category="programming",
        supported_parameters=["tools"], sort="pricing-low-to-high", q="flux", endpoints=["/api/v1/images"], context=8000,
        min_price=0, max_price=2.5, min_output_price=0, max_output_price=10, min_age_days=0, max_age_days=30, arch="Claude",
        model_authors=["openai", "google"], providers=["Together"], distillable=True, zdr=True, region="eu",
        min_intelligence_index=10, max_intelligence_index=90, min_coding_index=1, max_coding_index=2,
        min_agentic_index=1, max_agentic_index=2, min_tool_success_rate=0.5, max_tool_success_rate=1)
    p = to_query_params(f, offset=50, limit=50)
    assert p["output_modalities"] == "image" and p["input_modalities"] == "text,image" and p["model_authors"] == "openai,google"
    assert p["zdr"] == "true" and p["distillable"] == "true" and p["region"] == "eu" and p["offset"] == "50" and p["limit"] == "50"
    assert set(p) == {*OpenRouterModelFilters.model_fields} | {"offset", "limit"}
    assert "zdr" not in to_query_params(OpenRouterModelFilters(), 0, 50)

def test_parse_openrouter_models_bounds_and_skips_malformed():
    payload = {"total_count": 3, "links": {"next": "https://openrouter.ai/api/v1/models?offset=2"}, "data": [
        {"id": "black-forest-labs/flux-2", "name": "FLUX.2", "created": 1760000000, "context_length": 4096,
         "architecture": {"input_modalities": ["text", "bogus"], "output_modalities": ["image"]},
         "pricing": {"prompt": "0.000001", "completion": "nan?"}, "supported_parameters": ["seed", "BAD-PARAM"]},
        {"id": "bad id with spaces"}, {"id": "black-forest-labs/flux-2"}, "junk"]}
    models, total, has_next = parse_openrouter_models(payload, limit=50)
    assert [m["id"] for m in models] == ["black-forest-labs/flux-2"] and total == 3 and has_next
    m = models[0]
    assert m["input_modalities"] == ["text"] and m["output_modalities"] == ["image"]
    assert m["pricing_prompt"] == "0.000001" and m["pricing_completion"] is None and m["supported_parameters"] == ["seed"]

async def test_browse_route_uses_saved_key_only_for_openrouter_provider():  # provider kind openai + blank key → ok=False, error="credential_required", gateway never built
async def test_browse_route_sends_filters_to_fixed_endpoint():  # patch ModelGateway.list_openrouter_models; assert called with to_query_params(...) and result next_offset == offset+len
```

- [ ] **Step 2: Run to verify failure.** Run: `pytest tests/unit/modules/settings/test_openrouter_browse.py -v`. Expected: FAIL `ModuleNotFoundError: modules.settings.openrouter_models`.
- [ ] **Step 3: Implement the module, schemas, gateway method and route** per Interfaces.
- [ ] **Step 4: Run to verify pass, plus viz suites** (`discover_decision_models` refactor). Run: `pytest tests/unit/modules/settings tests/unit/core -q`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add modules/settings/openrouter_models.py tests/unit/modules/settings/test_openrouter_browse.py
git add -p modules/settings/schemas.py modules/settings/routes.py core/model_gateway/client.py
git commit -m "feat(settings): add allowlisted OpenRouter model browse endpoint"
```

---

### Task 9: Provider editor — key-only hosted kinds

**Files:**
- Modify: `apps/web/src/modules/settings/provider-editor.tsx` (types ~11–19, presets ~50, kind select ~123–128, URL input ~133, Discover button ~140)
- Modify: `apps/web/src/core/messages/ai-settings.ts` (en-us ~52–56, vi-vi ~109–113)
- Test: `apps/web/src/modules/settings/provider-editor.test.tsx`

**Interfaces:**
- Produces (exported from `provider-editor.tsx`):
  - `hostedProviderKinds: readonly ProviderKind[] = ['openai','openrouter','groq','gemini','anthropic']` and `isHostedKind(kind: ProviderKind): boolean`
  - `modelTypes` extended with `'image','video','speech','audio','transcription'`; `openRouterOnlyModelTypes = ['image','video','speech','audio','transcription'] as const`
  - `modelTypeForModality(modality: string): ModelType`, mapping `text→llm, embeddings→embedding, rerank→reranker, decisions→decision, image→image, video→video, speech→speech, audio→audio, transcription→transcription`, and any other value → `llm`
  - `providerPresets[*].baseUrl` stays and becomes display-only for hosted kinds. Its values must equal the backend map; add a one-line comment saying so.
- Behaviour:
  - Hosted kinds render no URL `Input`. In its place, a muted read-only line shows `t('providerEndpoint')`: `provider.base_url ?? preset.baseUrl`. That shows the verbatim legacy URL when the server kept one.
  - The Docker hint is shown only for `ollama`/`lmstudio` (unchanged).
  - Switching kind: to a hosted kind sets `base_url = preset.baseUrl`, which drops the typed URL. To a self-hosted kind, it keeps today's rule (rewrite only if still equal to the old preset; otherwise keep), and hosted→self-hosted sets `preset.baseUrl || null`.
  - The Discover button's `!provider.base_url` gate applies only to self-hosted kinds.
  - New strings `providerEndpoint` (en-us `'Endpoint'`, vi-vi `'Điểm cuối'`), `providerEndpointFixed` (en-us `'Fixed by Umwelt-OS for this provider.'`, vi-vi `'Được Umwelt-OS cố định cho nhà cung cấp này.'`), and `type_image`/`type_video`/`type_speech`/`type_audio`/`type_transcription` (en-us `'Image'`, `'Video'`, `'Speech'`, `'Audio'`, `'Transcription'`; vi-vi `'Hình ảnh (tạo)'`, `'Video'`, `'Giọng nói'`, `'Âm thanh'`, `'Chép lời'`).

- [ ] **Step 1: Write the failing tests**

```tsx
it('hides the URL field for hosted kinds and shows the fixed endpoint', () => {
  render(<ProviderEditor {...props({ providers: [newProvider('anthropic', [])] })} />);
  expect(screen.queryByLabelText('Base URL')).toBeNull();
  expect(screen.getByText('https://api.anthropic.com/v1')).toBeInTheDocument();
});
it('keeps the URL field for self-hosted kinds', () => {
  render(<ProviderEditor {...props({ providers: [newProvider('ollama', [])] })} />);
  expect(screen.getByLabelText('Base URL')).toHaveValue('http://localhost:11434/v1');
});
it('drops a typed URL when switching to a hosted kind', async () => {
  // custom provider with base_url 'https://my.gw/v1' → select kind OpenAI → onChange called with base_url 'https://api.openai.com/v1'
});
it('enables Discover for a new hosted provider with a key and no URL edit', async () => { /* secret key typed → button enabled */ });
it('maps OpenRouter output modalities to model types', () => {
  expect(modelTypeForModality('embeddings')).toBe('embedding');
  expect(modelTypeForModality('rerank')).toBe('reranker');
  expect(modelTypeForModality('image')).toBe('image');
  expect(modelTypeForModality('weird')).toBe('llm');
});
```

- [ ] **Step 2: Run to verify failure.** Run: `npm --prefix apps/web exec vitest run src/modules/settings/provider-editor.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces and add the strings to both locales.
- [ ] **Step 4: Run to verify pass, plus typecheck/lint.** Run: `npm --prefix apps/web exec vitest run src/modules/settings` and `npm --prefix apps/web run typecheck` and `npm --prefix apps/web run lint`. Script names may differ; check `apps/web/package.json`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/web/src/modules/settings/provider-editor.test.tsx
git add -p apps/web/src/modules/settings/provider-editor.tsx apps/web/src/core/messages/ai-settings.ts
git commit -m "feat(web): key-only hosted provider cards with fixed endpoints"
```

---

### Task 10: OpenRouter model browser + Models section integration

**Files:**
- Create: `apps/web/src/modules/settings/openrouter-model-browser.tsx`, `openrouter-model-browser.test.tsx`
- Modify: `apps/web/src/modules/settings/models-section.tsx` (`ModelsPanel` add row ~55–70; props type ~43)
- Modify: `apps/web/src/modules/settings/ai-settings.tsx` (new browse function beside `discover` ~159; pass to `ModelsPanel` ~286)
- Modify: `apps/web/src/core/messages/ai-settings.ts`
- Test: `models-section.test.tsx` (extend)

**Interfaces:**
- Consumes: `POST /api/v1/settings/ai/openrouter/models` (Task 8); `modelTypeForModality`, `openRouterOnlyModelTypes`, `selectableModelTypes` (Task 9)
- Produces:
  - Types in `openrouter-model-browser.tsx`:
    - `OpenRouterFilters`: TS mirror of `OpenRouterModelFilters`. Field names are identical (snake_case, because they go on the wire); all optional except `output_modalities` and `sort`.
    - `OpenRouterModel = { id: string; name: string; created: number | null; context_length: number | null; input_modalities: string[]; output_modalities: string[]; pricing_prompt: string | null; pricing_completion: string | null; supported_parameters: string[] }`
    - `OpenRouterBrowseResult = { ok: boolean; error: string | null; models: OpenRouterModel[]; total_count: number; next_offset: number | null }`
    - `BrowseOpenRouter = (filters: OpenRouterFilters, offset: number, limit: number) => Promise<OpenRouterBrowseResult>`
  - Component `OpenRouterModelBrowser({ providerId, browse, excluded, disabled, onPick, t }: { providerId: string; browse: BrowseOpenRouter; excluded: Set<string>; disabled: boolean; onPick: (model: OpenRouterModel, type: ModelType) => void; t: Translate })`:
    - `Tabs` row: `all,text,image,video,speech,decisions,embeddings,rerank,transcription,audio`, mapping to `output_modalities`.
    - Search `Input` (`q`), debounced 300 ms.
    - Sort `Select`: all 12 sort values, default `newest`.
    - "More filters" `Button` toggling a panel with:
      - input modality `Checkbox`es
      - category `Select`
      - supported parameters, model authors, providers and endpoints: comma-separated `Input`s, except endpoints, which are `Checkbox`es
      - context min, price min/max, output price min/max, age min/max, intelligence/coding/agentic index min/max and tool success rate min/max: number `Input`s
      - arch `Input`, region `Select` (any/eu/us), distillable `Select` (any/yes/no), ZDR `Checkbox`
      - a "Reset filters" button
    - Data: `useQuery({ queryKey: ['openrouter-models', providerId, filters, offset], queryFn: () => browse(filters, offset, 50), placeholderData: keepPreviousData })`.
    - Results: a list or `Table` showing name/id, context, `in → out` modalities, and prompt/completion price per 1M (`Number(p) * 1e6`, formatted with `Intl.NumberFormat(locale, {style:'currency',currency:'USD'})`). Items in `excluded` are disabled. Clicking calls `onPick(model, activeTab === 'all' ? typeForModel(model) : modelTypeForModality(activeTab))`.
    - `typeForModel` picks the first of `['embeddings','rerank','decisions','image','video','speech','transcription','audio','text']` present in `output_modalities`.
    - Prev/Next paging via `offset` and `next_offset`.
    - `ok:false` shows `t('openRouterBrowseError_' + error)` with a generic fallback.
    - "Pinned" and the List/Table view switch are omitted (spec: site-only UI).
  - `ModelsPanel` gains optional prop `browseOpenRouter?: (provider: ProviderRead) => BrowseOpenRouter`. When `provider.kind === 'openrouter' && browseOpenRouter`, the browser renders in place of `SearchableSelect`. `onPick` sets `pending` and `type`. The Type `Select` lists `selectableModelTypes` plus `openRouterOnlyModelTypes` only when the selected provider is OpenRouter. The Add button is unchanged (it already stores `{id, type}`).
  - `ModelDefaults` is unchanged; its filters already exclude new types. Pin that with a test.
  - `ai-settings.tsx`: `browseOpenRouter = (provider) => (filters, offset, limit) => apiRequest<OpenRouterBrowseResult>('/api/v1/settings/ai/openrouter/models', { method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ provider_id: savedIds.includes(provider.id) ? provider.id : undefined, api_key: secrets[provider.id]?.key ?? '', filters, offset, limit }) })`.
  - Strings (both locales) for tabs, sort options, filter labels, results columns, paging, empty/error states, and `openRouterBrowseError_credential_required` / `_network_denied` / `_timeout` / `_auth`. Mirror the existing viz error codes from `viz.error_from_gateway`; check its code set.

- [ ] **Step 1: Write the failing tests**

```tsx
it('requests the active tab, search and sort', async () => {
  const browse = vi.fn().mockResolvedValue({ ok: true, error: null, models: [], total_count: 0, next_offset: null });
  renderWithQuery(<OpenRouterModelBrowser providerId="or" browse={browse} excluded={new Set()} disabled={false} onPick={vi.fn()} t={t} />);
  await waitFor(() => expect(browse).toHaveBeenLastCalledWith(expect.objectContaining({ output_modalities: 'all', sort: 'newest' }), 0, 50));
  await user.click(screen.getByRole('tab', { name: 'Image' }));
  await user.type(screen.getByRole('searchbox'), 'flux');
  await waitFor(() => expect(browse).toHaveBeenLastCalledWith(expect.objectContaining({ output_modalities: 'image', q: 'flux' }), 0, 50));
});
it('sends advanced filters', async () => { /* open More filters, set ZDR + region eu + max price 2 → last call has zdr:true, region:'eu', max_price:2 */ });
it('pages with next_offset', async () => { /* result next_offset 50 → click Next → browse called with offset 50 */ });
it('picks a model with the type from the active tab', async () => { /* Image tab, click "FLUX.2" → onPick(model, 'image') */ });
// models-section.test.tsx
it('uses the OpenRouter browser for openrouter providers and adds an image model', async () => { /* pick → Add → onChange models [{id:'black-forest-labs/flux-2', type:'image'}] */ });
it('offers output-only types only for OpenRouter providers', () => { /* openai provider → no 'Image' option in Type select */ });
it('never lists output-only types in role defaults', () => { /* provider with {type:'image'} model → not in any ModelDefaults select */ });
```

- [ ] **Step 2: Run to verify failure.** Run: `npm --prefix apps/web exec vitest run src/modules/settings/openrouter-model-browser.test.tsx src/modules/settings/models-section.test.tsx`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement the browser, `ModelsPanel` integration, `ai-settings.tsx` wiring and strings** per Interfaces (umwelt-os-ui-system skill: shadcn primitives, semantic tokens, accessible labels on every control).
- [ ] **Step 4: Run to verify pass, plus typecheck/lint.** Run: `npm --prefix apps/web exec vitest run src/modules/settings`, the typecheck script and the lint script. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add apps/web/src/modules/settings/openrouter-model-browser.tsx apps/web/src/modules/settings/openrouter-model-browser.test.tsx
git add -p apps/web/src/modules/settings/models-section.tsx apps/web/src/modules/settings/models-section.test.tsx apps/web/src/modules/settings/ai-settings.tsx apps/web/src/core/messages/ai-settings.ts
git commit -m "feat(web): OpenRouter model browser with full filter set"
```

---

### Task 11: Documentation and status

**Files:**
- Modify: `docs/IMPLEMENTATION_STATUS.md` (add an entry: fixed hosted endpoints, legacy-row migration rule, Anthropic native transport with API version `2026-10-21`, OpenRouter browser, new stored-only model types)
- Modify: the settings/AI operator doc if one exists (`grep -ril "base url\|openrouter" docs/ --include=*.md`). Note that hosted kinds now need `ai_allow_public_providers` or their host in `ai_allowed_endpoint_hosts`. That is unchanged policy, now fixed hosts.

- [ ] **Step 1:** Update the docs (no code).
- [ ] **Step 2:** Run the whole check: `pytest tests/unit -q`, `npm --prefix apps/web exec vitest run`, the web typecheck and lint, `ruff check core modules` and `mypy core modules`. Expected: PASS. Report the real results, including any pre-existing failures.
- [ ] **Step 3:** `detect_changes({scope: "compare", base_ref: "main"})`. Review the affected processes.
- [ ] **Step 4: Commit**

```bash
git add -p docs/IMPLEMENTATION_STATUS.md
git commit -m "docs: record fixed provider endpoints and OpenRouter browser"
```

---

## Self-Review

1. **Spec coverage:**
   - Goal 1 (hosted key-only, server fixes URL): Tasks 1–3 and 9.
   - Goal 2 (self-hosted keep URL, Docker hint): Task 9.
   - Goal 3 (per-vendor calls): Anthropic in Tasks 5–7. The others keep the OpenAI-compat path at the verified fixed endpoints.
   - Goal 4 (all OpenRouter filters): Tasks 8 and 10.
   - Goal 5 (any output type addable): Tasks 1, 9 and 10.
   - Anti-goals: no silent config loss (Task 2 migration tests); key never sent to another host (Tasks 2 and 3); policy not weakened (Task 2 `test_hosted_fixed_endpoint_still_obeys_deployment_policy`, Task 8 uses `_endpoint`); no browser-side OpenRouter call (Task 8 server route).
   - Anthropic section: Tasks 4–7. Embeddings/rerank unsupported: Task 6.
2. **Placeholders:** the Task 8 route tests and the Task 10 UI tests carry comment-form assertions where the existing fixtures decide the harness. Each comment states the exact inputs and expected call.
3. **Type consistency:** `resolve_draft_endpoint → (str, ProviderKind)` (Task 3) feeds `kind=` (Task 4). `ModelGateway.kind` / `AIExecutionConfig.gateway_kind` are used in Tasks 6 and 8. `list_openrouter_models(params)` (Task 8) is built by `to_query_params`. `modelTypeForModality` (Task 9) is used in Task 10. `reasoning_details` is produced in Task 5 and consumed in Tasks 5 and 7.

## Unverified points / risks for the executor

- **Thinking passback (Task 7)** goes beyond the spec's "callers do not change". Two callers need a one-key change, or Claude 5.x tool loops return 400. Anthropic's `tool_use.id` must match `^[a-zA-Z0-9_-]+$`; confirm the harness `call_id` is the provider's id and not one it generates with other characters.
- **Forced `tool_choice`** is always downgraded to auto for Anthropic. Check whether any caller relies on forcing (`grep -rn "tool_choice=" modules`) and still behaves sensibly on a text answer.
- **`anthropic-version: 2026-10-21`** was chosen over the spec's `2023-06-01`, because the docs now show 2026-10-21 everywhere and it formally removes sampling params. Both are valid. Re-check at build time.
- **Read-time kind normalization** rotates `gateway_identity`/`model_identity` once for converted rows. Capability evidence is re-probed once; the key, URL, models and consent are untouched.
- **Gemini discovery** keeps the compat `models.list()`. Ids may come back as `models/<id>`, which is existing behaviour and out of scope.
- **Anthropic models list pagination:** a single `limit=1000` page. `has_more` is not followed.
- **The `anthropic` Python SDK is not a dependency.** Raw `httpx` is used so the pinned `approved_http_client` transport stays in place.
- Frontend script names (`typecheck`, `lint`) were not verified in `apps/web/package.json`.
