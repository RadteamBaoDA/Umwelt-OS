# Umwelt-OS Phase 12 — Hardening, Backup, Recovery and Full Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Verify the full approved product on a clean installation and documented mini-host workload with recoverable backups and honest operational limits.

**Architecture:** Implement the backup, export, settings plus cross-module validation capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Functional acceptance from Phases 1–11; actual target hardware and permitted live integrations for final release gate supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query, phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 71, 78–94, 108–135, 155, 161–162. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Functional acceptance from Phases 1–11; actual target hardware and permitted live integrations for final release gate.

**Implementation status:** Not started. This file is an implementation plan, not evidence of working code.

Code stage: implement production code and run affected production builds only. Do not create or modify test files, or run tests, lint, standalone typecheck, audits, or non-build acceptance checks. Begin deferred behavioral acceptance only after all Phase 1-12 production code is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R02–R16 lifecycle and R16 release**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

1. A backup that exists is not a verified restore, especially across PostgreSQL, raw files, n8n and graph.
2. Forget must reach vectors/graph/memory/caches and block in-flight recreation.
3. The 2-core/8GB target needs measured concurrent workload, not a Docker startup claim.
4. Drawer focus/viewport/context behavior must hold across desktop, mobile and interrupted streams.
5. Fresh installation and upgrade must preserve owner data; cleanup must never target user volumes.

## File Structure and Boundaries

Module ownership: **backup, export, settings plus cross-module validation**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.

## Task P12-T1: Consistent backup/restore and export

**Files and responsibilities:** Create modules/backup/manifest.py, modules/backup/service.py, modules/backup/routes.py; modules/export/public.py, modules/export/routes.py; scripts/backup.py and scripts/restore.py; frontend backup/storage settings; extend Makefile and scripts/dev.ps1.

**Interfaces — consumes/produces:** make backup, make restore BACKUP=... and PowerShell equivalents; authenticated backup/export operation endpoints return operation_id. Manifest includes component/schema versions,checksums,files,creation time,consistency method and required protected key references.


- [ ] **P12-T1.3 — Implement the minimal production behavior.** Quiesce ingestion/agents/automations and writes for the baseline consistency window; record state and resume even on failure. Snapshot PostgreSQL, raw files, compatible graph backup, n8n data and its encryption key, schedules and configuration; protect secret-bearing archive content and do not log keys. Restore into a separate instance first; validate versions/checksums and database migrations, and re-register jobs and workflows from restored configuration. Export JSON/Markdown/CSV per domain through public APIs, excluding credentials. Whole-instance restore onto existing owner data requires explicit destructive confirmation.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"format_version":1,"consistency":"quiesced","components":["postgres","raw_files","graph","n8n","configuration"],"checksums":{}}
```

- [ ] **P12-T1.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P12-T1.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P12-T2: Cross-module deletion, security and recovery safeguards

**Files and responsibilities:** Extend per-module deletion hooks and operation status; create docs/security.md; revise privacy settings help.

**Interfaces — consumes/produces:** Source deletion modes connector_only|with_data; immediate tombstone denies retrieval, durable purge removes every owned derived copy. DELETE entity/conversation and memory forget preserve unrelated/shared evidence; backup retention is explained separately.


- [ ] **P12-T2.3 — Implement the minimal production behavior.** Implement deletion handling for source documents, chunks, vectors, graph episodes, memory, citation payloads, caches, queued jobs and trace content under one deletion intent. Keep shared facts only when independent retained evidence supports them. Preserve the session/CSRF, upload-size, archive/PDF, SSRF, prompt-injection, MCP-grant, approval-replay and log safeguards defined by the owning modules. Recovery matrix covers kill API/worker/Redis/graph/n8n at durable boundaries; external uncertain effects require review. Apply production fixes at the owning module; validate them through the affected production build.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"operation_id":"uuid","status":"running","immediate_access_revoked":true,"remaining_stages":["graph","files"]}
```

- [ ] **P12-T2.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P12-T2.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P12-T3: Complete onboarding, demo data and drawer accessibility

**Files and responsibilities:** Extend onboarding route flow and seed modules; update docs/ux-proposals/2026-09-26-life-dashboard-proposal.md and README.md.

**Interfaces — consumes/produces:** Onboarding: owner -> model/privacy -> capability test -> sources -> explicit sample/personal import -> indexing progress -> Dashboard. Each step is resumable; no model or network still permits existing local data access.


- [ ] **P12-T3.3 — Implement the minimal production behavior.** Seed fictional projects/events/articles/tasks/entities/relationships/conversations only on explicit command with stable IDs and no user-data overwrite. Implement onboarding and drawer behavior using existing navigation, settings, accessibility and responsive UI patterns. Drawer default closed, right overlay/full-mobile, focus trap/return, preserved draft/context, explicit Stop and citations/history on full Chat; no permanent empty column. If the client cannot reach the server, show offline/unavailable; no browser-cache offline guarantee is implied. When only external providers are unavailable but Umwelt-OS is reachable, retained data and lexical search remain usable; remote-dependent chat/semantic query is explicitly unavailable. Add safe reset command with named workspace and explicit destructive confirmation, never a successful no-op.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"onboarding_step":"indexing","lexical_ready":true,"semantic_ready":false,"reason":"No permitted embedding model"}
```

- [ ] **P12-T3.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P12-T3.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P12-T4: Target hardware capacity and service budget

**Files and responsibilities:** Create docs/performance-report.md; update deployment profiles and limits only from measurements.

**Interfaces — consumes/produces:** Report exact hardware/OS/architecture,all versions,enabled profiles,gateway location,model IDs,dataset/concurrency,peak memory,CPU,queue delay and latency distribution. No hardware capacity statement from the development host alone.


- [ ] **P12-T4.3 — Implement the minimal production behavior.** Define the performance report and production configuration fields needed to record target-host measurements; change deployment limits only from measured results during the deferred acceptance stage.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"target_host_verified":false,"latency":{"provider_ms":null,"application_ms":null},"oom_events":null}
```

- [ ] **P12-T4.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P12-T4.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P12-T5: Clean-install, upgrade and complete-product release gate

**Files and responsibilities:** Create docs/release-checklist.md; update README.md, docs/deployment.md, docs/IMPLEMENTATION_STATUS.md, docs/ARCHITECTURE_DECISIONS.md and execution ledger.

**Interfaces — consumes/produces:** Final acceptance uses published documented commands, protected production proxy paths, backup/restore and module enable/disable. Use the production module descriptor and registration contracts; do not add test-only descriptors during code implementation.


- [ ] **P12-T5.3 — Implement the minimal production behavior.** Prepare release documentation for clean installation, Phase 0 upgrade, protected proxy paths, module enable/disable, backup/restore and acceptance status. Keep full-product acceptance pending for the deferred test stage; do not change feature code solely to satisfy an unverified gate.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"functional_acceptance":"pending","live_integrations":"pending","target_capacity":"pending","restore_verified":false}
```

- [ ] **P12-T5.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P12-T5.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Phase Build and Handoff

- [ ] Run the affected production build with `./scripts/dev.ps1 build` (or `make build`) and resolve failures.
- [ ] Record changed production files, exact build command/result, review findings, unresolved gates, and phase status in `EXECUTION.md`.
- [ ] Continue to the next ready production task. Do not start deferred acceptance before all Phase 1-12 production code is complete.

## Deferred test-stage acceptance (not implementation tasks)

Run behavioral, integration, UI, live-service, recovery, and capacity checks only after all Phase 1-12 production code is complete. Do not create or modify test files during the code stage.
- **P12-T1:** Successful restore after simulated host loss, missing raw blob/key, corrupt checksum, incompatible schema and interrupted backup; restored n8n credentials usable without printing them. Export-import roundtrip for supported canonical fields.
- **P12-T2:** No deleted content returned while purge pending, no resurrection after worker replay, independent evidence retained, expiry/revocation checks, and recovery without losing acknowledged data. Old backups remain governed by backup retention rather than falsely claimed erased.
- **P12-T3:** Fresh/resumed onboarding, keyboard-only flow, screen-reader dialog labels, viewport overflow, day switch during stream, drawer/full Ask same conversation, offline mode and demo repeat idempotency.
- **P12-T4:** The performance_report fixture reads measured output, never fabricated metrics; skip with explicit reason when target host absent, leaving this gate blocked. Off-host graph/browser or changing capabilities requires an explicit architecture/deployment decision.
- **P12-T5:** automated behavior checks; disposable clean migrations, seed, E2E, live connector/model evidence, recovery, and target performance report. Final independent review fixes actionable findings; preserve no-commit/no-push/no-deploy boundary unless separately authorized.

- Validate the review-focus risks listed above, production packaging/OpenAPI/module registration, and live or hardware-dependent requirements from the specification.
- Record deferred test-stage results and blocked live, restore, or target-capacity evidence in the execution ledger.

## Plan Self-Review Checklist

- [x] Goal, specification coverage, public interfaces, dependencies and file responsibilities are retained.
- [x] Implementation tasks contain production work, affected builds and build evidence only.
- [x] Behavioral acceptance is explicitly deferred until all Phase 1-12 production code is complete.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.
