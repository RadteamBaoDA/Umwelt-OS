# Umwelt-OS Phase 8 — Today, Daily Chat Drawer, Tasks and Goals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Deliver the daily dashboard with an on-demand contextual chat drawer, actionable tasks/goals, ranked news and saved briefs.

**Architecture:** Implement the dashboard, tasks, goals, news, notifications capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phases 1–7 public knowledge, chat, events and tools; source collection already runs in Phase 2 supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query; phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 38, 44–47, 56–59, 66, 95, 115, 138.1/7–9/19–21, 144; owner daily-history/drawer decisions. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phases 1–7 public knowledge, chat, events and tools; source collection already runs in Phase 2.

**Implementation status:** Not started. This file is an implementation plan, not evidence of working code.

Implementation stage: production code and affected production builds only. Do not create, modify, or run tests, fixtures, lint, or standalone typecheck. Begin the deferred test stage only after production code for Phases 1-12 is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R09–R12**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

1. Changing dashboard date cannot silently retarget an existing message or running response.
2. A saved historical brief is distinct from lists refreshed using current data.
3. Tasks crossing local midnight and date-only deadlines must render under the selected timezone.
4. Repeated brief/notification jobs cannot spam duplicates or silently replace historical revisions.
5. Stories from one source repeated many times must not fabricate cross-source importance.

## File Structure and Boundaries

Module ownership: **dashboard, tasks, goals, news, notifications**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.


## Task P08-T1: Tasks, goals, topics and approved planning

**Files and responsibilities:** Create modules/tasks/models.py, modules/tasks/schemas.py, modules/tasks/public.py, modules/tasks/routes.py, modules/tasks/tools.py; modules/goals/models.py, modules/goals/schemas.py, modules/goals/public.py, modules/goals/routes.py, modules/goals/tools.py; modules/news/topics.py; an owning generated Alembic revision under infrastructure/postgres/migrations/versions/ after checking current and worktree heads; frontend task/goal/topic modules

**Interfaces — consumes/produces:** CRUD /tasks,/goals,/topics; goals own milestones and linked task/entity references. Task statuses inbox,todo,in_progress,blocked,done,cancelled. POST /goals/{id}/accept-plan atomically materializes an owner-accepted proposal once.


- [ ] **P08-T1.1 — Implement production behavior.** Define goal proposals through the public proposal schema. Implement Inbox/Today/Upcoming/Blocked/Completed and All, due dates with date-only versus instant semantics, completion times and optimistic revisions. Goals contain desired outcome/deadline/progress/milestones; progress from linked completed milestones unless owner explicitly selects manual tracking. Register tools with existing registry; never create proposed tasks before acceptance. Extend global search with tasks/goals and fictional seed.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"title":"Prepare release","status":"todo","due_date":"2026-09-25","due_at":null,"goal_id":"uuid"}
```

- [ ] **P08-T1.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P08-T1.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_tasks_goals.py`.
- Acceptance behavior: Accepting the same goal plan twice produces the same task IDs without duplicate tasks. Cover timezone/DST, completion/reopen, stale revisions, deleted linked entities, replay, and ensure unaccepted proposals create no tasks.

## Task P08-T2: Story clustering, trends and explainable relevance

**Files and responsibilities:** Create modules/news/models.py, modules/news/stories.py, modules/news/trends.py, modules/news/relevance.py, modules/news/public.py, modules/news/routes.py; frontend apps/web/src/modules/news/story-list.tsx, apps/web/src/modules/news/story-detail.tsx, apps/web/src/modules/news/topic-settings.tsx.

**Interfaces — consumes/produces:** GET /stories,/trends; score_relevance(signals) -> {score,why_relevant}; cluster candidates preserve article IDs/evidence. Signals normalized [0,1]: topic,entity,goal,project,recency,importance,novelty. Default equal weights; editable owner interests.


- [ ] **P08-T2.1 — Implement production behavior.** Use deterministic canonical URL/hash groups first, then bounded embedding/entity/time similarity when embeddings exist. Candidate window default 72h; uncertain matches remain separate. Preserve per-source observations, choose representative evidence, and label auto-generated summaries. Trends compare current 24h activity with preceding 7-day per-day baseline and require at least two distinct sources plus three items before a rising alert; low baseline is flagged rather than infinite growth. Scores derive from recorded signals, no LLM-invented numeric confidence. Keep lexical-only ranking when AI unavailable.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"story_id":"uuid","trend":"rising","source_count":3,"evidence_ids":["uuid"],"why_relevant":["topic","goal"]}
```

- [ ] **P08-T2.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P08-T2.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/test_news_ranking.py`.
- Acceptance behavior: Relevance results explain each positive recorded signal. Cover repeated same-source articles, unrelated same-name entities, low baselines, stale stories, evidence removal, and no-model operation.

## Task P08-T3: Daily context, revisioned brief and notifications

**Files and responsibilities:** Create modules/dashboard/context.py, modules/dashboard/briefs.py, modules/dashboard/models.py, modules/dashboard/routes.py, modules/dashboard/worker.py; modules/notifications/models.py, modules/notifications/public.py, modules/notifications/routes.py; extend ARQ internal schedules and module widget descriptors.

**Interfaces — consumes/produces:** GET /context/current; GET /context/daily?date=YYYY-MM-DD&timezone=IANA; GET /briefs?date=&timezone=; POST /briefs/generate; GET/PATCH /notifications. DailyContext returns selected_date,timezone,generated_at,brief,widgets; briefs revisioned by date/timezone/input fingerprint.


- [ ] **P08-T3.1 — Implement production behavior.** Use real repositories and the configured ModelGateway client. Default brief schedule 07:00 Asia/Ho_Chi_Minh, editable; ARQ owns this internal schedule. During startup catch up once for current day if missing, not every missed historical day. Build context from public module providers, rank, generate cited brief and retain each revision. Historical brief stays saved; widgets use current records filtered to the selected date and show updated_at. Past-day open tasks are not claimed as historical task-state snapshots. Future dates show planned commitments without fabricated generated news. Notifications have dedupe keys and read state; emit only meaningful actionable changes.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"selected_date":"2026-09-25","timezone":"Asia/Ho_Chi_Minh","brief_revision":1,"widgets_updated_at":"2026-09-25T09:00:00Z","history_mode":"saved_brief_current_records"}
```

- [ ] **P08-T3.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P08-T3.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_daily_context.py`.
- Acceptance behavior: Regenerating a daily brief creates a new revision while preserving the earlier saved revision. Cover concurrent generation, model outage preserving the last brief, notification deduplication, date-only tasks, timezone changes, missed schedules, and source deletion invalidating citations/marking briefs stale.

## Task P08-T4: Configurable gadget dashboard and contextual chat UX

**Files and responsibilities:** Create apps/web/src/modules/dashboard/dashboard-page.tsx, apps/web/src/modules/dashboard/date-selector.tsx, apps/web/src/modules/dashboard/widget-registry.tsx, apps/web/src/modules/dashboard/daily-brief.tsx; notifications frontend; modify authenticated root routing and chat drawer controller; docs/ux-proposals/2026-09-26-life-dashboard-proposal.md.

**Interfaces — consumes/produces:** GET /context/daily drives widgets. ChatContext {kind:'day',date,timezone}; a chosen day conversation stores that immutable context. Drawer inherits the Phase 6 component; no permanent chat column. Widgets register id,module,title,priority,provider,refresh_policy,permissions.


- [ ] **P08-T4.1 — Implement production behavior.** Dashboard becomes authenticated root with multiple named dashboards/groups/presets. Implement R10–R12: every brief/task/goal/event/story/trend/map/highlight/activity is a movable and resizable gadget. Date navigation belongs to relevant gadgets/context, not a fixed Today layout. No calendar connector means only available manual/imported events, with explicit source status. Desktop chat is right overlay; mobile full-screen Sheet; default closed. History, citations and approvals belong to full Chat; drawer contains only New chat, messages, composer/send-stop and close. Selecting another day switches the drawer's selected conversation to that day without modifying an old conversation or cancelling its background run; in-flight output remains associated with its original response. Opening full Chat preserves conversation ID and date badge. Closing restores focus/draft and the main viewport remains full-width.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"conversationId":"uuid","context":{"kind":"day","date":"2026-09-25","timezone":"Asia/Ho_Chi_Minh"}}
```

- [ ] **P08-T4.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P08-T4.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/e2e/today-drawer.spec.ts`.
- Acceptance behavior: The daily chat drawer retains its selected date context and releases the layout column when closed. Cover mobile and keyboard interaction, day switching during streaming, cross-midnight, saved-brief/current-widget labels, tasks accepted from chat, duplicate history prevention, and full-width closed state.

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
