# Live World Map Implementation Plan

> **For agentic workers:** Implement the approved live-map scope in coordinated tasks, review each owned seam, and preserve the owner's unrelated worktree edits.

**Goal:** Provide a dashboard map with browser-triggered, ephemeral world, technology, and finance data, without persisting provider responses.

**Architecture:** Workspace-authorized same-origin APIs read fixed upstream providers through bounded, quota-aware requests. Normalized events and weather imagery exist only in bounded RAM caches and browser memory; credential/configuration storage is separate from source ingestion. Existing source-backed maps remain compatible.

**Tech Stack:** FastAPI, Python, PostgreSQL for encrypted configuration only, React Query, Next.js, globe.gl, deck.gl, existing semantic UI.

**Spec:** The approved live-world-map plan in the current user conversation supersedes earlier managed-source proposals.

## Global Constraints

- No provider content in database, Redis, files, local storage, IndexedDB, service worker, logs, or durable cache.
- Owner explicitly permits shared PostgreSQL quota metadata (provider, counters and timestamps) without upstream payloads or plaintext API keys.
- Browser visibility controls collection; shared in-flight work and bounded memory caches reduce upstream calls.
- Scope and credentials are workspace-bound; owner writes, authorized reads, secure credential fences, fixed hosts, and provider terms remain enforced.
- Old map instances retain `selected_sources`; new map templates default to live mode.
- Weather global temperature, precipitation, wind and clouds; USGS, EONET, FIRMS; HN, Lobsters, CISA, NVD, GitHub releases, business, Fed and SEC.
- Source timestamps and locations remain truthful; unknown locations never become markers; refresh cadence reflects provider limits.

## Review Focus

- In-flight credential or membership changes must not publish stale data or send with revoked authority.
- No secret or provider content leaks to durable stores, errors, proxy logs, or unscoped caches.
- No arbitrary target URL, uncontrolled redirect, unbounded payload, or unbounded raster request is accepted.
- Closing, hiding, switching workspace, or unmounting cancels work and prevents stale publication.
- Weather projection, original map interactions, and older saved map definitions stay functional.

## Tasks

- [ ] Backend provider reads: public bounded transient event API, safe provider normalization, cache/error handling and authenticated weather tile proxy; verify adapters and no-persistence behavior.
- [ ] Workspace live credentials: encrypted owner-controlled keys and SEC contact with scope/revision fencing, migration and erasure coverage; verify redaction and access lifecycle.
- [ ] Dashboard configuration: add live mode, provider scopes, migration-compatible defaults and server validation; verify older saved maps.
- [ ] Map weather: draw bounded correctly projected ephemeral tile imagery in existing globe and flat engines, preserve point selection and fallback; verify projection and teardown.
- [ ] Live gadget UI: shared in-memory requests, visibility-driven refresh, filters, details, configuration, localization, loading/error/freshness and accessible interaction; verify source and scope switching.
- [ ] Integration: reconcile contracts, review every security boundary and mandatory function documentation, run focused tests/build and report unrun provider/Docker/browser acceptance.
- [ ] Complete scope/impact audit, commit the isolated work, and integrate the feature branch into the user's original branch while preserving pre-existing changes.

## Checkpoint: 2026-11-04

- Added separate pending operator review for live terms, fenced to the exact acknowledgement; backend and UI focused tests pass. End-to-end operator authorization and concurrency still need integration verification.
- Live quota now updates shared quota windows without provider-send or per-request debit rows; this subject is instance-wide and does not combine a shared key's consumption with existing ingestion provider quotas. PostgreSQL concurrency remains unverified.
- Focused Python suite: 31 passed. Dashboard focused suite: 68 passed before the additional pending-review case, which separately passed with the consent test (2 passed). Typecheck passed. Full audit, build, browser and provider validation remain.
- Target branch advanced to 4f25f88b and has new migrations through 0010. Feature migration currently branches from 0007 and must be reconciled with target history during integration. Target has unrelated uncommitted changes; preserve them.
- GitNexus detect_changes returned critical risk and a stale, incomplete index that excludes untracked symbols. Re-run after integration and review affected surfaces; do not treat this output as complete proof.

## Checkpoint: source and map follow-up

- Matched EONET source links to the API contract; configured live point selection to focus both map engines while preserving source-backed map defaults. Focus tests cover the flat view; real WebGL still needs validation.
- USGS now requests the month feed and CISA retains the latest additions when the bounded result is truncated. Focused Python suite: 32 passed; frontend focused selection/consent/source tests and TypeScript passed.
- Webpack build compiled successfully, but its standalone trace omitted 27 files outside the tracing root because of the dependency junction. Turbopack build failed on the same external junction. Next.js reported a security upgrade recommendation. These are not deployment proof.
- Python production Ruff checks passed; focused frontend ESLint completed with one existing renderer effect-dependency warning. API factory imports with an explicit test Redis URL. No live providers, browser acceptance, PostgreSQL migration/concurrency, Docker or E2E have been exercised.

- Refreshed GitNexus index with `--index-only --skip-fts`; analysis reported truncated process discovery and cross-language unresolved links. `detect_changes` reports critical risk and currently covers tracked changes only; include new files and reassess before committing.
- Latest focused checks: 36 Python tests and 77 frontend tests passed. Weather teardown has a focused abort test, and API OpenAPI includes all five live-map routes. Browser/WebGL and live PostgreSQL/provider verification remain open.

## Integration rehearsal

- A temporary Git index based on target `4f25f88b` was used to apply all worktree changes without modifying either checkout or their real indexes. Three files conflict: `apps/web/src/app/settings/global/page.tsx`, `apps/web/src/core/messages/dashboard.ts`, and `apps/web/src/modules/dashboard/gadgets/globe-map.tsx`.
- Target extracted the existing operator review UI into `modules/settings/provider-review.tsx`; integrate live terms there without losing target settings changes. Target globe now uses `globe-view.ts` location keys to preserve user camera state; reconcile with live selection focus and weather rendering. Preserve both locale catalogs.
- Target migration head is `0010_observations_vn_stock`; live `0008_live_map_config` currently branches from `0007`. Reconcile the migration ancestry after target integration, before a migration check or merge.
- Focused suite now passes 37 Python tests, including HTTP-level denial for anonymous, nonoperator, and operator without CSRF. The temporary index is only a rehearsal, not a merge or proof of runtime compatibility.

## Integration progress

- Created feature checkpoint commit `283864bc` and merged target `4f25f88b` into the isolated worktree for conflict resolution; target checkout and its staged/unstaged changes were not touched. This merge is not yet committed or integrated back.
- Combined live reviews with target's existing provider-review table, preserving connector payloads; focused review and dashboard tests passed (74 frontend). Preserved target globe location-key behavior alongside live selection and weather. Python focused suite passed (37).
- Added `0011_merge_live_map` with both existing migration heads as parents; offline Alembic history now shows one head. PostgreSQL migration execution and concurrency remain unverified.

- After reconciliation, 56 existing Dashboard Python tests and 74 focused frontend tests passed; Alembic generated offline SQL from `0010_observations_vn_stock` through the merged head. Webpack completed after a Next.js security warning, but omitted 27 standalone trace files outside the junction tracing root.
- Reindexed the merged working tree and ran GitNexus `detect_changes(scope=all)` twice. Both reported critical risk, 1,415 changed symbols and `truncated: true` (1,000 shown); the merge includes target history and this is not a clean or complete graph audit. The source review and targeted tests cover selected seams only.

## Review follow-up in progress

- Independent review identified admin-view access, stale event display after terms withdrawal, and provider cancellation gaps. Uncommitted fixes allow only owner or admitted admin reads (redacting SEC contact for admin), bind event queries to config revision and eligibility, and cancel active provider I/O when a request disconnects. The existing member denial remains aligned with Dashboard admission.
- A second review found the cancellation change can cancel a shared fetch when its first client disconnects even while another waits. `test_shared_fetch_survives_leader_disconnect_while_another_client_waits` reproduces this and currently fails: two upstream fetches instead of one. Implement observer-aware shared-task lifetime and per-send authority before treating this work as ready.
- Added focused weather projection and teardown checks and updated the account erasure inventory; account-erasure coverage tests pass. No final merge into the user's branch has occurred.

## Shared-fetch review resolution

- The previously failing two-client test now passes: a shared provider task retains independently fenced observers, switches to a connected authorized session for each send, and cancels only after its final observer leaves. Final publication revalidates each client and is cancelled on disconnect; shared results remain available until cache insertion. Bounded-cache eviction and config-revocation behavior have regression tests.
- Follow-up independent review found no further concrete issue in those changes. Its limits: simulated requests only; real ASGI disconnects, live provider access, and PostgreSQL concurrency remain unverified.
- Target checkout now has an additional uncommitted migration `0011_model_capability_evidence` based on `0010`. The owner approved adding an uncommitted no-schema merge revision after integration; an isolated Alembic preview produced one head. Preserve the owner's migration and other working changes.
