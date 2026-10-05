# Umwelt-OS Phase 11 — Observability and Operational Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Make runs, failures, quality, usage and system capacity inspectable without leaking secrets or turning monitoring into a runtime dependency.

**Architecture:** Implement the observability, settings capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phases 1–10 already emit run IDs, events, status and timing supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query, phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 69–70, 95, 110, 126–127, 130, 138.21/23/28, 161. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phases 1–10 already emit run IDs, events, status and timing.

**Implementation status:** In progress (2026-10-05): T1 code/build/source review complete through repair 71fd47e and composed; T2 complete through e8b02e7; T3 active; T4 pending. Runtime acceptance remains deferred.

Code stage: implement production code and run affected production builds only. Do not create or modify test files, or run tests, lint, standalone typecheck, audits, or non-build acceptance checks. Begin deferred behavioral acceptance only after all Phase 1-12 production code is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R06 health and R16**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

## Review Focus

1. Missing token/cost values remain unknown rather than zero.
2. Private content and credentials must not be copied into telemetry by default.
3. A telemetry backend outage must not fail a knowledge write or agent result.
4. Retention jobs cannot delete canonical documents, raw files or version history.
5. A no-change poll should not generate repetitive operational logs or notifications.

## File Structure and Boundaries

Module ownership: **observability, settings**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.

## Task P11-T1: Structured traces, metrics and data minimization

**Files and responsibilities:** Create core/telemetry.py; modules/observability/schemas.py, modules/observability/public.py, modules/observability/routes.py; wire existing ingestion/agent/gateway request hooks.

**Interfaces — consumes/produces:** TraceContext(request_id,ingestion_run_id?,agent_run_id?,tool_call_id?); GET /system/metrics and /system/runs with owner protection. Usage(tokens_in?,tokens_out?,cost?,model_identity?) nullable when unavailable.


- [x] **P11-T1.3 — Implement the minimal production behavior.** Reuse installed logging/structlog and stable run IDs. Instrument API latency/errors, queue delay, ingest stages, embeddings, models and tool duration with bounded-cardinality labels. Redact auth headers, cookie values, credential URLs and provider keys; raw prompts/documents disabled by default. Unknown usage stays null; price estimates carry model/rate timestamp and never substitute for authoritative billing. Optional telemetry sink errors are isolated from application transactions.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"model_identity":null,"tokens_in":null,"tokens_out":null,"estimated_cost":null,"latency_ms":120}
```

- [x] **P11-T1.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [x] **P11-T1.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P11-T2: Operations and data-quality screens

**Files and responsibilities:** Create apps/web/src/modules/observability/operations-page.tsx, apps/web/src/modules/observability/run-table.tsx, apps/web/src/modules/observability/quality-panel.tsx, apps/web/src/modules/observability/usage-panel.tsx; integrate operations into advanced Data sources/AI settings, without new top-level groups

**Interfaces — consumes/produces:** GET /system/quality returns document counts,duplicate rate,unresolved entities,failed ingestion/extraction,stale sources,orphan chunks,graph lag; GET /system/queue returns bounded job summaries, not raw payloads.


- [x] **P11-T2.3 — Implement the minimal production behavior.** Build filterable runs/failures with links to evidence and run details; expose queue state, retry eligibility and actual worker health. Aggregate quality with bounded SQL queries and pagination; do not load every document into memory. Source stale thresholds use configured cadence. Existing UI errors remain visible even when observability profile is disabled.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"orphan_chunks":0,"graph_sync_lag_seconds":null,"usage_state":"unavailable"}
```

- [x] **P11-T2.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [x] **P11-T2.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P11-T3: Retention, maintenance and module lifecycle

**Files and responsibilities:** Create modules/observability/retention.py; extend worker maintenance and module settings; docs/operations.md.

**Interfaces — consumes/produces:** GET/PATCH /settings/retention; defaults agent traces 90 days,raw sources retain,document history retain; maintenance run summaries include deleted counts and next eligible time. Module disable removes schedules/navigation/tools while preserving data.


- [ ] **P11-T3.3 — Implement the minimal production behavior.** Use indexed timestamps and configured cutoffs for bounded cleanup batches. Delete expired telemetry and temporary data in bounded batches, with indexes and time limits; never run blocking vacuum/full-database maintenance from a UI request. Emit no log for unchanged polls/zero work; warn on actionable failure and debug on actual maintenance. Disable modules through descriptor dependency checks so dependents become unavailable consistently without cascade deletion.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"agent_trace_days":90,"raw_source_retention":"retain","document_history_retention":"retain"}
```

- [ ] **P11-T3.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P11-T3.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P11-T4: Optional Langfuse profile and operations documentation

**Files and responsibilities:** Create infrastructure/observability/compose.yml and docs/observability.md; update docs/deployment.md and status.

**Interfaces — consumes/produces:** Optional Langfuse integration is configured by deployment profile; public application APIs and core persistence are unchanged when unavailable. Egress of telemetry containing content requires explicit owner policy.


- [ ] **P11-T4.3 — Implement the minimal production behavior.** Keep Langfuse off on the base 8GB deployment. Pin integration dependency only if profile implemented; provide scrubbed trace summaries and owner-selected content policy. Record measurements with and without optional telemetry. Document troubleshooting from UI error to run to correlation ID, health boundaries and no-op logging semantics.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"telemetry_sink":"disabled","content_capture":false}
```

- [ ] **P11-T4.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P11-T4.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Phase Build and Handoff

- [ ] Run the affected production build with `./scripts/dev.ps1 build` (or `make build`) and resolve failures.
- [ ] Record changed production files, exact build command/result, review findings, unresolved gates, and phase status in `EXECUTION.md`.
- [ ] Continue to the next ready production task. Do not start deferred acceptance before all Phase 1-12 production code is complete.

## Deferred test-stage acceptance (not implementation tasks)

Run behavioral, integration, UI, live-service, recovery, and capacity checks only after all Phase 1-12 production code is complete. Do not create or modify test files during the code stage.
- **P11-T1:** Nested secrets, URLs with credentials, telemetry failure, absent model usage, unbounded-label rejection and API protection.
- **P11-T2:** Large synthetic run history paging, no raw payload leak, link to correct failed step, gateway unknown state, worker/graph/n8n outage and disabled optional metrics.
- **P11-T3:** Cutoff boundary, retry/idempotency, no-op log capture, module disable during pending job, dependency conflicts and preservation of stored knowledge.
- **P11-T4:** Start core stack without sink, simulate sink timeout, verify healthy writes and readable prior logs. Common gate; next P12-T1.

- Validate the review-focus risks listed above, production packaging/OpenAPI/module registration, and live or hardware-dependent requirements from the specification.
- Record deferred test-stage results and blocked live, restore, or target-capacity evidence in the execution ledger.

## Plan Self-Review Checklist

- [x] Goal, specification coverage, public interfaces, dependencies and file responsibilities are retained.
- [x] Implementation tasks contain production work, affected builds and build evidence only.
- [x] Behavioral acceptance is explicitly deferred until all Phase 1-12 production code is complete.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.



