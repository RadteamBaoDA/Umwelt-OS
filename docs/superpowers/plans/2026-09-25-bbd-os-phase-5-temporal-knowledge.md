# BBD-OS Phase 5 — Temporal Knowledge and Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-bbd-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Track events and time-varying facts through Graphiti with inspectable provenance, synchronization and a useful Timeline.

**Architecture:** Implement the timeline, knowledge/temporal capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 4 entities/evidence; Phase 3 permitted chat/structured/embedding capabilities supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query; phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 7, 13 Event, 34, 48, 95, 138.12–14, 158, 161–162. The [master plan](2026-09-25-bbd-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 4 entities/evidence; Phase 3 permitted chat/structured/embedding capabilities.

**Implementation status:** Production behavior T1–T4 is integrated in the unified phase worktree and independently source-reviewed. Whole-phase review closed the ready-event input-limit regression and lifecycle record correction; scoped frontend review closed build repair/token/OSS findings. Frozen 54-file prescribed builds pass Next.js compilation, integrated TypeScript and static generation, then fail because Docker Desktop cannot start. Final source freeze and staged scope receipts are in EXECUTION.md. Full four-image build, task completion receipts and squash into develop remain pending; runtime acceptance is deferred.

Implementation stage: production code and affected production builds only. Do not create, modify, or run tests, fixtures, lint, or standalone typecheck. Begin the deferred test stage only after production code for Phases 1-12 is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-bbd-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R06 events and R15 consumers**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

1. Occurred time, observation time and fact validity are distinct timestamps.
2. Late events and contradictory facts retain history instead of overwriting evidence.
3. Graph synchronization can retry without duplicate episodes or loss of manual corrections.
4. Graphiti must not bypass OmniRoute privacy or use its own ungoverned provider fallback.
5. The selected graph backend has no presumed 8GB resource guarantee.

## File Structure and Boundaries

Module ownership: **timeline, knowledge/temporal**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.


## Task P05-T1: Graphiti/FalkorDB compatibility and resource gate

**Files and responsibilities:** Create docs/graph-compatibility.md; infrastructure/graph/compose.yml; modules/knowledge/temporal/adapter.py; update pyproject.toml/uv.lock only for validated dependencies.

**Interfaces — consumes/produces:** TemporalGraph.initialize, upsert_episode, search_at, delete_episode, health; adapter accepts ModelGateway policy-aware clients and canonical entity ID mappings. FalkorDB is the first candidate, isolated from the ARQ Redis instance.


- [x] **P05-T1.1 — Implement production behavior.** Pin a compatible Graphiti/FalkorDB pair using official sources, route all Graphiti model calls through the controlled clients. Record unresolved compatibility or resource gates; do not silently swap backend, drop Graphiti or claim mini-host capacity. Never share the queue Redis volume with FalkorDB.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"backend":"falkordb","release_record":"docs/graph-compatibility.md","live_models_verified":false,"target_host_verified":false}
```

- [ ] **P05-T1.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P05-T1.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_graph_compatibility.py`.
- Acceptance behavior: Repeated episode upsert remains idempotent; Graphiti/FalkorDB initialize, search, delete and restart work through policy-aware ModelGateway clients; the graph service uses a Redis instance isolated from ARQ. Record actual dependency versions and measured host details; target-hardware capacity remains explicitly unverified when hardware is unavailable. Complete the documented compatibility probe before enabling the runtime profile. The compatibility record must include actual locked versions and observed results; if target hardware is unavailable, keep capacity explicitly unverified.

## Task P05-T2: Events, participants and time semantics

**Files and responsibilities:** Create modules/timeline/models.py, modules/timeline/schemas.py, modules/timeline/public.py, modules/timeline/routes.py, modules/timeline/extraction.py; an owning generated Alembic revision under infrastructure/postgres/migrations/versions/ after checking current and worktree heads

**Interfaces — consumes/produces:** CRUD /events; GET /timeline?date_from=&date_to=&timezone=&source_id=&entity_id=&cursor=; timeline uses occurred time, preserving observed_at and optional valid_from/valid_to. day_window(date,timezone) -> (UTC start,UTC end) with half-open intervals.


- [x] **P05-T2.1 — Implement production behavior.** Persist Event/EventParticipant and evidence references; do not mix operational ingestion logs with personal events. Extract structured events from documents through validated model policy; accept manual events with explicit manual origin. Handle unknown dates without inventing a timestamp, and distinguish date-only from timed events. Sort timeline by stable (occurred_at,id), UTC storage plus IANA timezone at query/display, including DST zones. Manual corrections use expected revision and survive reprocessing.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"type":"manual_event","started_at":null,"observed_at":"2026-09-25T03:00:00Z","date_precision":"unknown","origin":"manual","evidence":[]}
```

- [x] **P05-T2.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [x] **P05-T2.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/test_time_windows.py`, `tests/integration/test_timeline.py`.
- Acceptance behavior: Vietnam local-day boundaries map to a half-open UTC interval; cover DST zones, same-time cursor pagination, late arrivals, unknown dates, source filters, manual corrections, invalid ranges, evidence provenance, and the distinction between date-only and timed events. Derived events must cite valid evidence, and manual events must record owner provenance.

## Task P05-T3: Durable graph sync and knowledge changes

**Files and responsibilities:** Create modules/knowledge/temporal/models.py, modules/knowledge/temporal/public.py, modules/knowledge/temporal/worker.py; extend data deletion and ingestion stage handlers.

**Interfaces — consumes/produces:** GraphSync(document_version_id,canonical_entity_ids,episode_id,status); KnowledgeService.get_entity_timeline, get_timeline, get_events, find_changes. POST /system/graph/reconcile -> run_id.


- [x] **P05-T3.1 — Implement production behavior.** PostgreSQL owns canonical IDs, corrections and sync status; Graphiti owns derived temporal representation. Store stable episode mappings, tombstones and source policy on each sync request. Reconciliation repairs missed writes and applies evidence removal without deleting facts still supported elsewhere. Emit KnowledgeChanged after canonical changes; UI can show graph pending/failed without hiding the underlying record.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"document_version_id":"uuid","episode_id":"stable-id","status":"pending","last_error":null}
```

- [ ] **P05-T3.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P05-T3.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_graph_sync.py`.
- Acceptance behavior: A queued graph sync replay after source deletion cannot resurrect a visible episode. Cover database commit followed by graph timeout, restart, duplicate submission, correction replay, shared evidence removal, explicit graph outage, and bounded reconciliation without a global rebuild at startup.

## Task P05-T4: Timeline and historical entity UI

**Files and responsibilities:** Create apps/web/src/modules/timeline/api.ts, apps/web/src/modules/timeline/timeline-page.tsx, apps/web/src/modules/timeline/event-detail.tsx; extend knowledge entity-detail.tsx and relationship-graph.tsx; route /timeline

**Interfaces — consumes/produces:** Timeline displays occurred/observed/validity labels separately. Graph date filtering uses validity windows; historical truth is not implied by the dashboard date filter.


- [x] **P05-T4.1 — Implement production behavior.** Provide source/type/entity/date filters, pagination, event details and links to evidence/entity pages; render local time with explicit timezone. Display late-arrival and graph-sync status truthfully. Add entity history tabs only when their APIs exist and accessible relationship history controls.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"date_from":"2026-09-25","date_to":"2026-09-26","timezone":"Asia/Ho_Chi_Minh"}
```

- [ ] **P05-T4.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P05-T4.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/e2e/timeline.spec.ts`.
- Acceptance behavior: Timeline date selection survives reload. Cover mixed sources, event correction, evidence links, unavailable graph state, date filters, and separate occurred, observed, and validity times. Carry any unresolved target-hardware capacity gate forward in the execution ledger.

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
