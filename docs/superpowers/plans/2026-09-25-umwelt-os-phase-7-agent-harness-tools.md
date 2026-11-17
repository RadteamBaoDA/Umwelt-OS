# Umwelt-OS Phase 7 — Agent Harness, Tools, MCP and Approvals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Run bounded specialist agents using shared LangGraph orchestration, registered tools and durable approvals.

**Architecture:** Implement the agents, tools capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 6 chat/memory; Phase 3 capabilities; Phase 2 isolated browser runtime supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query; phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 9, 39–40, 62, 87–88, 95, 138.16–17, 140–150, 158, 160. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 6 chat/memory; Phase 3 capabilities; Phase 2 isolated browser runtime.

**Implementation status:** Not started. This file is an implementation plan, not evidence of working code.

Implementation stage: production code and affected production builds only. Do not create, modify, or run tests, fixtures, lint, or standalone typecheck. Begin the deferred test stage only after production code for Phases 1-12 is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R08**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

Main navigation is Dashboard/Chat/Settings; Settings has three groups; account/appearance/language live in the user menu. Detail routes do not become main-navigation items. Migration names below are historical planning examples: inspect current head and active worktrees, including unfinished Phase 4 0007_entities.py, and allocate a unique linked revision without rewriting shipped migrations.

## Global Constraints

- "Never hardcode secrets."
- "Every schema change must use Alembic."
- "Agents must use defined tools and APIs."
- "Create directories only when used."
- Public APIs use `/api/v1`; preserve single-owner auth, session-bound CSRF, source policy and provenance.
- Target 2 CPU cores/8 GiB with remote inference; never claim measured capacity from a larger host.
- Follow the master's mandatory privacy, module, durable-job, deletion and UI contracts. Keep source data and credentials out of logs.
- Commit each completed task after its affected production build and record; do not push, deploy, change branches, or perform destructive owner-data operations as part of this plan.

## Review Focus

1. A model-requested tool is not authorization to execute it.
2. An approval is bound to immutable arguments and expires; replay must not repeat effects.
3. A checkpoint resumes nodes, so external side effects need their own idempotency/reconciliation.
4. Cancellation/module disablement must be checked before every next tool.
5. MCP output and browser content remain untrusted regardless of server registration.

## File Structure and Boundaries

Module ownership: **agents, tools**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.


## Task P07-T1: Tool registry, permissions and MCP adapters

**Files and responsibilities:** Create core/tools/schemas.py, core/tools/registry.py, core/tools/policy.py; modules/tools/mcp.py, modules/tools/routes.py

**Interfaces — consumes/produces:** ToolDefinition(name,version,input_schema,output_schema,risk,confirmation,timeout,permissions,module); invoke_tool(actor,definition,args,grant) -> ToolResult. READ_ONLY automatic; INTERNAL_WRITE configurable; EXTERNAL_WRITE/DESTRUCTIVE require approval. MCP server config is explicit owner-managed allowlist.


- [ ] **P07-T1.1 — Implement production behavior.** Register public Knowledge/Search/Source tools from enabled modules with schemas, not hardcoded tool lists in the supervisor. Validate input and output, resource scope, module state, egress policy and timeout at dispatch. Use maintained MCP SDK; deny arbitrary stdio command execution from model input, pin allowed server configurations, and enforce grants on MCP calls like native tools. Only expose implemented tools; tasks/goals register in Phase 8.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"name":"knowledge.get_document","version":"1","risk":"READ_ONLY","timeout_seconds":10,"module":"knowledge"}
```

- [ ] **P07-T1.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P07-T1.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/test_tool_policy.py`, `tests/integration/test_mcp_tools.py`.
- Acceptance behavior: External-write and destructive actions require approval even when internal writes are configured for automatic execution; read-only actions do not. Cover malformed arguments, output-schema mismatch, disabled modules, missing grants, hostile MCP content, timeout, duplicate tool names, and version compatibility. Also cover duplicate registry names and version compatibility.

## Task P07-T2: LangGraph execution, checkpointing and run limits

**Files and responsibilities:** Create modules/agents/models.py, modules/agents/schemas.py, modules/agents/harness.py, modules/agents/worker.py, modules/agents/public.py, modules/agents/routes.py; an owning generated Alembic revision under infrastructure/postgres/migrations/versions/ after checking current and worktree heads; extend chat response/activity events.

**Interfaces — consumes/produces:** POST /agents/{id}/runs -> run_id; GET /agent-runs/{id}; POST /agent-runs/{id}/cancel; states queued,running,waiting_approval,succeeded,failed,cancelled. Limits default 20 steps,10 tool calls,300s active execution, configurable token budget when usage exists.


- [ ] **P07-T2.1 — Implement production behavior.** Use PostgreSQL checkpoints, shared ModelGateway and registered tool boundaries with controlled worker scheduling. Use LangGraph PostgreSQL checkpointer; integrate shared ModelGateway budget/concurrency/permissions. Check cancellation and limits before each model/tool. Save outputs/evidence, not hidden reasoning. Waiting approval releases the ARQ job; separate resume job revalidates permissions. Version prompts/workflows; incompatible old runs are explicitly migrated or cancelled, never silently run with new semantics.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"status":"waiting_approval","steps":3,"tool_calls":1,"active_seconds":12,"token_usage":null}
```

- [ ] **P07-T2.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P07-T2.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_agent_runs.py`.
- Acceptance behavior: Cancelling a run before the next tool call prevents that call. Cover checkpoint restart, transient provider failures, hard step/time limits, duplicate worker delivery, cancellation before and after tool invocation, and absent usage treated as unknown.

## Task P07-T3: Immutable approvals and effect reconciliation

**Files and responsibilities:** Create modules/agents/approvals.py, modules/agents/effects.py; frontend apps/web/src/modules/agents/approval-card.tsx; chat activity integration.

**Interfaces — consumes/produces:** POST /approvals/{id}/approve or /deny; approval stores tool version,normalized argument hash,action payload,expires_at,resolved_at. Effect ledger stores action_id,provider_key,state,result_reference; uncertain outcome state is requires_review.


- [ ] **P07-T3.1 — Implement production behavior.** Use the approval/effect repositories and provider idempotency or reconciliation for external effects. Display exact action/target/arguments before decision; use one atomic pending-to-approved transition and configurable expiry default 24h. Never pause after an irreversible side effect inside a replayed node without recording its outcome. Use provider idempotency keys or reconciliation; timeout with unknown outcome becomes requires_review rather than automatic retry. Destructive tool actions cannot be auto-approved by retrieved text or ordinary source permissions.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"risk":"EXTERNAL_WRITE","status":"pending","tool":"webhook.send","arguments":{"target":"configured-hook"},"expires_at":"2026-09-26T03:00:00Z"}
```

- [ ] **P07-T3.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P07-T3.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_approvals.py`.
- Acceptance behavior: A resolved approval can produce its external effect only once. Cover changed arguments, expired or denied approval, duplicate approval/resume, permission revocation while waiting, ambiguous provider timeout, and accessible approval UI.

## Task P07-T4: Specialists, browser-use and management UI

**Files and responsibilities:** Create modules/agents/specialists.py, modules/tools/browser.py; apps/web/src/modules/agents/agent-list.tsx, apps/web/src/modules/agents/agent-settings.tsx, apps/web/src/modules/agents/run-detail.tsx; guarded agent details opened from full Chat/advanced AI settings, not main navigation; docs/agents.md.

**Interfaces — consumes/produces:** Supervisor,Knowledge,Research,Personal,Project,News,Planning share one harness; Automation specialist is activated Phase 10. Browser tool submits bounded jobs to Phase 2 runtime; receives run_id/results through protected API.


- [ ] **P07-T4.1 — Implement production behavior.** Configure specialists by prompts, model aliases and allowed tools; start sequentially. Show unavailable dependent capabilities rather than fabricated task/goal actions before Phase 8. Add browser-use only after actual OmniRoute browser/tool capability probes, with per-job page/action/time/download limits and a separate credential-isolated browser worker. Preserve session state only in protected source-specific storage. Model assignment, prompt revision, permissions, recent runs and failures are editable in Agents UI; full Chat activity shows concise tool status.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"agent":"research","enabled":true,"tools":["knowledge.search","browser.read"],"model_alias":"reasoning-large"}
```

- [ ] **P07-T4.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P07-T4.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/e2e/agents.spec.ts`.
- Acceptance behavior: A run requiring an external action displays an approval request before that action. Cover browser network isolation, collection grant versus external write, exhausted browser budget, provider tool-format compatibility, run history, and full Chat approvals.

## Phase Acceptance and Handoff

- [ ] Build the phase production deliverables with `./scripts/dev.ps1 build` (or `make build`).
- [ ] Record production integration points and changed files in `EXECUTION.md`; build the affected deliverables.
- [ ] Complete independent review, fix actionable findings, and rerun only affected production builds.
Deferred test-stage acceptance is tracked above; execute that stage only after all Phase 1-12 production code is complete.
- [ ] Update `docs/IMPLEMENTATION_STATUS.md`, this checklist and `EXECUTION.md`; commit the phase and continue to the next ready task, carrying unresolved external gates forward.

## Plan Self-Review Checklist

- [x] Goal and spec sections mapped to named tasks and public interfaces.
- [x] Five review-focus risks assigned to the owning tasks and deferred acceptance criteria.
- [x] Production file targets, deferred acceptance criteria, implementation rules and build criteria included.
- [x] Module ownership, auth/privacy, safe deletion and retry/resume boundaries preserved.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.
