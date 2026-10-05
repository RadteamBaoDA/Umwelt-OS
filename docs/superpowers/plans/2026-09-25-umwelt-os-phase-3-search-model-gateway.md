# Umwelt-OS Phase 3 — Search and Model Gateway Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Provide lexical/hybrid search with safe OmniRoute access, explicit privacy controls and versioned embedding indexes.

**Architecture:** Implement the search, model_gateway, settings capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 2 chunks/provenance; live model acceptance needs configured endpoint and permitted aliases supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query, pytest and Playwright; phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 8, 33–35, 63–65, 76–77, 95, 113–114, 121–123, 157. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 2 chunks/provenance; live model acceptance needs configured endpoint and permitted aliases.

**Implementation status:** Original phase production code/build/review complete; behavioral acceptance deferred. Supplemental tasks not started; do not rerun completed baseline.

Code stage: implement production code and run affected production builds only. Do not create, modify or run tests, lint, or standalone typecheck until production code for all Phase 1-12 is complete. Behavioral acceptance is listed separately in the deferred test-stage section.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R05/R06**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

Main navigation is Dashboard/Chat/Settings; Settings has three groups; account/appearance/language live in the user menu. Detail routes do not become main-navigation items. Migration names below are historical planning examples: inspect current head and active worktrees, including unfinished Phase 4 0007_entities.py, and allocate a unique linked revision without rewriting shipped migrations.

## Global Constraints

- "Never hardcode secrets."
- "Every schema change must use Alembic."
- "Agents must use defined tools and APIs."
- "Create directories only when used."
- Public APIs use `/api/v1`; preserve single-owner auth, session-bound CSRF, source policy and provenance.
- Target 2 CPU cores/8 GiB with remote inference; never claim measured capacity from a larger host.
- Follow the master's mandatory privacy, module, durable-job, deletion and UI contracts. Keep source data and credentials out of logs.
- The owner has authorized committing completed work and merging a completed phase into `main`; do not push, deploy, change branches outside planned worktrees, or perform destructive owner-data operations.

## File Structure and Boundaries

Module ownership: **search, model_gateway, settings**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.

## Task P03-T1: Gateway configuration, capability probes and privacy policy


**Production files and responsibilities:** Create core/model_gateway/schemas.py, core/model_gateway/client.py and core/model_gateway/policy.py; modules/settings/models.py, modules/settings/schemas.py and modules/settings/routes.py; modules/model_gateway/routes.py; apps/web/src/modules/settings/models.tsx and apps/web/src/modules/settings/privacy.tsx.

**Interfaces — consumes/produces:** ModelGateway.chat, stream, embed, structured, tools, rerank accept an explicit RequestPolicy with reasoning_allowed,embeddings_allowed,local_only, permitted_destinations. POST /settings/models/{alias}/test; GET/PATCH /settings/privacy; secret values are write-only and represented as configured flags. Aliases: reasoning-large,reasoning-small,fast,embedding,reranker,vision,local-private.

- [x] **P03-T1.1 - Implement production behavior.** Implement may_send as a fail-closed predicate and apply it before all outgoing requests, including probes containing owner data. Store secrets in server configuration initially; settings UI edits alias mappings, not provider secrets. Separate capability results per alias/model/version and expire results when mapping changes. Cache no evidence of privacy guarantees from gateway hostname alone; unknown destination denies local-only sends. Add synthetic capability probes, explicit request deadlines, bounded retry only on transient errors, shared two-request concurrency cap with expiring leases across API/worker. Do not install a local LLM by default.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"alias":"embedding","capabilities":{"embeddings":"untested","streaming":"unsupported"},"credential_configured":false}
```

- [x] **P03-T1.2 - Build the affected deliverable.** `./scripts/dev.ps1 build` passed on 2026-09-26 after lockfile-pinned npm dependencies were installed. Next.js production build and Docker web/API/worker/migrate image builds succeeded.

- [x] **P03-T1.3 - Record build evidence, commit and continue.** Build evidence and review findings are recorded in `EXECUTION.md`; commits `4d1af2e`, `efac142`, and `3bbfb91` contain the implementation and review fixes. Independent review approved after two scoped fix rounds. Continue to P03-T2.

## Task P03-T2: Lexical index and embedding generations


**Production files and responsibilities:** Create modules/search/models.py, modules/search/indexing.py, modules/search/public.py, modules/search/schemas.py and modules/search/routes.py; infrastructure/postgres/migrations/versions/0006_search.py (down_revision: 0005_source_purge_operations); extend worker and deletion hooks.

**Interfaces — consumes/produces:** POST /search {query,filters,mode,limit,cursor}; SearchHit includes title,excerpt,score,source,observed_at,published_at,document_version_id,chunk_id,citation. IndexGeneration(model_id,dimensions,status); POST /search/reindex -> run_id. Modes lexical|hybrid, with effective_mode and warnings.

- [x] **P03-T2.1 - Implement production behavior.** Use PostgreSQL simple text-search configuration for Unicode lexical baseline; parameterize query construction. Pin vector dimensions per generation; create a new physical index for new generations, then switch active generation atomically after verification. Keep per-item indexing status so partial failures are visible. Use reciprocal-rank fusion for hybrid results. If query embeddings are unavailable return lexical results with effective_mode=lexical and a warning; never report hybrid success. Gate deleted/private references at retrieval time as well as indexing.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"items":[],"next_cursor":null,"effective_mode":"lexical","warnings":["Semantic search unavailable"]}
```

- [x] **P03-T2.2 - Build the affected deliverable.** `./scripts/dev.ps1 build` passed on 2026-09-26 after the final fix wave; Next.js and Docker web/API/worker/migrate images built.

- [x] **P03-T2.3 - Record build evidence, commit and continue.** Build evidence and review findings are recorded in `EXECUTION.md`; commits `006f053`, `8b68563`, and `3783b11` contain the implementation and review fixes. Independent review approved after two scoped fix rounds. Continue to P03-T3.

## Task P03-T3: Search UI and command palette


**Production files and responsibilities:** Create apps/web/src/modules/search/api.ts, apps/web/src/modules/search/search-page.tsx and apps/web/src/modules/search/search-results.tsx; apps/web/src/core/command-palette.tsx; add thin /search route.

**Interfaces — consumes/produces:** Search UI consumes the response contract without inventing normalized confidence. Ctrl/Cmd+K opens available actions; '/' focuses search except in inputs/editors. Citation links navigate to protected document revision views.

- [x] **P03-T3.1 - Implement production behavior.** Add query, source/date/type filters and stable pagination; show lexical fallback and indexing progress. Use accessible list results with date/source/excerpt, no fabricated scores or snippets. Register command-palette actions from enabled modules as they appear. Preserve typed search/filter state in URL; never put auth or raw private text in unrelated telemetry.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"query":"project","filters":{"source_ids":[],"date_from":null,"date_to":null,"content_types":[]}}
```

- [x] **P03-T3.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P03-T3.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit `5445f5a`. Independent review approved with no material findings. Continue to P03-T4.

## Task P03-T4: Search quality baseline and model acceptance record


**Production files and responsibilities:** Create modules/search/evaluation.py and docs/model-compatibility.md; update docs/IMPLEMENTATION_STATUS.md and execution ledger.

**Interfaces:** `recall_at_k(ranked_ids: list[str], expected_ids: set[str], k: int) -> float`; empty expected sets return 1.0 only for empty results, otherwise 0.0 for explicit no-answer fixtures. Deferred evaluation data contain queries, expected document IDs and allowed filters; reports include recall@10 and latency, with provider latency separate and current-generation configuration captured.

- [x] **P03-T4.1 - Implement production behavior.** Implement `recall_at_k(ranked_ids, expected_ids, k)` with the specified empty-set behavior. Create the model compatibility record with fields for endpoint release, model identity, dimensions, privacy routing guarantees and capabilities; unknown values remain unknown. This record is a dependency for extraction and Graphiti.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"alias":"embedding","model_id":"configured-model","dimensions":1536,"live_verified":false}
```

- [x] **P03-T4.2 - Build the affected deliverable.** `./scripts/dev.ps1 build` passed on 2026-09-26; Next.js and Docker web/API/worker/migrate images built.

- [x] **P03-T4.3 - Record build evidence, commit and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and proceed to Phase 3 whole-branch review.

## Phase Acceptance and Handoff

- [x] Build the phase deliverables with `./scripts/dev.ps1 build` (or `make build`).
- [x] Confirm packaging, Alembic metadata, API routes and module descriptors are included in affected production builds.
- [x] Complete independent source review and fix actionable findings; rebuild after production-code fixes.
- [x] Update `docs/IMPLEMENTATION_STATUS.md`, this checklist and `EXECUTION.md`; advance to whole-branch review.

Production-code completion for all Phases 1-12 is the gate to begin the separate deferred test stage.

## Deferred test-stage acceptance

This section is informational only during the code stage. Do not create or modify tests until production code for all Phase 1-12 is complete.

- Local-only content cannot leave via a reasoning or embedding fallback.
- Changing embedding model or dimensions never mixes vector spaces.
- No gateway preserves lexical retrieval and explicitly disables remote-dependent search.
- Deleting content removes it from retrieval even while indexing jobs are running.
- Search must handle Vietnamese text, filters and empty queries without unsafe SQL.

### P03-T1

Planned test-stage files: `tests/test_model_policy.py`.

- Local-only or non-opted-in remote requests are denied before any transport call. Cover privacy/fallback branches using an injected transport. Live probes write redacted evidence only when endpoint, aliases and opt-in are supplied; missing prerequisites defer live acceptance.

### P03-T2

Planned test-stage files: `tests/integration/test_search_index.py`, `tests/test_index_generation.py`.

- Lexical search returns results without embeddings. Cover filters, Unicode queries, stale generations, provider dimension mismatch, deletion during embedding, failed rebuild preserving the active index, and duplicate indexing events. Verify hybrid mode separately with a permitted live embedding alias.

### P03-T3

Planned test-stage files: `tests/e2e/search.spec.ts`.

- Cover keyboard shortcut exclusions, empty results, error/retry, filters, citation navigation and offline lexical access.

### P03-T4

Planned test-stage files: `tests/fixtures/search/relevance.json`, `tests/test_search_quality.py`.

- After all production code is complete, evaluate at least ten deterministic retrieval questions spanning Vietnamese text, dates, sources and no-answer cases; require exact known-term documents in lexical top 10. Record hybrid recall without claiming provider quality from stubs and complete the compatibility record using permitted live-provider evidence when available. The example dimension is not a configured default.

## Plan Self-Review Checklist

- [x] Goal and spec sections mapped to named tasks and public interfaces.
- [x] Deferred acceptance risks retained for the post-code test stage.
- [x] Exact production file targets, deferred acceptance criteria, implementation rules and build criteria included.
- [x] Module ownership, auth/privacy, safe deletion and retry/resume boundaries preserved.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.
