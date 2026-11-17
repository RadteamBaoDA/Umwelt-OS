# Umwelt-OS Approved Delivery Master Plan â€” Phases 0â€“12

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for task-by-task execution. Follow the phase files linked below and maintain [EXECUTION.md](EXECUTION.md). The owner approved the product scope and requested continuous progress through ready tasks; do not ask for repeated phase-scope approval.

**Goal:** Deliver the entire approved single-owner Personal Intelligence OS, including configurable gadget dashboards, minimal chat drawer and full Chat, collection, knowledge, agents and operational recovery.

**Architecture:** Python/FastAPI modular monolith; PostgreSQL is authoritative for canonical knowledge and durable work; Redis/ARQ executes bounded jobs. Reuse n8n for external collection scheduling, OmniRoute for permitted model access, LangGraph for agent checkpoints/interrupts, Graphiti for temporal knowledge, Crawlee for collection and browser-use for AI-directed browsing. Next.js composes module-owned screens and an on-demand chat drawer.

**Tech Stack:** The approved stack and Phase 0 lockfiles. Add and pin dependencies when the owning task uses them; capability/backend compatibility is checked against the installed versions.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 156–166. [R01–R16 reconciliation plan](2026-09-30-umwelt-os-spec-reconciliation.md) governs conflicting earlier examples.

## Global Constraints

- "Support exactly one owner profile."
- "Never hardcode secrets."
- "Every schema change must use Alembic."
- "Agents must use defined tools and APIs."
- "Do not introduce a Go backend or a second application language for backend services without measured need and a separate decision."
- "Create directories only when used."
- "Public APIs use /api/v1; earlier unversioned resource examples are shorthand, except /health."
- "Every derived piece of knowledge must be traceable back to its source."
- Preserve raw-source and document-history retention unless the owner explicitly deletes the relevant data.
- Python: four spaces; TypeScript: two spaces. Use existing dependencies before adding a framework.
- Target 2 cores/8 GiB/SSD with no mandatory local inference; distinguish available development hardware from target acceptance.
- Commit each completed phase as authorized. Merge Phase 1 into main after its implementation and review are complete. Do not push, deploy, reset, change branches or perform destructive owner-volume operations without explicit direction.
- n8n is source-available/fair-code; do not label the entire stack exclusively OSI open source.

## Review Focus

1. Durable acknowledgment must survive Redis/worker failure and duplicate delivery (P02, P07, P10).
2. Egress grants, source isolation, model fallback and tools must fail closed (P03, P06, P07).
3. Corrections, revisions and deletion propagate across every derived representation (P01, P04â€“P06, P12).
4. Chat retains the selected evidence/date/conversation through navigation and reconnect (P06, P08, P12).
5. A fresh install, upgrade, verified restore and measured target workload are separate release gates (P12).

## Plan Index and Dependencies

Phase 0 is implemented: [existing Phase 0 plan](2026-09-25-umwelt-os-phase-0.md). Preserve its acceptance evidence; do not rerun its implementation.

| Phase | Local implementation plan | Entry dependency | Tasks | Implementation |
| --- | --- | --- | --- | --- |
| 1 | [Core Data Platform](2026-09-25-umwelt-os-phase-1-core-data-platform.md) | Phase 0 acceptance | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 2 | [Ingestion and Packaged Connectors](2026-09-25-umwelt-os-phase-2-ingestion-connectors.md) | Phase 1 sources/documents public contracts | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 3 | [Search and Model Gateway Foundation](2026-09-25-umwelt-os-phase-3-search-model-gateway.md) | Phase 2 chunks/provenance; live model acceptance needs configured endpoint and permitted aliases | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 4 | [Entity Knowledge and Corrections](2026-09-25-umwelt-os-phase-4-entity-knowledge.md) | Phase 3 validated structured-output alias, search and public library | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 5 | [Temporal Knowledge and Timeline](2026-09-25-umwelt-os-phase-5-temporal-knowledge.md) | Phase 4 entities/evidence; Phase 3 permitted chat/structured/embedding capabilities | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 6 | [Chat, Drawer and Selective Memory](2026-09-25-umwelt-os-phase-6-ask-chat-drawer-memory.md) | Phase 3 retrieval/gateway, Phase 4 entities, Phase 5 temporal public APIs | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 7 | [Agent Harness, Tools, MCP and Approvals](2026-09-25-umwelt-os-phase-7-agent-harness-tools.md) | Phase 6 chat/memory; Phase 3 capabilities; Phase 2 isolated browser runtime | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 8 | [Configurable Dashboard, Tasks and Goals](2026-09-25-umwelt-os-phase-8-today-daily-chat-tasks.md) | Phases 1â€“7 public knowledge, chat, events and tools; source collection already runs in Phase 2 | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 9 | [GitHub Collection and Project Knowledge](2026-09-25-umwelt-os-phase-9-github-integration.md) | Phase 2 connector contract; Phases 4â€“8 entity/event/project presentation | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 10 | [Automation Rules and Workflow Management](2026-09-25-umwelt-os-phase-10-automation-workflows.md) | Phase 2 durable events/n8n; Phase 7 approvals; Phase 8 tasks/brief/notifications | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 11 | [Observability and Operational Controls](2026-09-25-umwelt-os-phase-11-observability-operations.md) | Phases 1â€“10 already emit run IDs, events, status and timing | 4 | Production code/build/source review delivered; P11 integration c58a56e; supplemental closure and runtime acceptance separately tracked |
| 12 | [Hardening, Backup, Recovery and Full Acceptance](2026-09-25-umwelt-os-phase-12-hardening-release-acceptance.md) | Production code/build contracts from Phases 1–11; behavioral acceptance deferred; actual target hardware and permitted live integrations for final release gate | 5 | Not implemented; preflight available; starts after dependency source closures |

Original P01–P03 code/build/review is complete. Execute supplementary foundations and remaining phases in the reconciliation plan order. A blocked live integration does not prohibit independent production schema/UI work, but dependent live acceptance stays blocked. Never mark a phase complete while its mandatory acceptance remains unverified.

## Decisions Preserved from the Owner

### Current Dashboard / Chat / Settings decisions

- All dashboard content is a gadget; multiple dashboards/groups/presets, max 20 square-unit columns, mobile/desktop layouts, edit-only grid, direct drag/resize and Save/Cancel/Undo/Redo.
- Large right shadcn Sheet contains only New chat, messages, composer/send-stop and close. Full Chat owns history/context/citations/activity/approvals/web search; shared conversation and draft. Port AnythingLLM source.
- Saved briefs retain revisions and selected date/timezone context remains available in gadgets. Current records are not historical task-state snapshots; no fixed Today layout.
- Three Settings groups: Data sources / AI & Ommi Router / Dashboard & Gadget. User menu owns account/theme/language.
- All normal connector setup stays in Umwelt-OS through protected adapters; n8n remains internal schedule owner. Provider OAuth consent and administrator app registration still apply.
- SSE for dashboard and run-scoped chat, REST commands; MCP client/server and capability-dependent collection.
- Add securely linked Google login, retain password pending owner choice. No local inference requirement.
- shadcn/Radix semantic UI, Recharts, globe.gl/deck.gl, en-us/vi-vi.

## Public Contracts and Ownership

- Sources/connectors own collection identity, source policy and cursor references. Documents own revisions and raw-content references; derived modules retain evidence links.
- Event time, observation time and validity time are separate. Persist UTC; use IANA timezone for date ranges with half-open boundaries.
- IDs are UUIDs internally; stable provider IDs are deduplicated within their source. Requests use validated schemas with unknown fields rejected on mutation.
- Read endpoints require owner authentication. Mutations require owner + Origin + session-bound CSRF. Collectors use separate source-scoped ingestion grants, never owner browser cookies.
- Lists use bounded limits and opaque cursors; input errors 422, missing resource 404, stale revision/known conflicts 409. Protected data responses are not shared-cacheable.
- Models own their tables in `modules/<capability>`. `core/` contains only consumed shared infrastructure/contracts. APIs/worker compose modules and never duplicate domain logic.
- The module descriptor provides id, name, version, description, enabled, dependencies, provides/requires, routes, emitted/consumed events, tools and settings schema. Add each field's consumer when used; no arbitrary plugin loader.
- Frontend module components/hooks/API clients stay under `apps/web/src/modules` with thin Next route wrappers. Shared shell, query/auth and drawer controller stay in frontend core.
- Domain events have id/type/version/time/producer/payload. PostgreSQL pending work/outbox is the durable bridge to ARQ; do not build a generic workflow engine or add Kafka.
- Every heavy parsing/indexing/browser job acquires the same cross-process lease; every model request uses the shared model concurrency limit. Lease failure never bypasses limits. Waiting approvals consume no worker capacity.
- Phase 1 supplies public auth dependencies; feature modules must not import private helpers from `core.auth.routes`.
- Phase 1 also updates Docker, wheel packaging, Alembic discovery, Make/PowerShell and CI to include real `modules`. The original Phase 0 configuration only includes `apps/core`.

## Canonical Model Coverage

| Models/capabilities | Owning implementation phase |
| --- | --- |
| Source, Document, DocumentVersion; private library and seed | 1 |
| Chunk, IngestionRun/Batch/Stage, source observations/cursors, raw storage | 2 |
| IndexGeneration, embeddings, search and model/privacy settings | 3 |
| Entity, EntityAlias, Relationship, evidence and corrections | 4 |
| Event, EventParticipant, temporal sync and graph mapping | 5 |
| Conversation, Message, response stream, Memory lifecycle | 6 |
| AgentRun, ToolCall, ApprovalRequest, effect ledger and MCP | 7 |
| Task, Goal, milestone, Topic, Story, Trend, DailyBrief, Notification | 8 |
| GitHub normalized records and repository relationships | 9 |
| Automation, rule revision, execution/action outcomes | 10 |
| Aggregated telemetry, quality/usage and retention settings | 11 |
| Backup manifest, export/recovery and full deletion acceptance | 12 |

Security, evidence, lifecycle logs and safe deletion are implemented with their owning feature; Phases 11â€“12 consolidate and audit them.

## Feature/UI Coverage and Scope Boundaries

| Feature | UI and functionality owner |
| --- | --- |
| Sources, manual notes/documents, version history | P01; sync/upload/configuration P02; GitHub P09 |
| News collection / story clustering / trends / relevance | P02 / P08 |
| Search and keyboard command palette | P03; later modules register their search/action providers |
| Knowledge entities/graph / Timeline | P04 / P05 |
| Full Chat and minimal drawer / selective Memory | P06 + R07 |
| Agent management, tool activity, approvals, browser-use | P07; task/goal tools P08; Automation specialist P10 |
| Configurable dashboard/gadgets, briefs, context, Tasks/Goals | P08 + R09–R12 |
| Notifications and owner interests | P08; operational producers start in their owning phases |
| Automation rules and run history | P10; n8n collection schedules already ship in P02 |
| System operations, data quality, usage and module lifecycle | P11; useful health exists from P00 |
| Model/privacy settings / storage/retention / backup | P03 / P11 / P12 |
| User-menu preferences / advanced AI agent settings / module settings | P01/P02/P08 as consumed / P07 / descriptors from P01 |
| First-run guided flow and complete fictional seed | Incremental with features; full acceptance P12 |
| Export, forget, backup/restore, offline and accessibility | Feature-local lifecycle first; complete P12 verification |

Required initial connectors: RSS/Atom, URL, file, REST, GitHub. Required file formats: PDF, TXT, Markdown, DOCX, JSON, CSV. Required GitHub resources: repositories, issues, pull requests, commits, releases.

Gmail/Calendar/Drive/Notion/Slack and named social/community providers remain later adapters per the spec. The news/social extension boundary is supported; this does not pretend those providers ship in the initial product. Optional Kanban, local inference and Langfuse are not silently enabled on the mini host. A scanned PDF without text is explicitly needs_ocr, not successfully indexed.

## Task Execution and Verification Protocol

1. Read this master, the phase plan, current code and `EXECUTION.md`. Reconcile drift before editing and preserve user changes.
2. The implementation stage covers Phases 1-12. Implement production code and run production builds only. Do not create, modify or run test files or fixtures, or run tests, lint, or standalone typechecks. The plans keep behavioral acceptance in a separately labeled deferred test-stage section.
3. Keep one task active. Record its ID, scope, code changes, exact build command/result, review findings, unresolved external gates and next task in `EXECUTION.md`.
4. Each task implements its production behavior and builds the affected deliverable. At phase completion, run `./scripts/dev.ps1 build` (or `make build`) and fix build failures before advancing. Do not use a test result to mark any task or phase complete during implementation.
5. Keep all test authoring and execution out of phase implementation checklists. Preserve behavioral acceptance requirements separately for the later test stage. Do not add production test-only routes or change security boundaries to accommodate future tests.
6. Continue through every ready task in P01-P12. A blocked live integration does not stop independent code work; record missing external evidence and do not mark it verified.
7. After all original Phase 1-12 and R01–R16 production code/build/review is complete, begin a separate test stage. First add/update the planned test files and fixtures, then run focused tests, integration/E2E suites, lint/typecheck, and the complete suite in the order defined by the relevant plans. Run destructive/reset/volume operations only against a verified disposable project/database.
8. At the end of the test stage, run the final build again, complete independent whole-branch review, fix findings and rerun the affected tests/builds, then update `docs/IMPLEMENTATION_STATUS.md`, the phase plans, architecture decisions when material, and `EXECUTION.md`.
9. Record exact commands and results; never invent evidence. Keep live-provider, target-hardware, restore and mini-host capacity gates separate from local tests/builds. Material architecture decisions and destructive owner operations require owner direction.
10. Continuous execution means continuing within an active run or resuming from this ledger. Markdown files do not create a scheduler/background process.
## Delivery Files and Current Checkpoint

- Preserve the original **49 task IDs** and Phase 0 acceptance evidence. [R01–R16](2026-09-30-umwelt-os-spec-reconciliation.md) adds **16 supplementary tasks** with exact files/contracts/deferred acceptance.
- Original Phases 1–3 code/build/review complete; commit 4d0f774 is in local main history. Runtime acceptance remains deferred.
- P04-T1 has uncommitted entity/relationship work and 0007_entities.py in D:/Project/Umwelt-OS-phase-4; preserve and reconcile before resuming.
- Current action: revised-plan review. Next production task: R01, then supplemental foundations and preserved Phase 4 work per new execution-order table.
- No production implementation begins during this planning turn. Preserve subagent-driven method, immediate task reports and authorized scoped commits/phase merges; no push/deploy.

## External Integration Evidence and Release Gates

Use primary project documentation and verify the versions installed at execution:
- [OmniRoute](https://github.com/diegosouzapw/OmniRoute): configured endpoint/aliases; per-capability testing and destination policy are Umwelt-OS responsibilities.
- [Graphiti](https://github.com/getzep/graphiti): start compatibility testing with FalkorDB; keep it separate from queue Redis, and do not infer 8GB suitability from backend support.
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts): persist checkpoints and resume only after tool permissions/approval state are revalidated.
- [n8n Schedule Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/): use explicit timezone and verify the pinned runtime's scheduling behavior.

Final acceptance imports three RSS feeds, one GitHub repository, five text-bearing PDFs and several URLs; verifies all primary screens, search/citations, entity/event/graph updates, approved tasks, automated briefs, recovery and backup/restore. Measure the target 2-core/8GiB workload; available larger-host evidence remains labeled separately.

## Reconciliation coverage and execution gate

Read [2026-09-30-umwelt-os-spec-reconciliation.md](2026-09-30-umwelt-os-spec-reconciliation.md) for supplementary dependency order and spec coverage. Original phase task counts are historical. All code/build/review must close before deferred test authoring/acceptance. Provider catalog entries are not implemented adapters. Review this revised plan before production execution; execution method is already subagent-driven.

## Current delivery snapshot — 2026-10-05

P01–P11 original production tasks delivered; P11 integratedc58a56e. Mandatory supplements remain: R12 source0522cc4 accepted/composition active; R14 source7086167 accepted/composition pending; R07 actual source port active; R15/P12/R16 production not complete. No full product or runtime/provider/restore/capacity acceptance asserted. Latest actual commits/task states live in EXECUTION.md; older planning examples are not current implementation evidence.

