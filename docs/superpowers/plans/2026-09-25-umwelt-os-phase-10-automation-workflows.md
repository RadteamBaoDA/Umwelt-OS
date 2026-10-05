# Umwelt-OS Phase 10 — Automation Rules and Workflow Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Let the owner configure bounded scheduled/event-driven rules without duplicating connector schedule ownership or inventing a workflow language.

**Architecture:** Implement the automations capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 2 durable events/n8n; Phase 7 approvals; Phase 8 tasks/brief/notifications supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query, phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 10, 60–61, 95, 138.18, 158–159. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 2 durable events/n8n; Phase 7 approvals; Phase 8 tasks/brief/notifications.

**Implementation status:** Not started. This file is an implementation plan, not evidence of working code.

Code stage: implement production code and run affected production builds only. Do not create or modify test files, or run tests, lint, standalone typecheck, audits, or non-build acceptance checks. Begin deferred behavioral acceptance only after all Phase 1-12 production code is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R04/R09 Settings placement**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

1. An automation must not trigger itself forever or fan out without bounds.
2. Duplicate events and retries must not repeat a completed action.
3. Pausing a rule invalidates queued work without deleting its history.
4. Preview must not send a webhook, create a task or spend model tokens.
5. External actions retain approval and uncertain-outcome semantics even when scheduled.

## File Structure and Boundaries

Module ownership: **automations**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.

## Task P10-T1: Rule schema, deterministic conditions and dry preview

**Files and responsibilities:** Create modules/automations/models.py, modules/automations/schemas.py, modules/automations/conditions.py, modules/automations/public.py, modules/automations/routes.py; an owning generated Alembic revision under infrastructure/postgres/migrations/versions/ after checking current and worktree heads

**Interfaces — consumes/produces:** CRUD /automations; POST /automations/preview. Automation(trigger,conditions,actions,enabled,revision). Conditions limited to whitelisted field/operator/value comparisons; no eval, scripts or arbitrary expressions. Preview returns matched/reasons/planned_actions only.


- [ ] **P10-T1.3 — Implement the minimal production behavior.** Use Pydantic discriminated unions for supported triggers schedule,new_event,new_document,entity_changed,task_due,goal_deadline,webhook,connector_sync_result and actions run_agent/create_task/create_notification/generate_brief/call_webhook. Fields must be declared in the selected trigger schema; supported operators eq,ne,in,gt,gte,lt,lte only. Store immutable rule revisions for runs; validate module dependencies and allowlist webhook targets. Preview reads supplied/current authorized metadata without queueing work or calling models.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"trigger":{"type":"new_document"},"conditions":[{"field":"source_id","operator":"eq","value":"uuid"}],"actions":[{"type":"create_notification","message":"New source document"}]}
```

- [ ] **P10-T1.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P10-T1.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P10-T2: Dispatch, scheduling and durable execution

**Files and responsibilities:** Create modules/automations/scheduler.py, modules/automations/worker.py, modules/automations/execution.py; extend ARQ and n8n adapter bindings.

**Interfaces — consumes/produces:** POST /automations/{id}/run; GET /automations/{id}/runs; execution identity=(rule_id,revision,trigger_event_id or scheduled_slot). Scheduled rules have explicit timezone, next slot and misfire policy.


- [ ] **P10-T2.3 — Implement the minimal production behavior.** Use the Phase 2 durable event/outbox and ARQ mechanisms. n8n remains sole owner of collection schedules; internal rule schedules use ARQ/DB slots and never recreate source polling. Default missed internal slot policy coalesces to one catch-up run; no unbounded replay. Add causation IDs, max chain depth 5, max 10 actions per run, per-rule cooldown default 60s and concurrency 1. Check current enabled/revision/grants before dispatch; persist each action outcome. External webhook actions use the Phase 7 approval/effect ledger and shared SSRF safeguards.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"rule_id":"uuid","revision":1,"trigger_id":"event-1","causation_depth":1,"status":"queued"}
```

- [ ] **P10-T2.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P10-T2.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P10-T3: Automation Agent and configuration UI

**Files and responsibilities:** Create apps/web/src/modules/automations/api.ts, apps/web/src/modules/automations/rule-editor.tsx, apps/web/src/modules/automations/rule-list.tsx, apps/web/src/modules/automations/run-detail.tsx; advanced automation UI in the relevant Settings group, not main navigation; extend modules/agents/specialists.py

**Interfaces — consumes/produces:** Automation Agent creates proposals using registered schemas; owner accept creates a disabled draft rule, separate explicit enable starts it. UI offers Trigger/Conditions/Actions with preview, enable/disable, run history and manual Run now.


- [ ] **P10-T3.3 — Implement the minimal production behavior.** Use schema-backed ordinary forms rather than a node-canvas or custom DSL. Show estimated scope and exact external targets, validation errors, preview reasons and action outcomes. Allow opening related approval on full Chat/run detail. Clear disabled/unavailable actions when their owning module is disabled. Keep source schedule editing in Settings/Data sources through Umwelt-OS adapters.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"proposal_id":"uuid","accepted":true,"rule_enabled":false}
```

- [ ] **P10-T3.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P10-T3.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Task P10-T4: Operational rule pack and sample documentation

**Files and responsibilities:** Extend the existing seed module with disabled fictional examples; create docs/automations.md; update implementation status.

**Interfaces — consumes/produces:** Optional fictional examples: new source document -> notification; due task -> notification; scheduled brief -> existing generate-brief action; owner-approved run_agent. Examples install disabled.


- [ ] **P10-T4.3 — Implement the minimal production behavior.** Document schedule ownership, missed-run behavior, permission model, action limits and recovery. Ensure sample rules never duplicate the existing Phase 8 brief schedule when enabled: selecting an automation-owned brief slot transfers ownership explicitly in the schedule record. Retain one scheduler owner per logical job.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"logical_job":"daily_brief","schedule_owner":"internal_brief","automation_id":null}
```

- [ ] **P10-T4.4 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix production build failures before proceeding.

- [ ] **P10-T4.5 - Record build evidence and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; then continue.

## Phase Build and Handoff

- [ ] Run the affected production build with `./scripts/dev.ps1 build` (or `make build`) and resolve failures.
- [ ] Record changed production files, exact build command/result, review findings, unresolved gates, and phase status in `EXECUTION.md`.
- [ ] Continue to the next ready production task. Do not start deferred acceptance before all Phase 1-12 production code is complete.

## Deferred test-stage acceptance (not implementation tasks)

Run behavioral, integration, UI, live-service, recovery, and capacity checks only after all Phase 1-12 production code is complete. Do not create or modify test files during the code stage.
- **P10-T1:** Invalid operators/fields, unknown action, missing module, preview side-effect count zero, revision conflicts and read/write authorization.
- **P10-T2:** Duplicate event, loop A-to-B-to-A, worker restart, pause between enqueue/execute, bounded catch-up, timezone/DST slots, 429 and uncertain webhook timeout.
- **P10-T3:** Keyboard authoring, preview, proposal acceptance, enable/disable, protected Run now, pending approval, history and failed action retry.
- **P10-T4:** Full source->event->rule->notification flow, due-task reminder, agent proposal approval, no duplicate brief and disabled sample no-op. Common gate; next P11-T1.

- Validate the review-focus risks listed above, production packaging/OpenAPI/module registration, and live or hardware-dependent requirements from the specification.
- Record deferred test-stage results and blocked live, restore, or target-capacity evidence in the execution ledger.

## Plan Self-Review Checklist

- [x] Goal, specification coverage, public interfaces, dependencies and file responsibilities are retained.
- [x] Implementation tasks contain production work, affected builds and build evidence only.
- [x] Behavioral acceptance is explicitly deferred until all Phase 1-12 production code is complete.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.
