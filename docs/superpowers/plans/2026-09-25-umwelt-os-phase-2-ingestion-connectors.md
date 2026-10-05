# Umwelt-OS Phase 2 — Ingestion and Packaged Connectors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Durably ingest supported files and collect RSS/Atom, URL and REST data with provenance, bounded jobs and recoverable progress.

**Architecture:** Implement the ingestion, connectors, news capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 1 sources/documents public contracts supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query, pytest and Playwright; phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 30–31, 52–55, 67–68, 89, 95, 138.3–6, 158–161. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 1 sources/documents public contracts.

**Implementation status:** Original phase production code/build/review complete; behavioral acceptance deferred. Supplemental tasks not started; do not rerun completed baseline.

Code stage: implement production code and run affected production builds only. Do not create, modify or run tests, lint, or standalone typecheck until production code for all Phase 1-12 is complete. Behavioral acceptance is listed separately in the deferred test-stage section.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R03/R04**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

Module ownership: **ingestion, connectors, news**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.

## Task P02-T1: Durable receipt, run stages and dispatch


**Production files and responsibilities:** Create modules/ingestion/models.py, modules/ingestion/schemas.py, modules/ingestion/public.py, modules/ingestion/routes.py and modules/ingestion/dispatcher.py; core/events.py; infrastructure/postgres/migrations/versions/0003_ingestion.py; modify apps/worker/main.py.

**Interfaces — consumes/produces:** POST /ingestion/batches -> 202 Receipt(batch_id,run_id,status); GET /ingestion/runs/{id}; POST /ingestion/runs/{id}/retry. Collector credentials are restricted to ingestion and allowed source IDs. ReceiveBatch contains source_id,batch_key,cursor_before,cursor_after,records; record identity is provider_id + version/hash. DomainEvent(id,type,version,occurred_at,producer,payload).

- [x] **P02-T1.1 - Implement production behavior.** Persist batch identity, observations, expected cursor and pending stage rows in one PostgreSQL transaction; acknowledge only after commit. Use PostgreSQL pending-work/outbox records and ARQ execution, not a second queue implementation. Compare-and-set cursor advancement, one source collection lease with expiry, idempotent stage keys, explicit timeouts and 4 transient retries with backoff/jitter; permanent auth/schema errors fail visibly. A dispatcher reconciles pending rows after Redis restart.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"source_id":"uuid","batch_key":"provider-page-version","cursor_before":null,"cursor_after":"page-2","records":[{"provider_id":"item-1","content":"text","observed_at":"2026-09-25T02:00:00Z"}]}
```

- [x] **P02-T1.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P02-T1.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Task P02-T2: File storage, parsers and chunking


**Production files and responsibilities:** Create core/storage.py; modules/ingestion/files.py, modules/ingestion/parsers.py, modules/ingestion/chunking.py and modules/ingestion/worker.py; modify Docker data mounts, pyproject.toml and uv.lock.

**Interfaces — consumes/produces:** POST /documents/upload -> Receipt; GET /documents/{id}/raw authenticated download; parse_file(path,mime) -> ParsedDocument(text,metadata,warnings); chunk_text(text,target_tokens=750,overlap_ratio=0.12) -> list[ChunkDraft]. Chunk stores document_version_id,index,content,token_count,metadata; vector indexing arrives Phase 3.

- [x] **P02-T2.1 - Implement production behavior.** Use stdlib for TXT/JSON/CSV, maintained pypdf and python-docx for text-bearing PDF/DOCX, and a maintained tokenizer for deterministic bounded chunks. Lock dependencies before use. Default input cap 25 MiB, parser deadline 120s, expanded DOCX cap 100 MiB and PDF page cap 500; configurable deployment limits. Verify file signatures, reject traversal, generated UUID storage names, atomic file finalize + durable DB receipt and orphan cleanup after grace period. Retain original bytes and extraction provenance. Scanned PDFs return needs_ocr, not success; OCR is not promised by this text parser. Clamp boundaries so small documents terminate without looping.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"status":"needs_ocr","warnings":["No extractable text"],"document_id":"uuid"}
```

- [x] **P02-T2.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P02-T2.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Task P02-T3: n8n workflows and Crawlee collection


**Production files and responsibilities:** Create modules/connectors/public.py, modules/connectors/registry.py, modules/connectors/n8n.py, modules/connectors/crawl.py and modules/connectors/routes.py; infrastructure/n8n/workflows/rss.json, infrastructure/n8n/workflows/url.json and infrastructure/n8n/workflows/rest.json; infrastructure/docker/browser.Dockerfile; docker-compose.connectors.yml; docs/connectors.md.

**Interfaces — consumes/produces:** Connector.validate(source), sync(source,cursor), normalize(record), health(source); POST /sources/{id}/validate, /sync; PATCH source pause/resume; crawl submissions return run_id. Umwelt-OS controls source identity/cursors; n8n owns external schedules/credentials.

- [x] **P02-T3.1 - Implement production behavior.** Package importable n8n workflow exports using credential references and protected receipt endpoints; include RSS/Atom pagination/overlap, URL and REST mappings. Add source timezone default Asia/Ho_Chi_Minh. Implement bounded overlap catch-up from last acknowledged cursor, not replay of every missed cron tick. Use Crawlee HTTP with BeautifulSoup first and PlaywrightCrawler for configured JS pages. One browser job, default 10 pages/depth2/60s and 25MiB aggregate download; network-level egress restrictions plus URL/redirect/DNS checks. No model-driven navigation until Phase 7.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"source_id":"uuid","mode":"http","max_pages":10,"max_depth":2,"timeout_seconds":60}
```

- [x] **P02-T3.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P02-T3.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Task P02-T4: Collection UI, deletion lifecycle and diagnostics


**Production files and responsibilities:** Extend apps/web/src/modules/sources/ with connector-setup.tsx and sync-history.tsx; create apps/web/src/modules/ingestion/upload.tsx; extend source deletion service and docs/privacy.md.

**Interfaces — consumes/produces:** Source health exposes collected_at,indexed_at and distinct collection/processing errors; data removal may return 202 with operation_id, completed synchronous removals remain 204. GET /system/operations/{id} tracks deletions; SourceRead includes retirement state.

- [x] **P02-T4.1 - Implement production behavior.** Show received/parsed/chunked separately from embedded/indexed—Phase 2 never claims semantic readiness. Guide credential setup to n8n and return validation; allow Sync now, pause/resume, retries and raw provenance inspection. Implement durable purge tombstones before deleting files/chunks/outbox work; workers check current source/document generation before writes. Prevent in-flight jobs recreating deleted content. Empty search/AI states remain explicit.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"collection":"succeeded","processing":"chunked","embedding":"not_configured","last_error":null}
```

- [x] **P02-T4.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P02-T4.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Phase Acceptance and Handoff

- [x] Build the phase deliverables with `./scripts/dev.ps1 build` (or `make build`).
- [x] Confirm packaging, Alembic metadata, API routes and module descriptors are included in affected production builds.
- [x] Complete independent source review and fix actionable findings, then repeat affected production builds.
- [x] Update `docs/IMPLEMENTATION_STATUS.md`, this checklist and `EXECUTION.md`; advance to the next ready task.

Production-code completion for all Phases 1-12 is the gate to begin the separate deferred test stage.

## Deferred test-stage acceptance

This section is informational only during the code stage. Do not create or modify tests until production code for all Phase 1-12 is complete.

- Duplicate batch delivery preserves one provider record and all distinct observations.
- Redis loss after receipt does not lose accepted data or advance an uncommitted cursor.
- Imported bytes and browser redirects cannot escape storage or reach protected internal networks.
- Source pause/delete racing with a worker prevents new processing or reappearance of forgotten data.
- An unsupported/scanned document must report extraction limits rather than indexing success.

### P02-T1

Planned test-stage files: `tests/integration/conftest.py` (collector fixtures) and `tests/integration/test_ingestion.py`.

- Duplicate batch delivery returns the same run. Cover crash after database commit but before enqueue, stale cursor writer, overlapping sync, cancellation, scoped token denial, and logs that identify run/stage without full payloads.

### P02-T2

Planned test-stage files: `tests/test_parsers.py`, `tests/fixtures/ingestion/`.

- CSV parsing preserves Unicode text and headers. Cover all six supported formats, malformed/encrypted PDF, ZIP expansion, Unicode, oversized input, interrupted writes, upload/raw-file authorization, and stable chunk count/hash across retries.

### P02-T3

Planned test-stage files: `tests/integration/test_collectors.py`.

- A paused source rejects collection. Validate the pinned n8n missed-schedule behavior and exercise packaged workflows against a local disposable fixture provider; cover redirects, DNS changes, private addresses and browser subresources. Confirm n8n outage leaves manual upload and existing knowledge usable, and pausing stops schedules and rejects queued stale runs.

### P02-T4

Planned test-stage files: `tests/e2e/ingestion.spec.ts`.

- Upload progress distinguishes received, parsed and chunked from embedded/indexed. Cover upload, RSS update, duplicate delivery, pause/resume, delete during processing and connector-only source removal. Reset cleanup must stay within disposable projects.

## Plan Self-Review Checklist

- [x] Goal and spec sections mapped to named tasks and public interfaces.
- [x] Deferred acceptance risks retained for the post-code test stage.
- [x] Exact production file targets, deferred acceptance criteria, implementation rules and build criteria included.
- [x] Module ownership, auth/privacy, safe deletion and retry/resume boundaries preserved.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.
