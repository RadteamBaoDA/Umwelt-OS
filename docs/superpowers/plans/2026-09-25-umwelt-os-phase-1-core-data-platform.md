# Umwelt-OS Phase 1 — Core Data Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. The owner authorized continuous progress through ready tasks; use the [master plan](2026-09-25-umwelt-os-master-plan.md) and [execution ledger](EXECUTION.md), without repeated phase-scope approval.

**Goal:** Persist and manage sources and documents with provenance, immutable revisions, authenticated APIs, and useful library screens.

**Architecture:** Implement the sources, knowledge/documents capability using the existing modular monolith, public DTO/service/event boundaries and small frontend feature modules. Phase 0 acceptance supplies the entry contracts. Reuse the approved OSS components; create only files with a consumer in this phase.

**Tech Stack:** Existing FastAPI/Pydantic/SQLAlchemy/Alembic/PostgreSQL, Redis/ARQ, Next.js/React/TypeScript/TanStack Query, pytest and Playwright; phase-specific OSS dependencies are pinned only after their compatibility checks.

**Spec:** [Canonical specification](../../../specs/personal-intelligence-os-spec-v2.md), sections 2.3, 13, 73–85, 95, 110–112, 139–156. The [master plan](2026-09-25-umwelt-os-master-plan.md) defines common contracts, the drawer decision, test harness and ownership across phases.

**Entry gate:** Phase 0 acceptance.

**Implementation status:** Phase 1 production code/build and whole-branch source review are complete. P01-T1 was completed and tested before the owner changed the sequence; its historical evidence is retained. P01-T2 through P01-T4 task reviews and the final Phase 1 review are clean. Behavioral acceptance is deferred until all Phase 1-12 production code is complete.

Code stage: implement production code and run affected production builds only. Do not create, modify or run tests, lint, or standalone typecheck until production code for all Phase 1-12 is complete. Behavioral acceptance is listed separately in the deferred test-stage section.

## Spec reconciliation — 2026-09-30

Read [R01–R16 supplemental plan](2026-09-30-umwelt-os-spec-reconciliation.md) and canonical sections **165–166** before execution. Assigned scope: **R01/R02**. Preserve original task IDs and historical evidence. Production code/build only until all original and supplemental code/build/review is complete; no tests/fixtures, lint, standalone typecheck or runtime acceptance in this stage.

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

Module ownership: **sources, knowledge/documents**. Backend domain models/services stay in their owning module; frontend components/API clients stay in the matching frontend module. The files listed per task are planned paths; read existing files before editing and extend existing equivalents instead of creating duplicates. Module descriptor, navigation, settings and tool registration must use the common mechanisms from Phase 1/7/8 as applicable. Migrations are sequential after the previous accepted phase; once a revision has shipped, add a revision rather than rewrite history.

## Task P01-T1: Public auth boundary and runnable domain packaging


**Production files and responsibilities:** Modify core/auth/routes.py, core/system/routes.py, apps/api/main.py, pyproject.toml, infrastructure/docker/api.Dockerfile, infrastructure/postgres/migrations/env.py, Makefile and scripts/dev.ps1; create core/auth/dependencies.py and modules/__init__.py for domain-package discovery.

**Interfaces:** `require_owner(request, session) -> AuthSession` and `require_owner_write(request, session, origin, csrf_token) -> AuthSession`; preserve existing cookie/signature/expiry semantics.

- [x] **P01-T1.1 - Implement production behavior.** Move the shared session, Origin and CSRF checks behind the public auth dependency without copying implementations into feature modules. Include modules in wheel packaging, Docker COPY and Alembic metadata imports.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"authenticated":true,"csrfToken":"test-session-token"}
```

- [x] **P01-T1.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P01-T1.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Task P01-T2: Source/document schema and authenticated CRUD


**Production files and responsibilities:** Create modules/sources/__init__.py, modules/sources/models.py, modules/sources/schemas.py, modules/sources/public.py, modules/sources/routes.py and modules/sources/descriptor.py; modules/knowledge/documents/__init__.py, modules/knowledge/documents/models.py, modules/knowledge/documents/schemas.py, modules/knowledge/documents/public.py and modules/knowledge/documents/routes.py; required Python package markers; core/modules.py; infrastructure/postgres/migrations/versions/0002_library.py.

**Interfaces — consumes/produces:** SourceCreate(type,name,provider?), SourceRead(id,type,name,status,local_only,created_at,updated_at); DocumentCreate(source_id,title,content,external_id?,metadata?); DocumentRead includes current_version. GET/POST /sources and /documents; GET/PATCH/DELETE /sources/{id}, /documents/{id}; PUT /documents/{id}/content accepts expected_version; GET /documents/{id}/versions and /versions/{number}. Lists use limit<=100 and opaque next_cursor.

- [x] **P01-T2.1 - Implement production behavior.** Use UUIDs, JSONB and UTC timestamps; map the SQL column metadata through an ORM attribute such as metadata_json (DeclarativeBase reserves metadata). Match canonical fields; optional imported attributes start null. Keep source config server-managed and secret-free. Add unique non-null (source_id,external_id), unique (document_id,version_number), relevant FK/date indexes. Use a row lock and expected_version for append; stale input returns 409, identical content returns the current revision. Cap manual content at 1 MiB UTF-8 and metadata at 64 KiB; reject unknown fields and non-object metadata. Keep hashes server-derived. Implement core/modules.py with descriptors for these real source/knowledge consumers; reject duplicate IDs/missing dependencies and provide navigation/settings metadata. Source lifecycle active/paused/archived is separate from connector health. Connector-only deletion archives identity and retains documents; with_data deletes the owned documents/versions transactionally in this phase. Later phases extend this exact deletion intent to their derived data.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"source_id":"uuid","title":"Manual note","content":"Original text","metadata":{},"external_id":null}
```

- [x] **P01-T2.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P01-T2.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Task P01-T3: Library UI and first real module consumers


**Production files and responsibilities:** Create apps/web/src/core/app-shell/workspace-shell.tsx and apps/web/src/core/module-registry.ts; apps/web/src/modules/sources/api.ts, apps/web/src/modules/sources/source-form.tsx and apps/web/src/modules/sources/source-list.tsx; apps/web/src/modules/knowledge/api.ts, apps/web/src/modules/knowledge/document-form.tsx and apps/web/src/modules/knowledge/document-detail.tsx; route wrappers for /sources, /knowledge/documents, /knowledge/documents/[documentId] and /settings/system; modify apps/web/src/app/app/page.tsx.

**Interfaces — consumes/produces:** Frontend DTOs mirror the API. Thin Next route wrappers import their owning frontend module. /app becomes a compatibility redirect to /knowledge/documents; /settings/system preserves Phase 0 health. Root auth routing remains valid; Today takes over authenticated / in Phase 8.

- [x] **P01-T3.1 - Implement production behavior.** Make manual sources explicit in the product UI. Build sources/documents list, detail, version history, metadata edit and confirmed deletion using existing form/query/UI libraries. Preserve drafts on 409; invalidate affected query keys on success; clear private caches on logout/401. Module descriptors supply navigation/settings; do not show future screens. Reuse existing semantic colors and add proper empty/loading/error states.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"id":"knowledge","label":"Knowledge","href":"/knowledge/documents","enabled":true}
```

- [x] **P01-T3.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P01-T3.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Task P01-T4: Seed, contracts and delivery record


**Production files and responsibilities:** Create modules/knowledge/documents/seed.py; modify Makefile, scripts/dev.ps1, README.md, docs/development.md and docs/IMPLEMENTATION_STATUS.md.

**Interfaces — consumes/produces:** make seed and ./scripts/dev.ps1 seed explicitly import fictional Phase 1 fixtures; seed_demo(session) -> SeedReport(created,existing). No startup seeding or silent successful no-op commands.

- [x] **P01-T4.1 - Implement production behavior.** Use stable fixture identities in a dedicated demo namespace and never update user-edited records on repeat seed. Record schema and deletion behavior. Do not classify the rest of the canonical catalog as omitted—its owning phases are in the master.

Concrete contract/configuration shape (illustrative IDs/timestamps are test data, not production defaults):

```json
{"created":3,"existing":0}
```

- [x] **P01-T4.2 - Build the affected deliverable.** Run `./scripts/dev.ps1 build` (or `make build`) and fix production build failures before proceeding.

- [x] **P01-T4.3 - Record build evidence, commit and continue.** Record changed files, the exact production build command/result, review findings and unresolved gates in `EXECUTION.md`; commit the completed task and continue to the next ready task.

## Phase Acceptance and Handoff

- [x] Build the phase deliverables with `./scripts/dev.ps1 build` (or `make build`).
- [x] Confirm packaging, Alembic metadata, API routes and module descriptors are included in affected production builds.
- [x] Complete independent source review and fix actionable findings, then repeat affected production builds.
- [x] Update `docs/IMPLEMENTATION_STATUS.md`, this checklist and `EXECUTION.md`; advance to the next ready task.

Production-code completion for all Phases 1-12 is the gate to begin the separate deferred test stage.

## Deferred test-stage acceptance

This section is informational only during the code stage. Do not create or modify tests until production code for all Phase 1-12 is complete.

- Expired sessions and cross-origin mutations remain denied after extracting shared auth dependencies.
- Concurrent content updates cannot overwrite revisions or create duplicate version numbers.
- Deleting a connector preserves evidence unless the owner explicitly requests data removal.
- A deployment image must include new modules and Alembic must discover their metadata.
- Tests must not depend on owner-setup test order or erase a non-test database.

### P01-T1

Planned test-stage files: `tests/integration/conftest.py`, `tests/integration/test_library_auth.py`, `tests/test_auth_session.py`, `tests/test_auth_setup.py`, `tests/e2e/fixtures.ts`, `scripts/test.sh`, `docker-compose.test.yml` and `.github/workflows/ci.yml` (test runner/CI wiring).

- Anonymous access to protected system health is rejected; public auth dependencies preserve cookie, signature, expiry, Origin and CSRF behavior.
- Cover missing Origin, expired CSRF, wrong session token and unrelated integrity errors. Exercise owner-race behavior, authenticated API access and browser bootstrap with an isolated disposable database; browser setup must not rely on test-file ordering. Configure the targeted API/browser runner, loopback-only disposable PostgreSQL, transaction/savepoint isolation and disposable-owner setup here. Retain Ruff/mypy CI configuration as deferred quality gates; do not run them in the code stage. P01-T1 was historically tested before the code-only stage; retain its actual commands and outcomes in `EXECUTION.md`.

### P01-T2

Planned test-stage files: `tests/integration/test_library_api.py`.

- A content update preserves the original immutable revision. Cover concurrent revision writes (one winner and one 409), unknown IDs, stable cursor ordering, protected reads/writes, and upgrade from 0001_auth followed by a repeat upgrade without auth loss.

### P01-T3

Planned test-stage files: `tests/e2e/library.spec.ts`.

- Cover document creation and reload, keyboard submission, conflict with draft preservation, delete cancel/confirm, source retirement, and a small viewport.

### P01-T4

Planned test-stage files: `tests/integration/test_seed.py`.

- Seeding twice creates no duplicate demo data and preserves a modified demo note. Cover migration from a Phase 0 database copy and the common deferred gate.

## Plan Self-Review Checklist

- [x] Goal and spec sections mapped to named tasks and public interfaces.
- [x] Deferred acceptance risks retained for the post-code test stage.
- [x] Exact production file targets, deferred acceptance criteria, implementation rules and build criteria included.
- [x] Module ownership, auth/privacy, safe deletion and retry/resume boundaries preserved.
- [x] Implementation and live/hardware verification are not claimed complete by this plan.
