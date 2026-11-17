# Umwelt-OS Phase 6 — Ask, Chat Drawer and Selective Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Answer questions with citations and reusable chat in an on-demand drawer that preserves screen space.

**Architecture:** Implement the chat, memory capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 3 retrieval/gateway, Phase 4 entities, Phase 5 temporal public APIs supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query; phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 34–43, 65, 95, 111–114, 138.2/15/24, 157; owner drawer decision 2026-09-25. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 3 retrieval/gateway, Phase 4 entities, Phase 5 temporal public APIs.

**Implementation status:** Not started. This file is an implementation plan, not evidence of working code.

Implementation stage: production code and affected production builds only. Do not create, modify, or run tests, fixtures, lint, or standalone typecheck. Begin the deferred test stage only after production code for Phases 1-12 is complete.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R07; P06-T2 follows R06 recovery**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

1. The drawer never reserves a permanent empty column when closed.
2. Closing the drawer does not lose the draft, conversation or a server-side response.
3. Retrieved instructions cannot override permissions or create fake citation IDs.
4. History storage, memory creation and remote processing are independent privacy choices.
5. Evidence deletion invalidates stored citation access and derived memory retrieval.

## File Structure and Boundaries

Module ownership: **chat, memory**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.


## Task P06-T1: Grounded retrieval and citation validation

**Files and responsibilities:** Create modules/chat/retrieval.py, modules/chat/citations.py, modules/chat/public.py, modules/chat/schemas.py; modules/knowledge/public.py extensions

**Interfaces — consumes/produces:** AnswerContext(query,source_scope,entity_ids,date_context?,policy); Citation(sourceType,sourceId,documentId,documentVersionId,chunkId,title,url,observedAt,quote). build_context returns bounded permitted evidence; validate_citations(answer,evidence) rejects references not in the retrieved set.


- [ ] **P06-T1.1 — Implement production behavior.** Retrieve lexical/hybrid + relevant temporal/entity context via public APIs; deduplicate and fit a context budget before model calls. Add a configured permitted reranker where available; if unavailable preserve retrieval ranking and label rerank unavailable rather than invent scores. Require exact evidence IDs/quotes from available revisions; answers without sufficient evidence explicitly say so. Treat documents/web text as untrusted data and never execute embedded instructions.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"answer":"Supported answer","citations":[{"sourceType":"document","sourceId":"uuid","documentId":"uuid","documentVersionId":"uuid","chunkId":"uuid","title":"Note","url":null,"observedAt":"2026-09-25T03:00:00Z","quote":"Evidence"}]}
```

- [ ] **P06-T1.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P06-T1.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/test_citations.py`, `tests/integration/test_ask_retrieval.py`.
- Acceptance behavior: Citations outside the retrieved evidence set are rejected. Cover grounded and insufficient-evidence answers, invented or removed citations, mixed-source privacy, prompt injection, quote bounds, unavailable reranker, and provider failures.

## Task P06-T2: Persistent conversations, responses and replayable stream

**Files and responsibilities:** Create modules/chat/models.py, modules/chat/routes.py, modules/chat/worker.py, modules/chat/stream.py; an owning generated Alembic revision under infrastructure/postgres/migrations/versions/ after checking current and worktree heads

**Interfaces — consumes/produces:** CRUD /conversations; POST /conversations/{id}/messages accepts client_request_id,content,context; returns message_id,response_id. GET /responses/{id}/events uses SSE event IDs; POST /responses/{id}/cancel. Message metadata holds model identity, usage unknown when absent, and citation references.


- [ ] **P06-T2.1 — Implement production behavior.** Keep ModelGateway as the only provider boundary; production routes and workers must not call providers directly. Persist user message and pending response before dispatch. Worker owns generation independently of browser connection; persist bounded stream events and final text so reconnect resumes by event ID. Refresh auth on reconnect; no response text after permission is revoked. Store partial/cancelled/failed states distinctly. With history storage off, retain only active-run state needed for delivery, then purge within 24h or earlier explicit delete; never turn it into memory automatically.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"event":"message.delta","id":"response-id:12","data":{"text":"partial"}}
```

- [ ] **P06-T2.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P06-T2.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_conversations.py`, `tests/test_stream_events.py`.
- Acceptance behavior: Repeated send with one client request ID returns the same response. Cover disconnect/reconnect by event ID, worker crash, cancellation, token expiry, history opt-out cleanup, deletion while streaming, and no storage or display of raw chain-of-thought.

## Task P06-T3: Reusable accessible chat drawer and full Chat route

**Files and responsibilities:** Create apps/web/src/modules/chat/api.ts, apps/web/src/modules/chat/chat-drawer.tsx, apps/web/src/modules/chat/chat-session.tsx, apps/web/src/modules/chat/chat-transcript.tsx, apps/web/src/modules/chat/chat-composer.tsx, apps/web/src/modules/chat/chat-history.tsx, apps/web/src/modules/chat/citation-panel.tsx; apps/web/src/core/app-shell/chat-controller.tsx; route /chat

**Interfaces — consumes/produces:** ChatDrawer.open({conversationId?,context?}); ChatContext {kind:'general'|'document'|'entity'|'day'|'selection',resource_id?,date?,timezone?}. Same ChatSession component powers /chat and the drawer; one server conversation per chosen ID, no duplicate transcript store.


- [ ] **P06-T3.1 — Implement production behavior.** Use the existing UI ecosystem's accessible Sheet/Dialog primitive, not a custom focus trap. Right-side overlay drawer, closed by default, large width responsive to viewport, full viewport on mobile; underlying page gets full width when closed. Escape/close restores focus and closes presentation only, not the server run. Stop is a separate explicit action. Keep draft in local in-memory UI state, not persistent browser storage of private content; server transcript remains TanStack Query state. Drawer contains only New chat, messages, composer/send-stop and close. Full Chat owns history/context/citations/activity/approvals/web search and shares conversation/context/draft. Port AnythingLLM source through R07; a new visual imitation is not completion.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"conversationId":"uuid","context":{"kind":"document","resource_id":"uuid"}}
```

- [ ] **P06-T3.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P06-T3.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/e2e/chat-drawer.spec.ts`.
- Acceptance behavior: Closing and reopening the drawer returns focus and retains the draft. Cover keyboard access, focus handling, mobile viewport, overflow, closing during a stream, transcript/citation views, and opening full Chat with the same conversation.

## Task P06-T4: Selective memory and privacy management

**Files and responsibilities:** Create modules/memory/models.py, modules/memory/schemas.py, modules/memory/public.py, modules/memory/routes.py, modules/memory/selection.py; apps/web/src/modules/knowledge/memory-list.tsx; apps/web/src/modules/settings/memory-privacy.tsx; extend deletion/export hooks.

**Interfaces — consumes/produces:** CRUD /memories; POST /memories/{id}/invalidate, /supersede, /forget; MemoryCandidate(content,type,provenance,confidence,reason). Lifecycle and memory policy are independent of conversation-history policy.


- [ ] **P06-T4.1 — Implement production behavior.** Provide an explicit owner create operation. Evaluate novelty/usefulness/confidence for suggestions and record why selected; default automatic permanent memory off until owner enables it. Differentiate manual facts from model-derived candidates. Forget marks content unavailable immediately and removes vector/graph/cache copies through durable deletion; purge stored candidate payloads containing removed evidence. Update KnowledgeService.get_memories and reusable context methods.

Concrete contract/configuration shape (illustrative values, not production defaults):

```json
{"store_conversation_history":true,"store_agent_memory":false,"auto_accept_memory":false}
```

- [ ] **P06-T4.2 - Build the affected production deliverable.** Run `./scripts/dev.ps1 build` (or `make build`); fix build failures before proceeding.

- [ ] **P06-T4.3 - Record build evidence, commit, and continue.** Record changed files, exact build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task, then continue to the next ready task.


### Deferred test-stage acceptance (non-executable)

- Planned coverage artifacts: `tests/integration/test_memory.py`.
- Acceptance behavior: Forgetting a memory makes it unavailable to retrieval immediately. Cover opt-out, source-linked forget, candidate deduplication, superseded-fact exclusion, and explicit owner-created memory.

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
