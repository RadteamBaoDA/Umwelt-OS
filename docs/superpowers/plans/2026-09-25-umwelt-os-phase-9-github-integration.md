# Umwelt-OS Phase 9 — GitHub Collection and Project Knowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Collect repositories, issues, pull requests, commits and releases through packaged workflows and expose their evidence in knowledge, Timeline and Today.

**Architecture:** Implement the connectors/github capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 2 connector contract; Phases 4–8 entity/event/project presentation supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query, phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 53, 95, 115, 138.5, 159. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 2 connector contract; Phases 4–8 entity/event/project presentation.

**Implementation status:** Not started. This file is an implementation plan, not evidence of working code.

Code stage: implement production code and run affected production builds only. Do not create or modify test files, or run tests, lint, standalone typecheck, audits, or non-build acceptance checks. Begin deferred behavioral acceptance only after all Phase 1-12 production code is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R13–R15**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

1. Bootstrap must visit all provider pages without skipping records that update mid-sync.
2. A webhook can be duplicated, out of order or forged.
3. Provider IDs survive repository rename and are not replaced by mutable URLs.
4. Rate limiting is different from authentication failure and must not cause tight retry loops.
5. Unsupported deletion discovery is shown honestly rather than silently retaining stale certainty.

## File Structure and Boundaries

Module ownership: **connectors/github**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.

## Task P09-T1: Versioned GitHub workflows and connection setup

**Files and responsibilities:** Create modules/connectors/github/adapter.py, modules/connectors/github/schemas.py, modules/connectors/github/normalization.py; infrastructure/n8n/workflows/github.json; docs/connectors/github.md; extend source setup UI.

**Interfaces — consumes/produces:** GitHubSourceConfig(repository,include_issues,include_pulls,include_commits,include_releases); provider credentials remain n8n-owned references. Connector registry exposes validate/sync/normalize/health without GitHub-specific imports in ingestion.

**Mandatory carried production slice R03-OAuth — 2026-09-30:** Implement server start/callback/refresh/revoke for the concrete GitHub connection using a supported expiring/refresh grant mode, consumed by the actual read-only collection adapter. Keep Google sign-in permission separate. State must be browser-bound/single-use/expiring with maintained provider validation; provider endpoints/scopes are allowlisted, credentials protected and redacted, and stale callbacks/refresh cannot overwrite newer configuration or resurrect disabled sources. Revoke/disconnect must fence collection immediately; require explicit provider registration/configuration and show unavailable states honestly. Credential storage follows the R03 native/n8n ownership contracts. Record provider API/dependency evidence and close R03-OAuth in EXECUTION before full R03 or the all-code milestone can be marked complete. No generic unused OAuth framework and no tests during production implementation.



- [ ] **P09-T1.3 — Implement the minimal production behavior.** Map stable numeric/node IDs to canonical external_id; document repository scope and permissions. Package real n8n export with credential references, source binding, batch receipt and response checking. Expose read-only collection only; no issue/comment writes implied. Validate selected repo and enabled resource types; display actual last successful validation.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"provider":"github","repository":"owner/repo","include_issues":true,"include_pulls":true,"include_commits":true,"include_releases":true}
```

- [ ] **P09-T1.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P09-T1.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P09-T2: Pagination, incremental sync and verified webhooks

**Files and responsibilities:** Create modules/connectors/github/sync.py, modules/connectors/github/webhooks.py; extend protected connector webhook routes and n8n template.

**Interfaces — consumes/produces:** POST /connectors/github/webhook verifies delivery signature and unique delivery ID; per-resource cursor persisted by Umwelt-OS only after durable batch acknowledgment. Source runs share the Phase 2 lease.


- [ ] **P09-T2.3 — Implement the minimal production behavior.** Implement webhook receipt persistence and signature validation in production code. Follow provider pagination, use bounded history plus updated-time overlap, dedupe by stable identity/version and prevent stale cursors replacing newer ones. Verify signatures over original bytes with constant-time comparison before parsing/trusting events; reject unsupported oversized events. Honor Retry-After/reset times, bound retries, and distinguish invalid permissions. Document which deletion/visibility changes can be discovered by webhook or reconciliation and mark unobservable records stale/unverified when access disappears.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"delivery_id":"provider-id","source_id":"uuid","resource":"issue","provider_id":"123","updated_at":"2026-09-25T03:00:00Z"}
```

- [ ] **P09-T2.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P09-T2.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P09-T3: Canonical mapping and incremental knowledge updates

**Files and responsibilities:** Extend modules/connectors/github/normalization.py and registry mappings; add project relationships via public entity/event APIs.

**Interfaces — consumes/produces:** Normalized GitHub documents retain source URL/provider identity/version. Repository is an entity; commits/issues/pulls/releases contribute typed events and relationships through public contracts, not direct table writes.


- [ ] **P09-T3.3 — Implement the minimal production behavior.** Implement receipt-backed collection through the production pipeline. Separate issue and pull-request normalization to avoid duplicate issue-like records; preserve commit SHA and release identifiers. Update entities/events only for changed versions; project relationships keep evidence. Deletions invalidate derived content through the common deletion workflow. Register github read tools for Project Agent using canonical services.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"external_id":"github:issue:123","canonical_url":"https://github.com/owner/repo/issues/1","event_type":"github_issue","repository_entity_id":"uuid"}
```

- [ ] **P09-T3.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P09-T3.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P09-T4: GitHub UI and project presentation

**Files and responsibilities:** Extend apps/web/src/modules/sources/ connector setup and sync history; project widgets in apps/web/src/modules/dashboard; docs/model-compatibility.md only when model use is exercised.

**Interfaces — consumes/produces:** Sources renders GitHub scope, run state, fetched/indexed timestamps and separate errors. Project views and drawer context use existing entity/source IDs.


- [ ] **P09-T4.3 — Implement the minimal production behavior.** Configure credential/repository/scope/schedule inside Umwelt-OS Settings/Data sources using R03/R04 supported APIs. Do not require n8n administration UI; surface unsupported provisioning. Show live run counts/status and links to provider evidence; surface project changes in configurable dashboard gadgets and Timeline details via existing providers.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"resource_counts":{"repositories":1,"issues":2,"pull_requests":1,"commits":3,"releases":1},"live_verified":false}
```

- [ ] **P09-T4.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P09-T4.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Phase Build and Handoff

- [ ] Run the affected production build with `./scripts/dev.ps1 build` (or `make build`) and resolve failures.
- [ ] Record changed production files, exact build command/result, review findings, unresolved gates, and phase status in `EXECUTION.md`.
- [ ] Continue to the next ready production task. Do not start deferred acceptance before all Phase 1-12 production code is complete.

## Deferred test-stage acceptance (not implementation tasks)

Run behavioral, integration, UI, live-service, recovery, and capacity checks only after all Phase 1-12 production code is complete. Do not create or modify test files during the code stage.
- **P09-T1:** Mapping tests for all required resources, repository rename, missing fields, multiline Unicode and private credential redaction. Import the workflow in the pinned n8n and validate against a controlled test repository.
- **P09-T2:** Multiple pages, edits during scan, forged signature, out-of-order events, duplicate deliveries, permission revoke, 429/403 semantics and restart mid-page. No external writes during tests.
- **P09-T3:** Edited/closed/reopened issue, PR changes, repeated commit observations, release edits, repository rename and deleted source evidence. Verify search/entity/timeline indexing reflects edits without duplicates.
- **P09-T4:** Packaged workflow, initial collection, incremental edit, restart, project display and cited Ask about a known repository fact. Common gate; next P10-T1.

- Validate the review-focus risks listed above, production packaging/OpenAPI/module registration, and live or hardware-dependent requirements from the specification.
- Record deferred test-stage results and blocked live, restore, or target-capacity evidence in the execution ledger.

## Plan Self-Review Checklist

- [x] Goal, specification coverage, public interfaces, dependencies and file responsibilities are retained.
- [x] Implementation tasks contain production work, affected builds and build evidence only.
- [x] Behavioral acceptance is explicitly deferred until all Phase 1-12 production code is complete.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.
