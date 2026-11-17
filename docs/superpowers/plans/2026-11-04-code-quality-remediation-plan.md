# Umwelt-OS: Code quality, maintainability & enterprise-readiness remediation plan

- Date: 2026-11-04
- Baseline: `feature/fix-ui` @ `a91749d8` (after pulling 19 new commits). `origin/main` has 30 commits that are not yet merged into this branch.
- Status: **PLAN ONLY.** No code has been changed yet.
- Branch strategy: all work goes into the working branch `feature/fix-ui-quality`, and PRs go **into `feature/fix-ui`**. Nothing merges into `main`.

---

## 0. Principles (apply to every PR)

1. **Two separate tracks; never mix them in one PR.**
   - **R (Refactor, zero logic change):** pure extract-method/extract-component, hoisting closures, gathering helpers, renames. Expected test results must stay unchanged.
   - **F (Fix / Feature, intentional behaviour change):** each change comes with a regression test that demonstrates the new behaviour.
2. **Mandatory gate for every PR:**
   1. GitNexus `impact` (upstream) on every symbol being edited. Record callers, processes and risk in the PR. `HIGH`/`CRITICAL`/`UNKNOWN` require confirmation by text search plus additional tests.
   2. Write a characterization test first if the function lacks coverage (see §3.4).
   3. Refactor, then run: `uv run ruff check`, `uv run mypy`, `uv run pytest` (targeted, then the whole `tests/unit`), `npm run lint`, `npm run typecheck`, `vitest`, and Playwright e2e for UI changes.
   4. `detect_changes --scope compare --base-ref feature/fix-ui`. Changed symbols must match the PR's intent.
3. **Golden rules for splitting backend functions** (lock and transaction safety):
   - An extracted function takes **the same `session`**. It does not open a new session, and does not `commit()`/`rollback()` unless it is extracted together with the commit that already existed.
   - Keep the lock order (access/privacy → conversation/parent → run → evidence; source → connector fence → credential → collector → grant → state → receipt), keep the order of `except` branches, and keep timeout/fence context blocks.
   - Shared state that is rebound via `nonlocal` moves into an explicit dataclass. `seq`/counters are returned explicitly; do not hide them.
   - Every new function gets a docstring describing its lock/commit/ordering contract (AGENTS.md).
4. **Small PRs:** one function, one cluster, or one convention theme per PR, around 400 changed lines or fewer (excluding moved files).
5. **Measurable gates (ratchets):** the CI baseline is committed in Phase 0. The number of violations may only go down. No new violations are allowed.

### Baseline checks recorded at the time of planning

| Check | Result |
|---|---|
| `ruff check` (current config) | pass. The config has no `select`, so the 348 `noqa` comments are all inert (RUF100) |
| `npm run typecheck` | pass (exit 0) |
| `npm run lint` | 0 errors, 42 warnings |
| `pytest tests/unit`, `mypy`, `vitest` | **not run yet.** The run was stopped at the owner's request. Phase 0 must record this baseline before any PR. |

---

## 1. Track F1: per-provider `max_tokens` (feature + fix)

### Requirement
- **Hosted cloud providers `anthropic`, `openai`, `groq`:** users **do not have to set** `max_tokens`, and the UI shows no field.
- **Other providers (`openrouter`, `gemini`, `ollama`, `lmstudio`, `custom`):** add an optional **Max output tokens** field when adding or configuring an LLM model.

### Technical constraints
- The Anthropic Messages API **requires** `max_tokens` in the request. "Not needing to set it" therefore means the gateway decides it internally. Today it is hard-coded to `DEFAULT_STREAM_MAX_TOKENS = 32000` (`core/model_gateway/anthropic.py:25,150`), which causes a 400 on models with lower output limits.
- OpenAI/Groq: do not send `max_tokens`; keep the current behaviour (the provider uses its own default).
- Non-streaming paths still validate `1..8192` (`core/model_gateway/client.py:790,1010,1076`). That value is passed in by internal callers and is separate from the per-model config.

### Design
1. **Schema** (`core/model_gateway/schemas.py`):
   - Add `max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)` to `ModelMapping`, following the same pattern as `reasoning_effort`.
   - Add a constant `PROVIDER_KINDS_WITHOUT_MAX_TOKENS = frozenset({"anthropic", "openai", "groq"})`.
   - Add a validator in `ModelDefaults`/the save path: if the provider kind is in that set, force `max_output_tokens=None` (the server is authoritative; do not trust the client).
2. **Gateway** (`core/model_gateway/client.py`, `anthropic.py`):
   - The resolution order of `max_tokens` for a request is: the explicit argument from the internal caller, then `mapping.max_output_tokens` (only for kinds outside the set), then do not send it at all (OpenAI-compatible) or use the Anthropic internal default.
   - For Anthropic, keep the internal default. On a 400 whose message refers to `max_tokens`, **retry once** with a lower value (`DEFAULT_MAX_TOKENS`), reusing the existing `reasoning_effort` retry pattern, and cache the "clamped" result per `model_identity` for 1 hour.
   - **Do not** record this 400 as `CapabilityUnsupported`.
3. **Settings API/persistence** (`modules/settings`): add the field to the DTO. No migration is needed if `ModelMapping` is stored as JSON; confirm this before implementing. If it is a column, add an additive migration with default NULL.
4. **UI** (`apps/web/src/modules/settings/models-section.tsx`, `ai-settings.tsx`):
   - Add a `Max output tokens` input (shadcn `Input type=number`, optional, i18n en-us/vi-vi), shown only when `kind ∉ {anthropic, openai, groq}`.
   - Switching the provider to a hosted kind clears the value.
5. **Tests:**
   - Unit tests for the schema validator (hosted kind → None).
   - Gateway: payload includes `max_tokens` only for the right kind; Anthropic retry on a `max_tokens` 400; no `unsupported` recorded.
   - Vitest for the conditional field display.
   - Playwright for the "add model custom + max tokens" flow.
6. **Docs:** `docs/model-compatibility.md`.

---

## 2. Track F2: bug and security fixes (intentional behaviour change)

| # | Severity | Issue | Recommended approach | Test |
|---|---|---|---|---|
| F2.1 | 🔴 Critical | Chat SQL tool runs LLM-written SQL as **Postgres superuser**; the regex denylist can be bypassed (`chat_messages`, `query_to_xml`, `lo_from_bytea`) (`modules/chat/db_query.py:33-48,199-227,445`) | (1) Create a `NOSUPERUSER NOINHERIT` role with SELECT only on 2 security-barrier views and switch to it with `SET LOCAL ROLE`. (2) `SET TRANSACTION READ ONLY`. (3) A parser-based allowlist (`pglast`): only 2 relations plus an allowlist of functions. (4) Later, move the whole app off the superuser account onto an owner role created by migrations. | One test per known bypass, plus valid queries that must still pass |
| F2.2 | 🔴 High | Any 400 mentioning "tool"/"function" permanently marks the model `tools: unsupported` (no TTL) (`client.py:228,413,590,879`) | Only record it for an explicit capability phrase or a 404/405; ignore `tool_use_id`/`tool_result`; add a 1-hour TTL to the short-circuit | Unit test classifying errors; TTL expiry |
| F2.3 | 🟠 | Forced `tool_choice` is silently downgraded to `auto` on Anthropic (`anthropic.py:~152`) | Raise `CapabilityUnsupported` so the viz router falls back to `json_schema` | Router test on the Anthropic kind |
| F2.4 | 🟠 | A single timeout/5xx overwrites recorded `supported` → `failed` | Do not record `failed` for timeouts or 5xx | Unit test |
| F2.5 | 🟠 | Default deployment: login lockout is global (all requests share the web container's IP) (`core/auth/routes.py:109-207`) | Fail closed at startup in production when no trusted client-IP source is configured; verify the Caddy → Next → API chain | Config test |
| F2.6 | 🟠 | A generative UI widget can open any https link and send prompts by itself (`sandbox-functions.ts:73-90`, `prompt-bridge.tsx`) | Host-side confirmation dialog; `send-prompt` only fills the composer | Vitest + e2e |
| F2.7 | 🟢 | `json.dumps` on a tool parts list; empty user content → 400; OpenRouter `next_offset` uses the post-filter length; JSDoc says "10/min" instead of 30; `read_rss` does not pin DNS; CSP `unsafe-inline` | Fix each item | Unit tests |

---

## 3. Track R: maintainability and conventions (full fix, Python + Next.js)

### 3.1 Target Definition of Done
- **0 functions/components over 100 lines** in production code (`core/`, `modules/`, `apps/`, `apps/web/src`), enforced by lint:
  - Python: ruff `PLR0915` plus a custom AST check in CI (`scripts/check_function_length.py`, max 100 lines).
  - TS: ESLint `max-lines-per-function: [error, {max: 100, skipBlankLines: true, skipComments: true}]`.
- Complexity: `C901` max 15, `PLR0912` max 15, nesting ≤ 4.
- 0 inert `noqa`; 0 `except Exception: pass`; 0 hard-coded colors or strings in feature code; formatter enforced.
- Files: `.py`/`.tsx` production files ≤ 800 lines (soft target), `public.py` acts only as a facade.

### 3.2 Current state (measured on a91749d8)

| Metric | Python | Next.js |
|---|---|---|
| Total functions | 4,272 | ~1,528 |
| > 100 lines | **169** | **78** |
| > 200 / > 500 lines | 33 / 6 | 32 / 5 |
| C901 / PLR0912 / PLR0915 | 333 / 208 / 175 | — |
| Inert `noqa` | 348 | — |
| `except Exception` (of which `pass`) | 229 (9) | — |
| `_actor`/`_admit` defined repeatedly | 45 | — |
| Non-route files raising `HTTPException` | 81 | — |
| `eslint-disable` | — | 31 |
| Raw `<button>`/`<input>` | — | 26 / 3 |
| Direct `fetch` bypassing `core/api.ts` | — | 5 files |

The full inventory is in Appendix A (Python) and Appendix B (Next.js).

### 3.3 Phase R0: guardrails (no logic change, done first)
1. `pyproject.toml`: add `[tool.ruff.lint] select = ["E","F","I","B","UP","C90","PLR0912","PLR0915","BLE","S","TRY","RUF100"]`, `mccabe.max-complexity = 15`. Generate a per-file baseline (`per-file-ignores` or `--add-noqa` on existing violations only). Thereafter every existing `noqa` actually takes effect.
2. `scripts/check_function_length.py` + `function-length-baseline.json` (the 169 current functions). CI fails if a new function exceeds 100 lines or an existing function grows.
3. `import-linter` (`.importlinter`) with three contracts, each allowlisting the current violations:
   - `modules.X` must not import `modules.Y.models`;
   - `core` must not import `modules`;
   - `descriptor.dependencies` must match the actual imports.
4. Next.js:
   - `prettier` + `prettier --check`. Run a single formatting commit as its own PR (formatting only).
   - ESLint: `max-lines-per-function` (warn + baseline), `complexity`, `no-restricted-imports`/`eslint-plugin-boundaries` (no deep imports across modules), and a ban on hex colors via `no-restricted-syntax`.
5. CI: `uv run mypy`, `pytest`, `vitest` and `tsc` must all run reproducibly. Record the baseline.

### 3.4 Phase R1: safety net (characterization tests before refactoring)

| Function | Current coverage | Tests to add first |
|---|---|---|
| `delete_episode` (`modules/knowledge/temporal/adapter.py:3258`) | **0** | Support-subtraction plan, partial outcome, `NodeNotFoundError`, intent-before-mutation |
| `accept_native_collection` (`modules/ingestion/public.py:2010`) | ~0 (all monkeypatched) | Table-driven matrix per provider (telegram/alpha_vantage/github/google/reddit/null) × replay/lease/fence |
| `execute_tool` (`modules/agents/harness.py:1119`) | Weak | Approval pending/approved/expired, effect state, 413 cap, publication order |
| `process_normalize_event` (`modules/ingestion/worker.py:898`) | Weak | Provider metadata branches, byte budget, validation reject → continue |
| `fetch_native_provider` (`modules/connectors/routes.py:763`) | Source-inspection tests only | Behaviour tests per provider; update `test_none_narrowing_guards`/`test_impact` in the same PR |
| `ConnectorEditor`, `DashboardPage`, `ChatSession` | e2e only | Vitest for the hooks to be extracted, plus e2e for key flows |

### 3.5 Phase R2: low-risk cleanup (mechanical)
**Python**
- Delete dead code: `core/tools.py` (shadowed by `core/tools/`) and `modules/tools/mcp.py` (unused; passes `os.environ` to child processes). Confirm with GitNexus first.
- Gather `_actor`/`_admit` (45 copies) into `core/workspaces/access.py`. **Diff each copy first:** a copy with a different `isinstance` branch keeps its own name and gets a comment explaining why.
- Make cursor helpers use `core/pagination.py`; gather `_utc`, `validate_export_fences`/`export_page` (12 copies) into a shared helper.
- Remove the 348 inert `noqa` comments, or enable their rules (after R0 they actually take effect).
- `except Exception: pass` (9) → specific exception + `logger.debug(..., exc_info=True)`, without changing control flow. Fix the wrong comment at `connectors/worker.py:685`.
- Naming conventions:
  - routes: `routes.py` or a `routes/` package;
  - `schemas.py`/`schemas/`;
  - rename tests named after tickets (`test_w2_phase_fix_p1.py`, `test_s6_hardening.py`, …) after their behaviour, and move them into `tests/unit/modules/<owner>/`.
- Fill the 142 missing docstrings (`connectors/collection.py` 23, `providers/news.py` 15, `core/body_limit.py`, `translations/service.py`, `core/events.py`).

**Next.js**
- Replace the npm `cn@0.4.0` package with a local `lib/utils.ts` (`clsx` + `tailwind-merge`) and unify `components/ui/*`.
- Route the 5 direct `fetch` calls through `core/api.ts`, with an added `apiRequestRaw`/blob variant (keep SSE as is).
- Use a single `core/format.ts` with memoised `Intl` formatters, replacing 3 copies of `formatDate` and 10+ ad-hoc formatters. Add a shared ApiError → message-key helper.
- Replace the 26 raw `<button>` with `Button variant="ghost"`/`asChild`, and raw inputs with primitives.
- Move hard-coded strings to i18n (`aria-label="loading"`, `'Export failed'`, `Ctrl K`).
- Remove leftover pre-rename `bbd` names (`@bbd-os/web`, the `bbd:unauthorized` event) together with every listener.
- Gradually migrate legacy CSS classes (`field`, `label`, …) to Tailwind/primitives.
- Fix the 24 `set-state-in-effect`: use a `key` reset or derive during render. `mcp-collection-editor.tsx` moves to `useQuery` + `reset()`, the `setInterval` state is renamed, and the hard-coded timezone is removed.

### 3.6 Phase R3: split functions over 100 lines (zero logic change), ordered from safest

| Wave | Group | How |
|---|---|---|
| R3.1 | `run_response_generation` (1,071 lines; good test coverage, ~75 tests) | 1. Pure extractions first: `_parse_sse_chunk`, `_validate_citations`. 2. `_claim_run`, `_load_turn_inputs`, `_retrieve_with_web`, `_build_gateway` (with `_SendAttemptFence`). 3. A `_StreamState` dataclass replaces the `nonlocal` closures. 4. A `_live_run_fence` context manager gathers 6 near-identical blocks; diff the 6 blocks first, since they call different cancel functions. 5. `_dispatch_round_tools` last. 6. `_persist_final`. |
| R3.2 | `build_workflow` (591) | Hoist the closures to module level and wire them with `functools.partial(fn, context)`. Keep `.nodes["call_model"].bound` working. |
| R3.3 | `process_entity_extraction_work` (580), `process_normalize_event` (531), `_context` (397), `process_timeline_extraction_work` (279) | Pure validators/normalizers first, then shared `_record_failure`. Phases that share a transaction take the same `session` and commit at the end. |
| R3.4 | Functions that only gain tests after R1: `delete_episode`, `accept_native_collection`, `execute_tool`, `fetch_native_provider`, `dispatch_github_webhooks`, `_advance_copied_cleanup` | Extract the pure parts first (`_plan_support_subtraction`, `_check_lease_matches`, `_decode_tool_arguments`, `_resolve_tool_slots`, `_map_fetch_error`), then the phases carrying locks. |
| R3.5 | The remaining ~150 Python functions between 100 and 300 lines (Appendix A) | Group per module (connectors 43, knowledge 35, chat 22, ingestion 13, news 9, agents 8, …), one PR per module. Mostly extract-method. |
| R3.6 | Next.js: `ConnectorEditor` (1,541), `DashboardPage` (1,311), `ChatSession` (1,086), `DashboardGrid` (567), `FeedStream` (529), `McpSettings` (500), `RealtimeProvider` (493), `PresetPicker` (464), … (Appendix B) | Custom hooks (`useConnectorDraft` with react-hook-form + zod, `useConnectorActivation`, `useDashboardLayout`, `useChatStream`, `useRealtimeStream`) plus child components per provider/section. Keep query keys, i18n keys, DOM structure/aria (so e2e selectors keep working). |

### 3.7 Phase R4: SOLID and architecture (after R3 has tests)

| Principle | Current state | Change |
|---|---|---|
| SRP | `public.py` is a god module (`knowledge/documents/public.py` 5.4k lines, 167 SQL calls) | `public.py` becomes a facade (re-exported DTOs and signatures); logic moves to `service/*.py` and `repository.py`. Routes only parse, authorize and delegate. |
| OCP | 51 `provider == "…"` checks; tool dispatch via an `elif name ==` chain; gateway with 4 `if kind == "anthropic"` branches | Registries: `NativeProofVerifier`/`ProviderCollector` per provider; `ToolHandler` registry; a `WireProtocol` strategy (`OpenAICompat`, `AnthropicMessages`) chosen once in `__post_init__` |
| LSP | Callers check `gateway_kind == "anthropic"`; `delete_episode` mixes return codes and raise | Gateway exposes capabilities (`supports_reasoning_echo`); unify the error contract |
| ISP | `accept_native_collection` takes 8 provider-specific keyword arguments | A param object per provider (`TelegramProof`, `GithubProof`, …) |
| DIP | `core/auth/deletion_cleanup.py` imports 23 `modules.*.erasure`; domain raises `HTTPException` (81 files); `ModelGateway(...)` is hand-built with ~12 arguments | An erasure registry through the descriptor; domain errors in `core/errors.py` mapped to HTTP in routes; a `ModelGatewayFactory` |
| Module boundaries | ~206 imports of other modules' private parts (19 `models`), 40 import cycles | Read projections in the owner's `public.py`; a single common `ModuleDescriptor` (routers, jobs and erasure derived from the descriptor); break cycles with events or shared DTOs |
| Folder structure | Flat `core/` (25 files); `model_gateway` split between core and modules; `connectors/` with 37 flat files; e2e artefacts at the repo root; `packages/` does not exist; `.claude/skills` and `.agents/skills` duplicated | `core/realtime/`, `core/heavy_work/`, `core/storage/`; merge `model_gateway`; `connectors/{catalog,oauth,provisioning,collection}/`; move the e2e logs to `docs/evidence/`; update AGENTS.md. Frontend: `(public)`/`(workspace)` route groups, shell in `layout.tsx`, a public entry file per module, generate API types from OpenAPI. |

---

## 4. Enterprise scorecard (re-scored)

Scale 0–10. A score of 9 or more means it meets the standard of an enterprise app (SLOs, observability, online-safe operation, horizontal scale, enforced gates).

### 4.1 Database, worker, cron and operations layer

| # | Criterion | Score | Main evidence | To reach 9+ |
|---|---|---|---|---|
| 1 | Schema & indexing | 6 | 158 tables, ~350 FKs, 368 indexes, 18 partial indexes. HNSW is per generation. **Duplicate indexes**: 2 identical GIN FTS indexes on `document_chunks` (`0001_init.py:247`/`:3687`), `relationship_evidence(document_id)` twice, 2 redundant indexes on `chat_stream_events` (`:1080-1086`). About **110 FKs without a leading index**. 7 single-column `status` indexes with low selectivity. | Drop duplicates; index FKs used for delete/cascade; turn `status` indexes into partial indexes |
| 2 | Query efficiency | 6 | No ORM lazy loading (0 `relationship()`), keyset pagination, LIMIT on worker slices. N+1 in entity context (4 queries × 100 entities, `chat/retrieval.py:323`). Per-id lock loops. `ts_rank_cd(to_tsvector(content))` re-parses content (`search/public.py:341`). Evidence revalidation re-reads 100 chunks including content on every token flush. | `EXPLAIN` regression suite on the top `pg_stat_statements`; batched `FOR UPDATE ORDER BY id`; stored `tsvector`; lock-only revalidation |
| 3 | Pooling & transactions | 6 | Bounded pools, `pool_timeout=5`, pre-ping, documented 84/100 budget. Worker/model worker use `statement_timeout=0` (`apps/worker/main.py:154,208`). The heavy slot advisory lock is held through a transaction for up to 150 s. A second worker replica exceeds `max_connections`. | PgBouncer (transaction mode); statement timeout per role; session-level advisory locks, no open transaction across I/O |
| 4 | Locking & concurrency | 5 | `SKIP LOCKED` outbox, sorted lock order. **Global privacy lock** `(ns, 1)` with 61 call sites, serializing every chat stream (`memory/public.py:550`). `CREATE INDEX … hnsw` without CONCURRENTLY blocks writes (`search/indexing.py:963`). | Lock per workspace + shared locks for readers; CONCURRENTLY; lock-wait metrics |
| 5 | Migration safety | 4 | 3,779-line squash baseline; downgrade is `DROP SCHEMA public CASCADE` (`0001_init.py:3771`); 0 `CONCURRENTLY`; `env.py` has no `lock_timeout`; already 2 merge heads | `lock_timeout`/`statement_timeout` in `env.py`; expand/contract; CONCURRENTLY with an autocommit block; a migration linter (squawk) + single-head check in CI; replace the destructive downgrade with a guard |
| 6 | Data lifecycle | 5 | Purges for sessions/runs/receipts/health/automation runs. The outbox purge covers only 2 event types. No partitioning for `event_outbox`, `chat_stream_events`, `source_observations`, `realtime_replay_events`. DELETE-based retention causes vacuum churn. | Time partitioning + drop partition; retention for every event type; per-table autovacuum tuning |
| 7 | Worker queue design | 6 | Separate default/model/chat queues; `job_timeout` 180 s matches leases; deterministic job ids. **No exponential backoff, no DLQ**, no visibility of jobs that exhaust `max_tries`; `stop_grace_period` 30 s < `job_timeout` 180 s; Redis 256 MB `noeviction` | Backoff + jitter; DLQ table + replay tool; drain on SIGTERM; queue depth/age alerts |
| 8 | Cron/scheduler | **4** | 29 cron jobs: **8 jobs every 5 s**, 8 jobs at second :00 every minute, and about 24 jobs together at hh:00:00 against `max_jobs=6` (thundering herd, **no jitter**). No single-flight lease per cron; per-process `asyncio.Lock` is not safe across replicas; missed runs are not caught up. | Spread offsets and add jitter; a Redis/PG lease per cron; turn the 5 s pollers into LISTEN/NOTIFY or event-driven enqueue; a separate light worker for cron |
| 9 | Outbox/eventing | 7 | Transactional outbox, `SKIP LOCKED`, stale reclaim, invalid-envelope quarantine, at-least-once with idempotent job ids. No per-aggregate ordering; no replay tool; outbox code lives inside `ingestion` instead of `core`. | Move the outbox to core; ordering contract; consumer idempotency table; replay CLI; lag metrics |
| 10 | Caching | 5 | Redis for metrics, cursors, breaker state, rate limits. Model gateway limits are per process. A new HTTP client/TLS handshake on every model call (`model_gateway/client.py:543`). The query-vector cache is per process and skips page 1. | Global Redis semaphore; cached HTTP client per origin; explicit cache layer with invalidation events |
| 11 | Observability | 4 | `X-Request-ID`, JSON logs, redaction, `pg_stat_statements`, slow-query log at 500 ms. **No Prometheus/OTel**; Langfuse is off by default; no metrics for queue depth, outbox lag or cron duration; **no alerting** | OTel traces API→arq→DB; Prometheus exporter (pool, queue, outbox lag, cron, breaker); alert rules + dashboards |
| 12 | Scalability & resilience | 4 | Circuit breaker, jittered retry, timeouts, rate limit, body limit. The connection budget blocks horizontal scaling; global locks and in-process state; single Redis instance with `noeviction`; heavy slot serializes the whole cluster | Stateless workers with distributed leases; PgBouncer; Redis HA; autoscaling by queue depth |
| 13 | Load testing & SLOs | **1** | `docs/performance-report.md`: "measurement pending", every metric null; no k6/Locust | Define SLOs (chat TTFT, search p95, ingest lag, SSE delivery); k6 scenarios in CI on a seeded dataset |

**Weighted score for the data/worker/ops layer: ≈ 5.1/10.**
- Weights: schema, queue 10; pool, lock, outbox 9; query, lifecycle, cron, observability 8; migration 7; scale 6; cache, load test 4.
- Relative to the earlier ~5.2 assessment, locking dropped one point because the global lock directly affects every chat stream.

### 4.2 Overall enterprise scorecard

| Area | Score | Short comment |
|---|---|---|
| Architecture & modularity | 5 | Modular monolith in the right direction, but boundaries are not enforced: ~206 private imports, 40 cycles, core → modules |
| Folder structure | 6 | Mirrors the domain well; core is flat, connectors oversized, model_gateway split |
| Maintainability (function size, SOLID) | **3** | 247 functions/components over 100 lines; 6 over 500 lines; SRP/OCP/DIP violated on core paths |
| Conventions & quality gates | 5 | Docstrings 96–99%, clean i18n; but lint gates are inert (348 `noqa`), no formatter, mypy not reproducible |
| Security | 6 | Strong auth/CSRF/SSRF/sandbox controls; **1 Critical** (SQL tool superuser) pulls the score down |
| App-path performance (chat/search/indexing) | 4 | Indexing at 50 chunks/minute, heavy flushes, SSE polling, global lock |
| Data/worker/cron/ops layer | 5.1 | §4.1 |
| Frontend performance | 5 | No `next/dynamic`; Recharts loaded eagerly; 41% client components |
| Testing & safety net | 5 | ~350 unit test files and good chat coverage; critical functions (`delete_episode`, `accept_native_collection`) have nearly 0 coverage; no load tests |
| Async correctness (no event-loop blocking) | 8 | §5A.2: argon2, parsing, chunking, uploads and DNS are all offloaded; still left: HTMLParser on the worker loop (up to ~6 s), live-map parsing on the API loop (~0.5 s) |
| File storage / S3 readiness | 3 | §5A.1: local filesystem only, no storage abstraction, OS file-lock fence, multiple modules access the path directly |
| **Overall (simple average)** | **≈ 5.0/10** | Detailed and careful in correctness (locks, fences, provenance, async offload), weak in operability, maintainability and storage scalability |

---

## 5. Track P: performance, DB, worker, cron (intentional behaviour change, measured before and after)

Each item first needs a benchmark or `EXPLAIN (ANALYZE, BUFFERS)` baseline, then a change, then a re-measurement. Record the results in `docs/performance-report.md`.

| # | Item | Impact | Effort | Score target |
|---|---|---|---|---|
| P1 | Observability: Prometheus exporter + OTel traces (API→arq→DB), metrics for pool, queue depth/age, outbox lag, cron duration, breaker; alert rules | High | M | #11 → 8 |
| P2 | Migration safety: `lock_timeout` in `env.py`, CONCURRENTLY, squawk + single-head check in CI, guarded downgrade | High | S–M | #5 → 8 |
| P3 | HNSW `CREATE INDEX CONCURRENTLY` outside the transaction | High | S | #4 |
| P4 | Privacy lock per workspace + shared locks for readers (61 call sites, needs F-track tests) | High | M | #4 → 8 |
| P5 | Cron: offsets/jitter, single-flight lease per cron, 5 s pollers → LISTEN/NOTIFY or enqueue-driven, cron worker split from ingestion | High | M | #8 → 8 |
| P6 | Lightweight chat flush revalidation (ids/generation only; one `IN` query for source fences; budget-based revalidation); SSE via Redis pub/sub instead of polling | High | M | App perf |
| P7 | Batch embedding (32–128 chunks/request), enqueue on document ready instead of cron only | High | M | App perf |
| P8 | Cache the model gateway HTTP client per origin (keep DNS pinning) | Medium | S | #10 |
| P9 | Index migration: drop 2 duplicates + 3 redundant indexes; add FK indexes on delete/cascade paths; partial `status` indexes; stored `tsvector` | Medium | S | #1 → 8 |
| P10 | PgBouncer + recomputed per-role connection budget; per-role `statement_timeout` | High | M | #3, #12 |
| P11 | arq exponential backoff, DLQ table + replay, graceful drain ≥ job timeout | Medium | M | #7 → 8 |
| P12 | Partition `event_outbox`/`chat_stream_events`/`source_observations`/`realtime_replay_events`; retention for every event type | Medium–High | M–L | #6 → 8 |
| P13 | Batch APIs for entity context (`get_entities`, `list_entity_evidence_bulk`, `get_neighbors_bulk`) | Medium | M | #2 |
| P14 | Frontend: `next/dynamic` per gadget, SVG sparklines, `(workspace)/layout.tsx` to avoid remounting the shell | Medium | S–M | FE perf |
| P15 | SLOs + k6 scenarios (chat TTFT, search p95, ingest lag, SSE) on a seeded dataset in CI | Medium | M | #13 → 7 |

---

## 5A. File processing / S3 and async (event loop) review

### 5A.1 File processing and S3: current state

**Conclusion: the codebase has no S3 at all.** There is no `boto3`/`aioboto3`/`minio`/`obstore` in `pyproject.toml` or the code. Files are stored on the **local filesystem** (`core/storage.py`, `core/storage_admission.py`, a shared volume between api and worker). The spec only requires "storage abstraction so MinIO can be added later" (`specs/personal-intelligence-os-spec-v2.md:384`), and that abstraction **does not exist yet**.

**Current upload/processing flow (works well on local disk):**

| Step | Implementation | Blocks the loop? | Assessment |
|---|---|---|---|
| Receive upload | `save_upload` (`core/storage.py:39`) reads 1 MB at a time, writes and fsyncs via `to_thread_joined`, streaming SHA-256, temp file + `os.replace` (atomic), checks symlink/traversal | No (only mkdir/mkstemp/replace are synchronous, at µs–ms scale) | ✅ Good: bounded, atomic, joins the thread on cancel |
| Workspace fence | `workspace_filesystem_lease` (`core/storage_admission.py:21`): OS file lock `LOCK_NB` + permanent deletion flag with fsync | No (non-blocking lock, < 1 ms on local disk) | ✅ Correct on local disk; ❌ **does not work on S3** (no flock, no inode) |
| Validate upload | `to_thread_joined` (`modules/ingestion/routes.py:225`) | No | ✅ |
| Parse PDF/DOCX | `parse_file_bounded`: separate **spawn process** + timeout + terminate/join (`modules/ingestion/parsers.py:128-173`) | No (`Process.start()` inline ~5–30 ms) | ✅ Very good: isolates CPU and memory, can be killed |
| Small-text parse | `wait_for(to_thread(...))` (`parsers.py:141-145`) | No | ⚠️ On timeout the thread keeps running after the heavy slot is released, which breaks the "join before release" rule |
| Chunking/tiktoken | `to_thread_joined(chunk_text, …)` (`ingestion/worker.py:1483`) | No | ✅ |
| Download | `FileResponse` streams asynchronously, `nosniff` + attachment (`documents/routes.py:284`) | No | ✅ |
| Erasure / orphan cleanup | `unlink`/`scandir`/`rmdir` inline (`knowledge/documents/erasure.py:86-146`, `worker.py:564`); orphan sweep via `to_thread` (`ingestion/worker.py:1584`) | Very small on local disk | ⚠️ Becomes noticeable if the volume is NFS or EBS |

**Blockers when moving to S3/MinIO (or when scaling api/worker across multiple hosts):**

1. **No storage abstraction:** `storage_path(root, …)`, `Path`, `os.replace` and `FileResponse(path)` are used directly in ingestion, documents, erasure and chat media. Every caller depends on the filesystem.
2. **The workspace fence relies on OS file locks and a deletion-flag file.** S3 has no equivalent, and on S3 the fence would lose its account-erasure guarantee. A fence based on Postgres (advisory lock/row) or a Redis lease plus a durable deletion flag in the DB is needed.
3. **Atomic publish with temp + `os.replace`:** on S3 this needs multipart upload + `If-None-Match: *` (conditional put), or a write to a staging key followed by a DB pointer update.
4. **The parser process needs a local path:** objects must be downloaded to a temp file (streaming, bounded) before parsing, and temp space has to be cleaned up.
5. **Download through `FileResponse`:** this has to change to a streaming proxy or a presigned URL with a short TTL. A presigned URL bypasses API authorization and the `nosniff`/attachment headers, so it needs `response-content-disposition` and a short TTL.
6. **A shared volume is required between api and worker:** this is the main constraint on horizontal scaling (P10/§4.1 #12).
7. **Orphan cleanup uses `glob` on the filesystem:** on S3 this needs a prefix listing + lifecycle rules, or a reference table.

**Plan S (storage), intentional behaviour change, after R0 and R1:**

| # | Item | Notes |
|---|---|---|
| S1 | Define a `StorageBackend` Protocol in `core/storage/` with async methods: `put_stream(key, aiter, max_bytes) -> (size, sha256)`, `open_stream(key)`, `download_to_temp(key) -> Path`, `delete(key)`, `exists(key)`, `list_prefix(prefix)` | First step **with no behaviour change**: `LocalFilesystemBackend` wraps the current code 1:1 |
| S2 | Move every caller (ingestion routes/worker, documents erasure/routes, chat media, orphan sweep) onto the protocol | Pure R-track refactor; tests kept unchanged |
| S3 | Separate the **fence** from the filesystem: a `StorageFence` interface. The local implementation keeps the flock; the S3 implementation uses a PG advisory lock per workspace + a `workspace_storage_state(deleted_at)` row | Keep the "deletion flag is never undone" invariant |
| S4 | `S3Backend` (`aioboto3` or `obstore`, both async): streaming multipart, conditional put, SSE-S3/KMS encryption, explicit `endpoint_url` for MinIO, credentials from env/secret, prefix `workspaces/<uuid>/documents/<uuid>/` | Add MinIO to `docker-compose.dev.yml` as an option; add `.env.example` placeholders |
| S5 | Download: keep streaming through the API by default (keeps authorization and headers); presigned URLs are an option with TTL ≤ 60 s + `response-content-disposition=attachment` | |
| S6 | Parse: `download_to_temp` (bounded, streaming) → parser process → delete temp in `finally` | |
| S7 | Lifecycle/orphans: a reference table + paginated `list_prefix`, or S3 lifecycle rules for the `staging/` prefix | |
| S8 | Tests: contract tests that run the same suite against `LocalFilesystemBackend` and `S3Backend` (MinIO in the integration harness) | Needs owner approval for Docker |

### 5A.2 Async / event-loop blocking in the Python API and workers

**Conclusion: good, rated 8/10.** The design is clearly async-aware.
- 0 of 361 routes are synchronous `def` handlers.
- Every client is async: SQLAlchemy async, `redis.asyncio`, `AsyncOpenAI`, Neo4j `AsyncGraphDatabase`, LangGraph `ainvoke`, MCP `create_subprocess_exec`.
- There is no `requests`, `httpx.Client`, `time.sleep` or `subprocess.run` in request or job code.
- argon2 runs in `to_thread`, including the dummy verify that keeps login timing uniform. Parsing, chunking, upload I/O, DNS for SSRF checks, XML/feed parsing, BeautifulSoup and export rendering are all offloaded.
- `to_thread_joined` joins the thread on cancel.

What still blocks (track A, all are `to_thread` wrappers with **no logic change**: same input/output and order, only moved off the loop):

| # | Severity | Location | What blocks | Worst case | Process | Fix |
|---|---|---|---|---|---|---|
| A1 | 🔴 High | `modules/connectors/collection.py:764-765` (`_run_web`) | Pure-Python `HTMLParser.feed()` on pages up to 10 MB | ~2.6 s/page of 10 MB, ~6 s per run → stalls all 6 arq slots + cron (heartbeat, heavy-slot renew) | Worker | Move the parse into a sync helper returning `(parts, links)` and call it with `to_thread_joined` (`crawl.py:176/477` already does this) |
| A2 | 🟠 Medium | `modules/connectors/live_map.py:123-221` via `dashboard/live_map_routes.py:291` | CSV/JSON parse up to 4 MB + `sorted()` + `_item()` for every row | ~0.3–0.6 s on a cache miss | **API** | `await asyncio.to_thread(_parse, provider, body)`. Stopping item construction after `MAX_ITEMS` is a separate optimisation that keeps the row count |
| A3 | 🟠 Medium | `modules/ingestion/worker.py:676-710` (`_collect_web_job`) | `json()` 10 MB + 500× `model_validate`/`model_dump` + canonical dumps + sha256 | ~0.2–0.4 s | Worker | Gather the validate/dump/hash steps into a sync helper and run it in `to_thread`; SQL stays where it is |
| A4 | 🟠 Medium | `connectors/providers/rest.py:378` (+ `map_page`), `research.py:59`, `social.py:113`, `google_api.py:112`, `local_services.py:169` | `json.loads` on pages up to 10 MB + mapping | ~70–150 ms/page | Worker | `await asyncio.to_thread(json.loads, body)`; offload `map_page` too |
| A5 | 🟢 Low–Med | `connectors/collection.py:1253-1574` (direct `_payload(...)` calls) | JSON decode + map for macro/SEC/OpenAlex/GDELT… | ~30–100 ms | Worker | Wrap with `to_thread`, as the feed path already does (`:1451/1490`) |
| A6 | 🟢 Low | `ingestion/parsers.py:141-145` | `wait_for(to_thread)`: a timed-out thread outlives the heavy slot | — | Worker | `to_thread_joined` inside `asyncio.timeout` |
| A7 | 🟢 Low | `knowledge/documents/public.py:1835` `content_hash`, `crawl.py:485-561` | sha256/json.dumps on up to 10M characters | ~20–40 ms | API/worker | Fold into the existing `to_thread_joined` |
| A8 | 🟢 Low | `core/auth/routes.py:83` `get_auth_redis` | The only **sync dependency**, which runs through the AnyIO threadpool for no reason | µs + handoff | API | Make it `async def` |
| A9 | 🟢 Low | `core/storage.py:60-83`, `storage_admission.py:21-74`, `documents/erasure.py:86-146` | Sync mkdir/replace/unlink/fsync/scandir | ms on local disk; noticeable on NFS/EBS | API/worker | Optional: `to_thread_joined` around the publish/erase block when storage is a network volume (S2 will cover this) |
| A10 | ℹ️ | SSRF DNS `to_thread(getaddrinfo)` + `wait_for(5)` (`connectors/public.py:3244`, `rest.py:79`, `local_services.py:103`) | Timed-out lookups occupy threads in the shared default executor (`min(32, cpu+4)`), which can starve argon2/uploads | — | API/worker | `loop.getaddrinfo` or a separate small executor for DNS; consider `set_default_executor` with an explicit size |
| A11 | ℹ️ Startup | `core/chunking.py:8` | `tiktoken.get_encoding` downloads the BPE at import time if it is not cached, so offline startup fails | — | API + worker | Pre-cache in `docker/api.Dockerfile` + `TIKTOKEN_CACHE_DIR` |
| A12 | ℹ️ | `docker/api.Dockerfile:21` `--loop asyncio` | uvloop is not used even though it is installed | Throughput | API | Consider `--loop uvloop` after benchmarking (P15) |

**Guardrails so the loop does not regress:**
- A pytest fixture enables `asyncio` debug mode with `loop.slow_callback_duration = 0.1` in the integration/worker tests, and logs and fails on any callback blocking for more than 100 ms.
- Production: a Prometheus metric for event-loop lag (P1).
- Ruff `ASYNC` rules (`ASYNC210/230/251`) are added to the `select` list in R0.

---

## 6. Order and PR breakdown (all PRs go into `feature/fix-ui`)

| Step | Content | Type |
|---|---|---|
| 1 | Phase R0 guardrails + record the pytest/mypy/vitest baseline | R |
| 2 | F2.1 SQL tool (Critical) | F |
| 3 | F1 per-provider `max_tokens` + F2.2 tool-unsupported TTL + F2.3/F2.4 | F |
| 4 | R2 mechanical cleanup (Python, then Next.js; prettier formatting in a separate PR) | R |
| 5 | R1 characterization tests | Tests |
| 6 | R3.1, R3.2 (chat worker, harness) | R |
| 7 | P2, P3, P9 (migration safety, CONCURRENTLY, indexes) + A1–A8 (async offload, small PRs, same input/output) | P / R |
| 8 | R3.3 to R3.6 (one PR per module) | R |
| 9 | F2.5 to F2.7 | F |
| 10 | P1, P4, P5, P6, P7, P8 | P |
| 11 | R4 SOLID/architecture + folder restructure | R |
| 12 | P10 to P15 | P |
| 13 | S1–S2 (StorageBackend + LocalFilesystemBackend, no behaviour change), then S3–S8 (fence, S3/MinIO backend) | R → F |

Merge strategy: each PR is rebased onto the latest `feature/fix-ui`. Merging `main` into `feature/fix-ui` (30 commits ahead) is a separate decision for the owner and is not part of this plan.

### Definition of Done for the whole plan
- 0 production functions/components over 100 lines; C901 ≤ 15; lint/format/import-linter gates pass with no baseline exceptions left.
- No test regressions relative to the Phase 0 baseline; every F-track item has a regression test.
- Data/worker/ops scorecard ≥ 8 for criteria 1–9 and 11; recorded SLOs and k6 results.
- Docs updated: `docs/development.md` (gates), `docs/model-compatibility.md` (max tokens), `docs/performance-report.md`, AGENTS.md (actual structure).

---

## Appendix A: Python functions over 100 lines (169)

Measured with AST over `core/`, `modules/`, `apps/` (excluding migrations). Nested functions are counted separately and also inside the line count of their parent.

| # | Lines | Function | Location |
|---|---|---|---|
| 1 | 1071 | `run_response_generation` | `modules/chat/worker.py:912` |
| 2 | 681 | `delete_episode` | `modules/knowledge/temporal/adapter.py:3258` |
| 3 | 591 | `build_workflow` | `modules/agents/harness.py:968` |
| 4 | 580 | `process_entity_extraction_work` | `modules/knowledge/entities/worker.py:456` |
| 5 | 531 | `process_normalize_event` | `modules/ingestion/worker.py:898` |
| 6 | 524 | `accept_native_collection` | `modules/ingestion/public.py:2010` |
| 7 | 405 | `execute_browser_read` | `modules/tools/browser_public.py:299` |
| 8 | 399 | `execute_tool` | `modules/agents/harness.py:1119` |
| 9 | 397 | `_context` | `modules/knowledge/temporal/worker.py:552` |
| 10 | 388 | `fetch_native_provider` | `modules/connectors/routes.py:763` |
| 11 | 367 | `dispatch_github_webhooks` | `modules/connectors/worker.py:896` |
| 12 | 336 | `_advance_copied_cleanup` | `modules/knowledge/documents/worker.py:774` |
| 13 | 331 | `_make_driver_type` | `modules/knowledge/temporal/adapter.py:1335` |
| 14 | 317 | `stdio_client_transport` | `modules/tools/mcp_stdio.py:325` |
| 15 | 306 | `purge_document_copied_evidence_page` | `modules/agents/public.py:1119` |
| 16 | 304 | `_index_generation` | `modules/search/indexing.py:588` |
| 17 | 304 | `reconcile_canonical_nodes` | `modules/knowledge/temporal/adapter.py:2672` |
| 18 | 290 | `build_context` | `modules/chat/retrieval.py:239` |
| 19 | 283 | `enqueue_github_hint` | `modules/connectors/public.py:2391` |
| 20 | 280 | `reconcile_fact_recovery` | `modules/knowledge/temporal/adapter.py:2977` |
| 21 | 279 | `process_timeline_extraction_work` | `modules/timeline/worker.py:124` |
| 22 | 277 | `activate_source` | `modules/connectors/provisioning_routes.py:773` |
| 23 | 271 | `evaluate_gadget_highlights` | `modules/dashboard/public.py:151` |
| 24 | 255 | `publish_extracted_events` | `modules/timeline/public.py:1323` |
| 25 | 236 | `crawl` | `modules/connectors/crawl.py:82` |
| 26 | 232 | `_lock_worker_event` | `modules/ingestion/worker.py:308` |
| 27 | 230 | `export_page` | `modules/chat/public.py:1102` |
| 28 | 227 | `_run_native` | `modules/connectors/collection.py:829` |
| 29 | 220 | `_execute_structured_mode` | `modules/chat/db_query.py:537` |
| 30 | 216 | `get_response_events` | `modules/chat/routes.py:1093` |
| 31 | 214 | `export_page` | `modules/knowledge/documents/public.py:388` |
| 32 | 214 | `generate_brief` | `modules/dashboard/briefs.py:628` |
| 33 | 206 | `create_app` | `apps/api/main.py:87` |
| 34 | 198 | `_captured_inputs_match` | `modules/dashboard/briefs.py:370` |
| 35 | 190 | `drive_activation` | `modules/connectors/activation.py:114` |
| 36 | 189 | `purge_document_copied_evidence_page` | `modules/chat/public.py:1676` |
| 37 | 183 | `search` | `modules/search/public.py:427` |
| 38 | 183 | `collect` | `modules/connectors/mcp.py:213` |
| 39 | 182 | `_apply_normalized_document` | `modules/knowledge/documents/public.py:3139` |
| 40 | 181 | `recover_news_work` | `modules/news/worker.py:229` |
| 41 | 180 | `collect_github_releases` | `modules/connectors/providers/social.py:123` |
| 42 | 177 | `process_agent_run` | `modules/agents/worker.py:449` |
| 43 | 176 | `revoke_github_grant` | `modules/connectors/github/routes.py:882` |
| 44 | 176 | `mutate_message` | `modules/chat/routes.py:914` |
| 45 | 175 | `purge_expired_chat_runs` | `modules/chat/worker.py:703` |
| 46 | 174 | `list_stories` | `modules/news/stories.py:579` |
| 47 | 174 | `list_review_candidates` | `modules/knowledge/entities/public.py:1132` |
| 48 | 174 | `collect_gmail` | `modules/connectors/providers/google_api.py:283` |
| 49 | 170 | `_handle_browser_read` | `modules/tools/browser.py:20` |
| 50 | 170 | `process_news_document_ready` | `modules/news/worker.py:57` |
| 51 | 168 | `collect_drive` | `modules/connectors/providers/google_api.py:838` |
| 52 | 167 | `erase_account_remote_page` | `modules/connectors/erasure.py:113` |
| 53 | 166 | `_recover` | `modules/knowledge/temporal/worker.py:1383` |
| 54 | 166 | `validate_github_segment` | `modules/connectors/github/sync.py:191` |
| 55 | 162 | `assign_review_candidate` | `modules/knowledge/entities/public.py:1349` |
| 56 | 162 | `export_page` | `modules/knowledge/entities/public.py:236` |
| 57 | 162 | `complete_github_authorization` | `modules/connectors/github/routes.py:650` |
| 58 | 161 | `_apply_configured_reranking` | `modules/chat/retrieval.py:76` |
| 59 | 160 | `browser_control_event` | `modules/tools/browser_control.py:141` |
| 60 | 160 | `_records_from_feed` | `modules/connectors/providers/feed_catalog.py:284` |
| 61 | 159 | `process_graph_operation` | `modules/knowledge/temporal/worker.py:1222` |
| 62 | 159 | `receive_batch` | `modules/ingestion/public.py:2536` |
| 63 | 157 | `enable_builtin` | `modules/connectors/builtins.py:295` |
| 64 | 156 | `__call__` | `modules/tools/mcp_auth.py:158` |
| 65 | 156 | `recover_timeline_extraction_work` | `modules/timeline/worker.py:430` |
| 66 | 156 | `google_callback` | `core/auth/routes.py:761` |
| 67 | 154 | `_discover` | `modules/knowledge/entities/corrections.py:183` |
| 68 | 153 | `_invoke_tool` | `core/tools/registry.py:177` |
| 69 | 152 | `build_correlations` | `modules/news/correlation.py:26` |
| 70 | 152 | `dispatch_pending_work` | `modules/ingestion/dispatcher.py:153` |
| 71 | 151 | `collect_slack_channel_messages` | `modules/connectors/providers/workspace_apps.py:328` |
| 72 | 149 | `update_event` | `modules/timeline/public.py:911` |
| 73 | 149 | `_live_story_rows` | `modules/news/stories.py:382` |
| 74 | 149 | `revoke_deleted_source_github_grants` | `modules/connectors/worker.py:276` |
| 75 | 147 | `recover_entity_extraction_work` | `modules/knowledge/entities/worker.py:307` |
| 76 | 146 | `merge_entity` | `modules/knowledge/entities/corrections.py:854` |
| 77 | 146 | `_collect_telegram_page` | `modules/connectors/routes.py:1153` |
| 78 | 146 | `call_model` | `modules/agents/harness.py:972` |
| 79 | 145 | `process_document_ready` | `modules/knowledge/entities/worker.py:135` |
| 80 | 144 | `_make_clients` | `modules/knowledge/temporal/adapter.py:1668` |
| 81 | 144 | `decision` | `modules/agents/approvals.py:322` |
| 82 | 143 | `_memory_export_source_fence` | `modules/memory/public.py:251` |
| 83 | 143 | `get_conversation` | `modules/chat/routes.py:567` |
| 84 | 142 | `inspect_write_receipt` | `modules/knowledge/temporal/adapter.py:2529` |
| 85 | 142 | `_list_relationships_as_of` | `modules/knowledge/relationships/public.py:363` |
| 86 | 142 | `resolve_relationship_review` | `modules/knowledge/entities/public.py:1513` |
| 87 | 140 | `validate_export_fences` | `modules/chat/public.py:1334` |
| 88 | 137 | `sse_event_stream` | `modules/chat/routes.py:1160` |
| 89 | 135 | `seed_demo` | `modules/knowledge/documents/seed.py:61` |
| 90 | 134 | `read_rss` | `modules/connectors/n8n.py:363` |
| 91 | 134 | `_request` | `core/model_gateway/client.py:557` |
| 92 | 133 | `apply_preset` | `modules/dashboard/public.py:2037` |
| 93 | 132 | `probe_model` | `modules/model_gateway/routes.py:222` |
| 94 | 132 | `get_connector_configuration` | `modules/connectors/public.py:1092` |
| 95 | 132 | `drive_workflow_operation` | `modules/connectors/provisioning.py:2886` |
| 96 | 131 | `source_cleanup_progress` | `modules/knowledge/documents/public.py:2265` |
| 97 | 131 | `map_telegram_update` | `modules/connectors/providers/telegram.py:207` |
| 98 | 130 | `__post_init__` | `modules/knowledge/temporal/adapter.py:422` |
| 99 | 130 | `validate_draft_configuration` | `modules/connectors/provisioning_routes.py:334` |
| 100 | 129 | `delete_canonical_entity` | `modules/knowledge/entities/corrections.py:526` |
| 101 | 128 | `_retain_and_append` | `core/realtime.py:457` |
| 102 | 127 | `score_relevance` | `modules/news/relevance.py:96` |
| 103 | 126 | `_search_for_run` | `modules/chat/worker.py:397` |
| 104 | 125 | `list_trends` | `modules/news/trends.py:18` |
| 105 | 125 | `_record_bulk_write_intent` | `modules/knowledge/temporal/adapter.py:2139` |
| 106 | 125 | `publish_event` | `modules/ingestion/public.py:639` |
| 107 | 124 | `run_specialist_handoff` | `modules/agents/harness.py:842` |
| 108 | 122 | `split_entity` | `modules/knowledge/entities/corrections.py:1002` |
| 109 | 121 | `get_document_correction_decisions` | `modules/knowledge/entities/public.py:2141` |
| 110 | 121 | `collect_huggingface_models` | `modules/connectors/providers/research.py:72` |
| 111 | 121 | `_execute_static_agent_read` | `modules/connectors/crawl.py:392` |
| 112 | 120 | `suggest_candidates` | `modules/memory/public.py:1893` |
| 113 | 119 | `queue_connector_crawl` | `modules/ingestion/public.py:2775` |
| 114 | 119 | `build_workflow` | `modules/connectors/n8n.py:109` |
| 115 | 118 | `validate` | `modules/connectors/registry.py:42` |
| 116 | 118 | `scrub_document_runs` | `modules/automations/execution.py:636` |
| 117 | 116 | `save_ai_settings` | `modules/settings/public.py:645` |
| 118 | 116 | `_stream_inner` | `core/model_gateway/client.py:856` |
| 119 | 115 | `_discover_delete_closure` | `modules/knowledge/entities/corrections.py:409` |
| 120 | 115 | `process_ingestion_event` | `modules/ingestion/worker.py:780` |
| 121 | 114 | `complete_google_authorization` | `modules/connectors/oauth_routes.py:273` |
| 122 | 113 | `purge_source_copied_evidence_page_in_uow` | `modules/memory/public.py:996` |
| 123 | 113 | `validate_provider_snapshot_fields` | `modules/knowledge/documents/schemas.py:220` |
| 124 | 113 | `wake_packaged_collection` | `modules/connectors/public.py:3006` |
| 125 | 112 | `classify_telegram_probe` | `modules/ingestion/schemas.py:346` |
| 126 | 112 | `retry_run` | `modules/ingestion/public.py:3193` |
| 127 | 112 | `acknowledge_workflow_step` | `modules/connectors/provisioning.py:2576` |
| 128 | 112 | `collect_calendar` | `modules/connectors/providers/google_api.py:541` |
| 129 | 111 | `scrub_document_evidence` | `modules/notifications/public.py:323` |
| 130 | 111 | `cluster_observation` | `modules/news/stories.py:146` |
| 131 | 111 | `erase_account_storage_page` | `modules/knowledge/documents/erasure.py:40` |
| 132 | 111 | `accept_plan` | `modules/goals/public.py:574` |
| 133 | 111 | `collect_home_assistant_snapshot` | `modules/connectors/providers/local_services.py:315` |
| 134 | 110 | `process_source_memory_coverage` | `modules/sources/worker.py:465` |
| 135 | 110 | `github_connection_status` | `modules/connectors/github/routes.py:358` |
| 136 | 110 | `revalidate_context_fence` | `modules/chat/retrieval.py:531` |
| 137 | 109 | `create_relationship` | `modules/knowledge/relationships/public.py:859` |
| 138 | 109 | `admit_collection_request` | `modules/connectors/scheduler.py:279` |
| 139 | 109 | `erase_account_shared_rows` | `modules/connectors/erasure.py:282` |
| 140 | 108 | `parse_github_delivery` | `modules/connectors/github/webhooks.py:122` |
| 141 | 107 | `_action_step` | `modules/automations/execution.py:1076` |
| 142 | 106 | `purge_document_copied_evidence_page` | `modules/memory/public.py:834` |
| 143 | 106 | `process_uploaded_file` | `modules/ingestion/worker.py:1433` |
| 144 | 106 | `validate_citations` | `modules/chat/citations.py:311` |
| 145 | 105 | `get_story` | `modules/news/stories.py:811` |
| 146 | 105 | `delete_document` | `modules/knowledge/documents/public.py:4205` |
| 147 | 105 | `_recover_unknown_create` | `modules/connectors/worker.py:625` |
| 148 | 105 | `_account_segment` | `modules/agents/worker.py:167` |
| 149 | 104 | `remove_provider_credential` | `modules/connectors/provisioning_routes.py:1079` |
| 150 | 104 | `begin_activation_bundle_in_uow` | `modules/connectors/provisioning.py:1547` |
| 151 | 104 | `_cursor_sweep` | `modules/automations/producers.py:68` |
| 152 | 103 | `probe_draft` | `modules/model_gateway/routes.py:116` |
| 153 | 103 | `_save_desired_in_uow` | `modules/connectors/provisioning.py:1334` |
| 154 | 102 | `preview_preset` | `modules/dashboard/public.py:1933` |
| 155 | 102 | `send_message` | `modules/chat/routes.py:804` |
| 156 | 102 | `resolve_gadget_context` | `modules/chat/public.py:325` |
| 157 | 102 | `build_meeting_time_picker_payload` | `modules/chat/generative_ui/tools/form.py:104` |
| 158 | 102 | `extract_supporting_quote` | `modules/chat/citations.py:62` |
| 159 | 101 | `request_translations` | `modules/translations/public.py:202` |
| 160 | 101 | `revalidate_native_output_fences_many` | `modules/tools/public.py:193` |
| 161 | 101 | `revalidate_fence` | `modules/tools/mcp_runtime.py:454` |
| 162 | 101 | `list_observations` | `modules/knowledge/observations/public.py:779` |
| 163 | 101 | `_collect_web_job` | `modules/ingestion/worker.py:634` |
| 164 | 101 | `receive_file` | `modules/ingestion/public.py:2896` |
| 165 | 101 | `reconcile_github_source_hints_lifecycle` | `modules/connectors/public.py:2759` |
| 166 | 101 | `query_records` | `modules/chat/generative_ui/tools/query.py:113` |
| 167 | 101 | `build_plan_payload` | `modules/chat/generative_ui/tools/plan.py:63` |
| 168 | 101 | `_execute_sql_mode` | `modules/chat/db_query.py:434` |
| 169 | 101 | `erase_account_data` | `core/auth/deletion_cleanup.py:76` |

## Appendix B: Next.js functions/components over 100 lines (78)

Estimated by brace matching over `apps/web/src` (excluding tests); may be off by a few lines.

| # | Lines (approx.) | Function/component | Location |
|---|---|---|---|
| 1 | 1541 | `ConnectorEditor` | `apps/web/src/modules/sources/connector-editor.tsx:359` |
| 2 | 1311 | `DashboardPage` | `apps/web/src/modules/dashboard/dashboard-page.tsx:329` |
| 3 | 1086 | `ChatSession` | `apps/web/src/modules/chat/chat-session.tsx:130` |
| 4 | 567 | `DashboardGrid` | `apps/web/src/modules/dashboard/dashboard-grid.tsx:190` |
| 5 | 529 | `FeedStream` | `apps/web/src/modules/dashboard/gadgets/feed-gadget.tsx:154` |
| 6 | 500 | `McpSettings` | `apps/web/src/modules/sources/mcp-editor.tsx:97` |
| 7 | 493 | `RealtimeProvider` | `apps/web/src/core/realtime-provider.tsx:120` |
| 8 | 464 | `PresetPicker` | `apps/web/src/modules/dashboard/preset-picker.tsx:87` |
| 9 | 379 | `GadgetEditor` | `apps/web/src/modules/dashboard/gadget-editor.tsx:399` |
| 10 | 350 | `EntityDetail` | `apps/web/src/modules/knowledge/entity-detail.tsx:64` |
| 11 | 336 | `GenerativeBarChart` | `apps/web/src/modules/chat/generative-ui/components/charts/bar-chart.tsx:187` |
| 12 | 310 | `GadgetFrame` | `apps/web/src/modules/dashboard/gadget-frame.tsx:112` |
| 13 | 298 | `TableCatalog` | `apps/web/src/modules/chat/generative-ui/components/table-catalog.tsx:236` |
| 14 | 296 | `OpenGenerativeUIRenderer` | `apps/web/src/modules/chat/generative-ui/sandbox/renderer.tsx:184` |
| 15 | 288 | `MemoryPrivacySettings` | `apps/web/src/modules/settings/memory-privacy.tsx:26` |
| 16 | 287 | `AISettingsWorkspace` | `apps/web/src/modules/settings/ai-settings.tsx:55` |
| 17 | 286 | `ToolRendering` | `apps/web/src/modules/chat/generative-ui/components/tool-rendering.tsx:47` |
| 18 | 271 | `PreferencesDialog` | `apps/web/src/modules/account/preferences-dialog.tsx:69` |
| 19 | 266 | `ChatTranscript` | `apps/web/src/modules/chat/chat-transcript.tsx:216` |
| 20 | 266 | `ChatComposer` | `apps/web/src/modules/chat/chat-composer.tsx:76` |
| 21 | 257 | `MemoryList` | `apps/web/src/modules/knowledge/memory-list.tsx:204` |
| 22 | 256 | `MeetingTimePicker` | `apps/web/src/modules/chat/generative-ui/components/meeting-time-picker.tsx:128` |
| 23 | 252 | `LayoutEditor` | `apps/web/src/modules/dashboard/layout-editor.tsx:115` |
| 24 | 244 | `WorkspaceShell` | `apps/web/src/core/app-shell/workspace-shell.tsx:123` |
| 25 | 237 | `openStream` | `apps/web/src/core/realtime-provider.tsx:338` |
| 26 | 232 | `PlanCard` | `apps/web/src/modules/chat/generative-ui/components/plan-card.tsx:139` |
| 27 | 230 | `GenerativePieChart` | `apps/web/src/modules/chat/generative-ui/components/charts/pie-chart.tsx:83` |
| 28 | 223 | `MetricsChart` | `apps/web/src/modules/dashboard/gadgets/metrics-chart.tsx:127` |
| 29 | 215 | `ProviderScope` | `apps/web/src/modules/sources/provider-scope.tsx:22` |
| 30 | 215 | `GlobeMap` | `apps/web/src/modules/dashboard/gadgets/globe-map.tsx:115` |
| 31 | 214 | `RuleEditor` | `apps/web/src/modules/automations/rule-editor.tsx:85` |
| 32 | 201 | `TopicSettings` | `apps/web/src/modules/news/topic-settings.tsx:37` |
| 33 | 200 | `StoryList` | `apps/web/src/modules/news/story-list.tsx:49` |
| 34 | 196 | `CommandPalette` | `apps/web/src/core/command-palette.tsx:53` |
| 35 | 195 | `ExportOverlay` | `apps/web/src/modules/chat/generative-ui/sandbox/export-overlay.tsx:58` |
| 36 | 195 | `PromptBridgeProvider` | `apps/web/src/modules/chat/generative-ui/bridge/prompt-bridge.tsx:114` |
| 37 | 191 | `ChatControllerProvider` | `apps/web/src/core/app-shell/chat-controller.tsx:140` |
| 38 | 178 | `ChatHistory` | `apps/web/src/modules/chat/chat-history.tsx:40` |
| 39 | 174 | `StoryDetail` | `apps/web/src/modules/news/story-detail.tsx:37` |
| 40 | 172 | `useChatScroll` | `apps/web/src/modules/chat/use-chat-scroll.ts:13` |
| 41 | 172 | `createTripAnimator` | `apps/web/src/modules/chat/generative-ui/sandbox/trip-animator.ts:47` |
| 42 | 167 | `WatchlistGadget` | `apps/web/src/modules/dashboard/gadgets/watchlist-gadget.tsx:75` |
| 43 | 163 | `TimelineGadget` | `apps/web/src/modules/dashboard/gadgets/timeline-gadget.tsx:35` |
| 44 | 157 | `AgentSettingsWorkspace` | `apps/web/src/modules/agents/agent-settings.tsx:28` |
| 45 | 156 | `SourceEntry` | `apps/web/src/modules/sources/source-list.tsx:154` |
| 46 | 154 | `WorkspaceMembers` | `apps/web/src/modules/settings/workspace-members.tsx:39` |
| 47 | 152 | `StarterPackWizard` | `apps/web/src/modules/sources/starter-pack-wizard.tsx:36` |
| 48 | 144 | `CitationPanel` | `apps/web/src/modules/chat/citation-panel.tsx:36` |
| 49 | 139 | `SystemStatusDashboard` | `apps/web/src/modules/observability/system-status-dashboard.tsx:55` |
| 50 | 136 | `InputForm` | `apps/web/src/modules/chat/generative-ui/components/input-form.tsx:122` |
| 51 | 133 | `ChatPageContent` | `apps/web/src/app/chat/[[...id]]/page.tsx:22` |
| 52 | 130 | `TodosPanel` | `apps/web/src/modules/chat/todos/todos-panel.tsx:32` |
| 53 | 130 | `GuardedNavigationProvider` | `apps/web/src/core/guarded-navigation.tsx:38` |
| 54 | 128 | `BuiltinWizard` | `apps/web/src/modules/sources/builtin-sources.tsx:169` |
| 55 | 124 | `SourceList` | `apps/web/src/modules/sources/source-list.tsx:329` |
| 56 | 123 | `InviteAccept` | `apps/web/src/app/invite/invite-accept.tsx:64` |
| 57 | 120 | `GithubProjectGadget` | `apps/web/src/modules/dashboard/gadgets/github-project-gadget.tsx:48` |
| 58 | 119 | `EntityGadget` | `apps/web/src/modules/dashboard/gadgets/entity-gadget.tsx:60` |
| 59 | 118 | `RuleList` | `apps/web/src/modules/settings/highlight-rule-editor.tsx:86` |
| 60 | 118 | `SingleWeatherPanel` | `apps/web/src/modules/dashboard/gadgets/weather-panel.tsx:122` |
| 61 | 118 | `FlatMap` | `apps/web/src/modules/dashboard/gadgets/flat-map.tsx:93` |
| 62 | 117 | `TimelinePage` | `apps/web/src/modules/timeline/timeline-page.tsx:25` |
| 63 | 116 | `DocumentDetail` | `apps/web/src/modules/knowledge/document-detail.tsx:27` |
| 64 | 115 | `TablePanel` | `apps/web/src/modules/dashboard/gadgets/table-panel.tsx:58` |
| 65 | 114 | `SelectedSourcesMapGadget` | `apps/web/src/modules/dashboard/gadgets/map-gadget.tsx:36` |
| 66 | 110 | `LoginPage` | `apps/web/src/app/login/page.tsx:36` |
| 67 | 108 | `TaskList` | `apps/web/src/modules/tasks/task-list.tsx:20` |
| 68 | 108 | `CreateMemoryDialog` | `apps/web/src/modules/knowledge/memory-list.tsx:54` |
| 69 | 108 | `UserInputRequest` | `apps/web/src/modules/chat/generative-ui/components/user-input-request.tsx:38` |
| 70 | 108 | `useChatAttachments` | `apps/web/src/modules/chat/chat-attachments.tsx:63` |
| 71 | 107 | `EntityDialogBody` | `apps/web/src/modules/detail/entity-dialog-body.tsx:24` |
| 72 | 106 | `McpCollectionEditor` | `apps/web/src/modules/sources/mcp-collection-editor.tsx:19` |
| 73 | 106 | `VizRouterCard` | `apps/web/src/modules/settings/viz-router-card.tsx:34` |
| 74 | 106 | `SearchPage` | `apps/web/src/modules/search/search-page.tsx:47` |
| 75 | 105 | `OnboardingPage` | `apps/web/src/modules/settings/onboarding-page.tsx:39` |
| 76 | 103 | `DocumentDeletionReceiptPanel` | `apps/web/src/modules/knowledge/document-deletion-receipt.tsx:22` |
| 77 | 103 | `DisplayPreferencesProvider` | `apps/web/src/core/query-provider.tsx:70` |
| 78 | 101 | `WizardShell` | `apps/web/src/modules/sources/source-wizard.tsx:27` |
