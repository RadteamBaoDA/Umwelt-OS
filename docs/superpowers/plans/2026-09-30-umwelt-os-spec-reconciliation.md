# Umwelt-OS Spec 165–166 Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Preserve the owner-selected execution method. This plan requires review before implementation; this planning turn does not start production work.

**Goal:** Reconcile the existing Phase 1–12 delivery with the configurable Life Dashboard, embedded connector/MCP setup, SSE, Google sign-in and current source ownership decisions.

**Architecture:** Keep the Python/FastAPI modular monolith and existing PostgreSQL/pgvector, protected raw storage and Redis/ARQ. n8n owns its external source schedules behind Umwelt-OS adapters; LangGraph and Graphiti retain their approved roles. Next.js provides Dashboard/Chat/Settings; OpenAI SDK uses the existing OmniRoute boundary.

**Tech Stack:** Existing locked stack; shadcn/ui with Radix and semantic tokens, Recharts through shadcn Chart, globe.gl/deck.gl, maintained OAuth/OIDC and MCP libraries selected/pinned when consumed. No mandatory Ollama, Airbyte, separate time-series DB or WebSocket service.

**Spec:** [Canonical sections 165–166](../../../specs/personal-intelligence-os-spec-v2.md#165-approved-life-dashboard-and-settings-revision--2026-09-26); [architecture decisions](../../ARCHITECTURE_DECISIONS.md); [design system](../../DESIGN_SYSTEM.md). Older phase examples do not override these sections.

## Global Constraints

- "Support exactly one owner profile."
- "Never hardcode secrets."
- "Every schema change must use Alembic."
- "Agents must use defined tools and APIs."
- "Create directories only when used."
- Public application APIs use `/api/v1`; protected writes require owner/Origin/session-bound CSRF. Collectors and inbound MCP clients use separate scoped authentication.
- Main navigation: **Dashboard / Chat / Settings**. Settings: **Data sources / AI & Ommi Router / Dashboard & Gadget**. Account/appearance/language belong to the user menu.
- App locale IDs `en-us` / `vi-vi`; formatting locales `en-US` / `vi-VN`; themes Light/Dark/System.
- Dashboard uses at most **20 square-unit columns**, separate desktop/mobile layouts and document scrolling.
- One heavy background job and two simultaneous model requests are starting cross-process limits, not measured capacity guarantees.
- Production code/build only until **all original and reconciliation production tasks** are complete. Do not create, modify or run tests/fixtures, lint, standalone typecheck or runtime acceptance during this stage.
- Retain historical evidence and completed task IDs; new requirements do not retroactively mark production functionality complete.
- Report task status immediately after completion, record exact evidence and next task, commit completed work/merge completed phases per existing owner authorization; no push/deploy or destructive owner-data operations.
- Google-only versus password fallback is unresolved. Implement additive Google login and retain password until the owner chooses otherwise. Graph backend and provider capabilities remain explicit external gates.
- Select compatible dependencies/maintained public APIs during execution, lock versions, retain notices; package/license installation is not feature completion.

## Review Focus

1. Owner linkage must reject arbitrary Google accounts, forged/replayed callbacks and removal of the last login method (R02).
2. Source settings can save while external activation fails; retries and disablement must not duplicate or resurrect collection (R03/R04).
3. SSE reconnect must survive expired replay windows and the snapshot/subscription race without duplicate sends or lost reading state (R06/R07/R11).
4. MCP discovery and source text must not grant tool permissions; schema drift/revoked grants must fail closed (R08).
5. Missing source access, units, CII method/license or provider capabilities must remain unavailable rather than fabricated data/coverage (R12–R15).

Each risk has deferred acceptance scenarios in its owning task. No test files are authored in this stage.

## Source ownership and migration policy

Follow current `public.py / routes.py / schemas.py / models.py` module shapes. Add `service.py`, `worker.py` or provider files only when consumed. Frontend routes stay thin; feature code lives in `apps/web/src/modules`, shared UI in `components/ui`, shared session/realtime infrastructure in frontend `core`.

Source and document owners expose DTO/service contracts. Cross-module persistence access must be explicitly approved read projection with permission/deletion invariants, or replaced with an owner public contract. Domain writes remain owner-only.

Migration filenames in old unexecuted plans are illustrative sequence labels, not permission to collide with existing revisions 0001–0006. At execution generate the next unique revision, inspect the current head and migrations in all active worktrees, including uncommitted Phase 4 0007_entities.py, and set the actual dependency:

```powershell
uv run alembic revision -m "owning feature name"
```

Replace the message with the task's feature name, implement the generated revision and record its real path in the task report. Do not rewrite shipped migrations or run migrations as acceptance in the code/build stage.

## Execution order and phase integration

| Order | Work | Completion boundary |
| --- | --- | --- |
| 1 | R01 → R02 → R03 → R09 → R04 → R05 → R06 | Supplemental foundations; original P01–P03 evidence retained |
| 2 | P04-T1–T4 → P05-T1–T4 | Original entity/time/graph work; gate dependent graph code on compatibility |
| 3 | P06-T1/T2 → R07 with P06-T3 → P06-T4 | Grounded chat, ported UI and selective memory |
| 4 | P07-T1 with R08 → P07-T2–T4 | MCP/tool boundaries, harness, approvals and specialists |
| 5 | P08-T1–T3 → R10 → R11 replacing P08-T4 fixed layout → R12 | Tasks/brief/news delivered as configurable gadgets |
| 6 | P09-T1–T4 → R13 → R14 → R15 | GitHub plus source catalog/adapters, observation/map/intelligence support |
| 7 | P10-T1–T4 → P11-T1–T4 → P12-T1–T5 with R16 | Automation/operations/recovery production deliverables |
| 8 | All code/build/reviews closed → deferred validation stage | Test authoring/execution, live integrations and target capacity |

R09 follows R03 here so the new shell can host the source editor; R09 consumes R06 footer state when R06 becomes available. Unavailable streams/providers remain visibly unavailable. A live gate can delay dependent work while independent tasks continue; no test stage begins early.

R01–R16 are supplementary task IDs, not renumberings of the original 49 tasks. The completed P01–P03 baseline is not reopened; their new scope is tracked separately. Existing Phase 4 worktree must be inspected for uncommitted progress before resuming; this plan claims no completion there.

## Production task steps

Every task first reads its existing owning phase, spec and consumers. File lists are exact planned owners; extend an existing equivalent rather than create duplicates. For every changed symbol run GitNexus impact before edits, warn on HIGH/CRITICAL and trace direct dependents. If the index is stale/degraded, refresh it and supplement incomplete results with source tracing. Before commit run change detection and inspect the real diff.

## Task R01: Repository instructions, boundaries and build-stage CI

**Phase/dependencies:** Before new production work; supplements P01.

**Files and responsibilities:** Modify: AGENTS.md, README.md, .agents/skills/implementing-umwelt-os/SKILL.md, .agents/skills/implementing-umwelt-os/plan-map.md, .github/workflows/ci.yml, docs/development.md. Modify only cross-module callers confirmed by impact analysis when an internal import must be replaced; extend modules/knowledge/documents/public.py and modules/sources/public.py for the consumed read contract.

**Interfaces — consumes/produces:** Consumes spec 165–166 and current source. Produces one current-task ledger, an explicit public-contract/read-projection policy and build-only default CI; deferred validation is a separately enabled workflow stage.

- [ ] **R01.1 — Trace and implement the owning production behavior.** Keep apps/api/main.py a composition root. Move domain-specific collection orchestration from apps/worker/main.py into modules/ingestion/worker.py only where needed for clear ownership; leave worker registration in apps/worker. Document any permitted read projections by owner, allowed fields and deletion/permission checks. Add public DTO reads for callers that currently import private persistence models without an approved projection contract. Do not create empty directories or a generic repository framework. In CI retain dependency installation and production build; gate existing lint/typecheck/test steps behind explicit deferred-validation dispatch so push/PR during code stage runs builds only.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"delivery_stage":"code_build","default_checks":["build"],"deferred_checks":["lint","typecheck","test"]}
```

- [ ] **R01.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R01.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Existing setup/build commands still work; the deferred workflow performs the original validation; boundary checks distinguish authorized projections from private writes.

## Task R02: Google sign-in and secure owner linking

**Phase/dependencies:** After R01; supplements P01; no new owner registration.

**Files and responsibilities:** Create: core/auth/google.py, core/auth/google_schemas.py, apps/web/src/modules/account/google-link.tsx. Modify: core/auth/models.py, core/auth/routes.py, core/config.py, apps/web/src/app/login/page.tsx, .env.example, docker-compose.yml, docs/deployment.md, pyproject.toml and uv.lock when selecting the maintained OIDC dependency. Generate an Alembic revision under infrastructure/postgres/migrations/versions/ for identity linkage.

**Interfaces — consumes/produces:** GET /api/v1/auth/google/status returns configured,linked; POST /api/v1/auth/google/start accepts purpose=login|link and returns authorization_url; GET /api/v1/auth/google/callback exchanges code; POST /api/v1/auth/google/unlink requires owner reauthentication. Issuer+subject identifies the linked owner. Existing session/logout/CSRF contracts remain.

- [ ] **R02.1 — Trace and implement the owning production behavior.** Use a maintained OIDC library with authorization-code+PKCE, browser-bound single-use expiring state and nonce. Link requires a recently reauthenticated owner; login cannot create/link an identity. Verify signature/JWKS, issuer, audience, expiration, nonce and verified email, then rotate Umwelt-OS session/CSRF cookies. Request openid email profile only; bounded callback handling and allowlisted return targets. Retain password login because Google-only policy is unresolved. Reject unlink of the last usable method; show unconfigured/provider-error states. Store client secret server-side and document one-time provider app registration; Gmail collection grants are separate.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"purpose":"link","scopes":["openid","email","profile"],"identity_key":["issuer","subject"],"password_fallback":"retained"}
```

- [ ] **R02.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R02.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Forged/replayed callback, wrong audience, cancelled consent, unlinked Google login, session fixation, expired linking proof, unlink lockout and login without Gmail permission.

## Task R03: Connector catalog, credential ownership and n8n reconciliation

**Phase/dependencies:** After R01; supplements P02.

**Files and responsibilities:** Create: modules/connectors/catalog.py, modules/connectors/credentials.py, modules/connectors/provisioning.py, modules/connectors/oauth.py. Modify: modules/connectors/public.py, modules/connectors/registry.py, modules/connectors/n8n.py, modules/connectors/routes.py, modules/sources/models.py, core/config.py, .env.example, infrastructure/n8n/workflows/{rss,rest,url}.json, docs/connectors.md. Generate a provisioning/grant Alembic revision.

**Interfaces — consumes/produces:** CatalogEntry declares provider_id,auth_methods,scope_fields,collection_modes,history/edit/delete support and availability. POST /api/v1/connectors/{source_id}/validate returns validation results without activation; PUT /api/v1/connectors/{source_id}/configuration accepts expected_revision and desired settings; GET /api/v1/connectors/{source_id}/activation returns desired_revision,applied_revision,state,error_code.

- [ ] **R03.1 — Trace and implement the owning production behavior.** Use supported pinned n8n APIs for template creation/update, credentials/reference association and activation; no n8n database writes. Persist desired revision and recoverable reconciliation work before calling external APIs. Retry an idempotent reconcile without duplicating workflow/credentials. Fence disabled sources immediately. Native credentials live in protected Umwelt-OS storage; n8n credentials use its protected store and Umwelt-OS references. Mask/redact secrets, distinguish unchanged/replaced/removed values, enforce SSRF and endpoint policy. Add server OAuth start/callback/refresh/revoke flows to the relevant provider adapter; prevent stale callback/configuration overwriting a new revision. Mark unsupported provisioning operations unavailable rather than sending the owner to n8n UI.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"desired_revision":2,"applied_revision":1,"state":"saved_not_active","credential_ref":"opaque-reference"}
```

- [ ] **R03.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R03.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Save followed by activation failure, retry after worker crash, duplicate reconcile, source disable race, credential rotation, OAuth revocation and unsupported n8n credential API.

**Execution amendment — 2026-09-30:** Current RSS/URL/REST adapters have no OAuth-grant consumer. Retain the mandatory **R03-OAuth** production slice and deliver it with the concrete GitHub adapter in **P09-T1**, including start/callback/refresh/revoke, revision/source-generation fencing and an actual authorized read-only collection consumer. Do not build disconnected generic Google grant endpoints or claim Gmail/Calendar/Drive collection. R03-core catalog/credentials/n8n reconciliation can build, review and integrate before that slice; full R03 remains incomplete until R03-OAuth closes. Catalog OAuth capabilities stay planned/unavailable with a precise reason until implemented. R09 is shell/theme/locales. This amendment changes delivery placement, not requirements, and the deferred test stage still waits for R03-OAuth plus every other production task.


## Task R04: Embedded data-source editor

**Phase/dependencies:** After R03; supplements P02.

**Files and responsibilities:** Modify: apps/web/src/modules/sources/{api.ts,connector-setup.tsx,source-list.tsx,sync-history.tsx}. Create: apps/web/src/modules/sources/connector-editor.tsx, apps/web/src/modules/settings/settings-workspace.tsx. Modify settings route wrappers and existing source route to route into the Settings workspace.

**Interfaces — consumes/produces:** Consumes R03 catalog/configuration/activation APIs and existing sync/pause/run APIs. Produces Connect → Choose data → Collect editor inside Settings/Data sources, including a nested MCP entry when R08 is available.

- [ ] **R04.1 — Trace and implement the owning production behavior.** Use provider schemas and real authorized scope discovery. Keep credential/connect/scope/schedule/timezone/history/Save & enable in Umwelt-OS. Show collected/indexed/current-run/error separately; support Collect now, Pause/Resume, reconnect, retry and disconnect with keep-data/delete-data choice. A saved-but-inactive source remains visibly inactive. Retain safe drafts after errors, guard dirty navigation, prevent duplicate submissions and show actual server validation time. OAuth consent may open the provider and return, but no ordinary task requires another administration UI.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"steps":["connect","scope","collect"],"state":"saved_not_active","primary_action":"retry_activation"}
```

- [ ] **R04.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R04.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Complete setup without opening n8n, dirty-form cancellation, narrow/mobile layouts, unknown timestamps, credential retention and disabled unsupported providers.

## Task R05: Direct OmniRoute setup and AI settings

**Phase/dependencies:** After R01; supplements P03; consumed by P06.

**Files and responsibilities:** Modify: core/model_gateway/{client.py,policy.py,schemas.py}, modules/model_gateway/routes.py, modules/settings/{models.py,schemas.py,routes.py}, apps/web/src/modules/settings/models.tsx, apps/web/src/modules/settings/privacy.tsx, pyproject.toml, uv.lock and docs/model-compatibility.md. Create: apps/web/src/modules/settings/ai-settings.tsx.

**Interfaces — consumes/produces:** Draft connection validation, capability-specific probes and model discovery/manual IDs use backend APIs. AI settings return endpoint, masked credential presence, chat/brief model IDs, configured web-search provider and advanced embedding/privacy/budget/history options.

- [ ] **R05.1 — Trace and implement the owning production behavior.** Adopt server-side OpenAI SDK at the existing ModelGateway client boundary, preserving routing policy, provider/model identity, concurrency and index-generation contracts; do not create a parallel client. Validate draft without persistence, redact failures and require explicit Save. Discovery does not establish streaming/tools/embedding support. Unpermitted embeddings leave lexical search usable. Configure web-search provider credential and allowed destinations through the same Settings workspace; reasoning, embedding and search egress grants remain separate. No Ollama or automatic external fallback.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"ui_label":"Ommi Router","integration":"OmniRoute","chat_model":"configured-model-id","semantic_available":false}
```

- [ ] **R05.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R05.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Draft probes do not persist, secret replacement/removal, manual model IDs, gateway capability mismatch, budget exhaustion and source egress restrictions.

## Task R06: Shared SSE dashboard transport and recovery

**Phase/dependencies:** After R01 and existing durable outbox; before R11; reuse in P06-T2.

**Files and responsibilities:** Create: core/realtime.py, core/realtime_routes.py, apps/web/src/core/realtime-provider.tsx. Modify: core/events.py, apps/api/main.py, modules/ingestion/dispatcher.py, apps/web/src/core/query-provider.tsx, apps/web/next.config.ts and docs/deployment.md. Generate a durable replay-event migration if existing outbox cannot supply replay safely.

**Interfaces — consumes/produces:** GET /api/v1/realtime/events emits authenticated SSE with ordered replay IDs; GET /api/v1/realtime/snapshot returns authorized revision/watermark. Event types source.changed,ingestion.changed,knowledge.changed,dashboard.changed,notification.changed. P06 keeps its own run-scoped /responses/{id}/events stream.

- [ ] **R06.1 — Trace and implement the owning production behavior.** Publish committed changes through outbox; use one dashboard stream per view and identifier/status payloads rather than full private documents. Establish a snapshot watermark and replay after it to cover the snapshot/subscription race. Validate Last-Event-ID, bound replay retention/buffers, deduplicate client events, heartbeat and clean up disconnected subscriptions. Expired/revoked sessions stop access; no credentials in URLs. Replay expiry emits resync_required then refetches snapshot; it never silently claims full replay. Configure proxy nonbuffering/timeouts. Refetch affected TanStack Query keys and queue reading updates.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"id":"1042","event":"source.changed","data":{"source_id":"uuid","revision":3}}
```

- [ ] **R06.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R06.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Snapshot race, gap/expired cursor, duplicate/reordered deliveries, Redis loss, slow clients, session revocation, proxy buffering and independent API/stream health.

## Task R07: AnythingLLM chat port and minimal drawer/full Chat

**Phase/dependencies:** Alongside P06-T3 after P06-T1/T2 and R05/R06.

**Files and responsibilities:** Create: docs/anythingllm-port.md, OSS_USED.md, apps/web/src/app/chat/page.tsx and apps/web/src/modules/chat/chat-page.tsx. Modify planned P06 chat components/controller and docs/DESIGN_SYSTEM.md only when port integration needs a documented contract. Reuse P06 files rather than building a second chat stack.

**Interfaces — consumes/produces:** ChatContext supports general/document/entity/day plus selected item identities and versions. One ChatSession/conversation/run powers drawer and /chat. Drawer only New chat,messages,composer/send-stop,close; full page owns history,context,citations,activity,approvals and web-search controls.

- [ ] **R07.1 — Trace and implement the owning production behavior.** Clone upstream AnythingLLM at a recorded revision, review license/notices, port only needed chat source into the owning frontend module and track original paths/modifications in OSS_USED.md. Adapt upstream transport/auth/state to Umwelt-OS P06 APIs and shadcn; no iframe, standalone AnythingLLM deployment or copied provider secrets. Use large right Sheet, full mobile, focus return and in-memory private drafts. Multi-item Ask AI carries exact versions and permission-checked citations. History opens existing IDs; opening full Chat never starts a duplicate thread. Closing does not Stop. Full page uses configured permitted web search through registered tools.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"conversationId":"uuid","context":{"kind":"selection","items":[{"sourceId":"uuid","documentId":"uuid","documentVersionId":"uuid"}]}}
```

- [ ] **R07.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R07.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Port provenance, history/new/open, selection changes during generation, mobile focus, Stop versus close, replay without duplicate send and citations reopening exact versions.

## Task R08: MCP client/server, grants and collection adapter

**Phase/dependencies:** Alongside P07-T1 after R03/R04; agents/actions use P07-T2/T3.

**Files and responsibilities:** Create: modules/tools/mcp_client.py, modules/tools/mcp_server.py, modules/tools/mcp_schemas.py, modules/connectors/mcp.py, apps/web/src/modules/sources/mcp-editor.tsx. Extend planned modules/tools/registry.py, modules/tools/dispatch.py, connector catalog/routes, credential storage and app composition. Generate MCP grant/connection migration.

**Interfaces — consumes/produces:** Remote transport Streamable HTTP; local stdio is administrator allowlisted. POST /api/v1/mcp/connections/{id}/discover returns tool/resource schema revisions; connection grants explicitly select capabilities. Scoped inbound clients call Umwelt-OS MCP tools via /api/v1/mcp with independent authentication/revocation.

- [ ] **R08.1 — Trace and implement the owning production behavior.** Use maintained MCP SDK; pin compatible protocol/auth versions at execution. Draft checks do not grant every discovered tool. Verify schemas, timeout, current connection/module/grant state, source scope, egress and approval at dispatch. Changed schemas require review. Do not launch arbitrary commands from UI/model input. Expose public Search/Knowledge tools only when implemented. Inbound clients receive explicit scopes, never owner privilege by default. MCP collection adapter declares record identity, normalization, provenance, pagination/history limitations; n8n-backed schedule calls the existing protected collection API. Disable stops new calls and retains data unless deleted.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"transport":"streamable_http","allowed_tools":["search"],"allowed_resources":[],"collection_enabled":false}
```

- [ ] **R08.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R08.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Malicious endpoint, schema drift, revoked grants, inbound isolation, stdio command denial, timeout cleanup, collection duplicates and approved/uncertain external effects.

## Task R09: Application shell, user menu, theme and locales

**Phase/dependencies:** Before R04/R07 UI finalization; supplements P08.

**Files and responsibilities:** Modify: apps/web/src/core/app-shell/workspace-shell.tsx, apps/web/src/core/query-provider.tsx, apps/web/src/core/module-registry.ts, apps/web/src/app/globals.css, apps/web/src/app/login/page.tsx. Create: apps/web/src/core/i18n.ts, apps/web/src/modules/account/preferences-dialog.tsx, apps/web/src/core/app-shell/connection-footer.tsx. Extend modules/settings schema/routes for persisted owner preferences.

**Interfaces — consumes/produces:** Main navigation Dashboard/Chat/Settings. Owner preferences {theme:light|dark|system,locale:en-us|vi-vi,timezone}; server persistence with revision. Formatting locales en-US/vi-VN. User menu owns account/appearance/language/sign-out.

- [ ] **R09.1 — Trace and implement the owning production behavior.** Use shadcn/Radix family and semantic tokens. Dialog previews reversibly; Save persists and Cancel restores. Translate UI and accessibility labels without auto-translating source content. Shell is full viewport width/min-height with document scroll, safe areas and no horizontal overflow. Header logo left/user right; footer derives separate API and SSE state including expired-session. Move old Sources/Knowledge/Agents/Automation/System entries into their owning detail/context/advanced Settings surfaces, keeping existing private routes guarded. No fourth top-level Settings group.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"theme":"system","locale":"vi-vi","formatting_locale":"vi-VN","navigation":["dashboard","chat","settings"]}
```

- [ ] **R09.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R09.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Preference reload/cancel, system-theme change, missing locale key, mobile keyboard/safe area, footer transitions and session expiry.

## Task R10: Dashboard/group/gadget persistence and presets

**Phase/dependencies:** Alongside P08-T4 after R01; R11 consumes this task.

**Files and responsibilities:** Create: modules/dashboard/layouts.py, modules/dashboard/schemas.py, modules/dashboard/gadgets.py. Extend planned modules/dashboard/models.py, modules/dashboard/routes.py and public providers. Create apps/web/src/modules/dashboard/api.ts. Generate dashboard/group/gadget layout migration.

**Interfaces — consumes/produces:** CRUD /api/v1/dashboards and gadget definitions; PUT /api/v1/dashboards/{id}/layout takes expected_revision,breakpoint,items[{instance_id,x,y,w,h}]. GadgetDefinition {renderer,source_ids,scope,filters,highlight_rules}; instance references definition and group. Preset preview resolves existing sources and missing-access warnings.

- [ ] **R10.1 — Trace and implement the owning production behavior.** Persist multiple named dashboards/groups, reusable definitions and separate desktop/mobile layouts. Validate integer coordinates, width max 20, renderer minimum sizes, overlap and instance ownership; reject stale revision 409 and save atomically. Never start collectors from layout/gadget saves. Preview replacement before overwriting; default to create another dashboard. Presets include overview/technology/finance/personal and world/tech/finance/commodity/happy/energy with explicit missing capabilities.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"expected_revision":2,"breakpoint":"desktop","items":[{"instance_id":"uuid","x":0,"y":0,"w":8,"h":6}]}
```

- [ ] **R10.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R10.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Bounds/overlap/minimum size, stale tab save, source disable, atomic rollback, preset replace/cancel and separate mobile layout.

## Task R11: Dashboard edit interaction and reading stability

**Phase/dependencies:** After R10/R06/R09; replaces fixed Today layout in P08-T4.

**Files and responsibilities:** Create: apps/web/src/modules/dashboard/dashboard-page.tsx, dashboard-grid.tsx, layout-editor.tsx, gadget-frame.tsx, preset-picker.tsx under the same directory. Modify authenticated root wrapper, planned widget-registry.tsx, date-selector.tsx and chat controller.

**Interfaces — consumes/produces:** View/edit draft state; drag title=move,edge/corner=resize. Commands move/resize/add/remove/save/cancel/undo/redo operate on R10 integer rectangles. Persisted layout revision stays separate from reading state and incoming data.

- [ ] **R11.1 — Trace and implement the owning production behavior.** Select a maintained compatible grid dependency only if needed for snapping/collision/resize/keyboard; record it in OSS_USED.md. Hide grid/handles in view; show proposed drop and live size, predictable displacement, per-gesture cancel and keyboard/mobile alternatives. Undo/Redo update draft only. Dirty switch/navigation offers Save/Discard/Stay; failed save retains draft. Expand preserves instance filters/selection/scroll/layout. Every content block including map/highlights/watch rules/tasks/brief is a gadget. Queue N new items without jumping or moving layout; distinguish unread/rule-match/severity with text.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"mode":"edit","dirty":true,"columns":20,"desktop_mobile_layouts":"separate","incoming_items_action":"queue"}
```

- [ ] **R11.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R11.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Drag/resize collision and cancel, keyboard/Undo/Redo, failed Save and stale revision, dirty dashboard switch, expanding while streaming and no reading jumps.

## Task R12: Reusable gadget templates, rules and finance charts

**Phase/dependencies:** After R10/R11 and P08-T1/T2/T3; supplements P08.

**Files and responsibilities:** Create apps/web/src/modules/dashboard/gadgets/{news-feed.tsx,telegram-feed.tsx,text-panel.tsx,table-panel.tsx,video-panel.tsx,finance-chart.tsx,personal-context.tsx}; modules/dashboard/highlights.py; apps/web/src/modules/settings/gadget-editor.tsx. Extend gadget registry and source-scoped read/save APIs.

**Interfaces — consumes/produces:** Renderer descriptors declare accepted data shapes,minimum sizes,permissions and source capabilities. Rules return match reasons/severity; read/bookmark state is per owner. FinanceSeries carries timestamp,value,unit,currency,provider_delay metadata where known.

- [ ] **R12.1 — Trace and implement the owning production behavior.** Provide compatible source/template selection, filters and explainable highlight rules with rule-specific notifications. Use Recharts via shadcn Chart for market series. Telegram renderer accepts one/many channels, identity/published/collected/edited timestamps, media placeholders and multi-item Ask AI; Umwelt-OS read state is not Telegram receipt. Text/table/video render only authorized data; sanitize source markup and validate media URLs. Loading/empty/stale/delayed/quota/error remain distinct. Charts respond to container size; no invented price/delay metadata. Map layers belong to map gadget editor.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"renderer":"telegram_feed","source_ids":["channel-source-1","channel-source-2"],"highlight":{"keywords":["AI"],"show_reason":true}}
```

- [ ] **R12.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R12.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Template incompatibility, cross-source permissions, unsafe content/media, highlight reasons/read state, missing finance units and responsive charts.

## Task R13: News/social/research provider adapters and Telegram collection

**Phase/dependencies:** During P09 after R03/R04; GitHub P09-T1–T4 remain.

**Files and responsibilities:** Create: modules/connectors/providers/{telegram.py,feed_catalog.py,social.py,research.py}, apps/web/src/modules/sources/provider-scope.tsx, docs/connectors/provider-catalog.md. Extend catalog, registry, protected webhook routes and packaged n8n workflows only for implemented capabilities.

**Interfaces — consumes/produces:** Required configured collection modes retain validate/sync/normalize/health. Provider manifests enumerate all section 165/166 sources and distinguish implemented,requires_credentials,unsupported_operation,planned; capability gates are visible. Normalized social records retain provider/channel/message/thread IDs and version/observed times.

- [ ] **R13.1 — Trace and implement the owning production behavior.** Implement feed-backed Google News/YouTube/arXiv/Hugging Face/GitHub Release integrations where the chosen official feed/API supports the scope, reusing RSS/REST receipt contracts. Add Telegram Bot API/provider adapter for explicitly authorized channels; support bounded available history and received edits without assuming arbitrary-channel access or inferring deletion during outage. Add provider-specific Reddit/Hacker News/Mastodon/Bluesky/X/Vietnamese-press adapters only when endpoint/license/auth/quotas are established; otherwise catalog them visibly as planned/unavailable, never advertise generic REST as completed support. Credential and schedule setup stays inside Umwelt-OS. Preserve source policy and source-version citations.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"provider":"telegram","capabilities":{"incremental":true,"history":"provider_dependent","deletion":"not_guaranteed"},"availability":"requires_credentials"}
```

- [ ] **R13.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R13.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Provider schema/pagination, Bot API channel access, available history boundaries, edits/outage, quotas and no false completion of catalog-only sources.

## Task R14: Structured world-data adapters and observation contracts

**Phase/dependencies:** During P09 after R03 and R12; supplements finance/weather/research catalog.

**Files and responsibilities:** Create: modules/connectors/providers/world_data.py, modules/knowledge/observations/{models.py,schemas.py,public.py,routes.py}, apps/web/src/modules/dashboard/gadgets/weather-panel.tsx. Extend finance renderer/catalog and ingestion public mappings. Generate observation migration.

**Interfaces — consumes/produces:** Observation {source_id,external_id,version,observed_at,published_at,location?,metric,value,unit,currency?,provider_delay?,raw_ref}. Bounded query API returns authorized series by metric/source/date/region/symbol. All raw data enters existing ingestion before observation mapping.

- [ ] **R14.1 — Trace and implement the owning production behavior.** Use provider-declared schemas and units for configured finance/stocks/crypto/commodities/macro/weather/disaster/climate/traffic/CVE/government/research APIs; no arbitrary model-generated measurements. Provide explicit API-key/symbol/region/metric scope editor. Keep PostgreSQL observation storage and dedupe/revision policy, not a new time-series service. State unimplemented/provider/license gates for unavailable catalog entries. Retain original units/currency/time and quality/missing-value metadata; formatting is not unit/currency conversion. No future health/personal finance/IoT adapter is claimed live because its catalog entry exists.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"metric":"price","value":123.4,"unit":"currency","currency":"USD","provider_delay":null,"location":null}
```

- [ ] **R14.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R14.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Series duplicate/edit, missing units, timezone boundaries, delayed/unknown quotes, provider access errors and source deletion propagation.

## Task R15: Dual map engine and evidence-based intelligence gadgets

**Phase/dependencies:** After P04/P05, R12/R14; during P09.

**Files and responsibilities:** Create: apps/web/src/modules/dashboard/gadgets/{globe-map.tsx,flat-map.tsx,intelligence-panel.tsx}, apps/web/src/modules/dashboard/map-layers.ts, modules/news/correlation.py, modules/connectors/providers/cii.py, docs/intelligence-methods.md. Extend public event/observation APIs and gadget configuration.

**Interfaces — consumes/produces:** Shared MapLayer descriptor {id,source_ids,geometry_kind,enabled,availability}; globe.gl and deck.gl consume identical layer identity/scope. Correlation result carries signal/evidence IDs,time window,method version,uncertainty. CII result carries method_version=v8,country,score?,band?,movement_24h?,as_of,availability.

- [ ] **R15.1 — Trace and implement the owning production behavior.** Implement globe.gl and deck.gl as client renderers, lazy-loaded and sized by gadget container. Preserve layers/filter/selection across map switch. Build bounded military/economic/disaster/escalation co-occurrence/correlation from recorded evidence with declared method and uncertainty; correlation is not causation. CII must use verified v8 method/data/license and specified 31 Tier-1-country coverage; absent evidence returns unavailable/null, not guessed scores. Record the exact permitted source/method before enabling CII collection. Map layers and variants are gadget/preset settings, not permanent dashboard regions.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"method_version":"v8","score":null,"band":null,"movement_24h":null,"availability":"method_data_license_unverified"}
```

- [ ] **R15.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R15.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Layer parity, resize/device WebGL unavailable state, map selection citations, sparse/conflicting signals, missing CII method/license and exact country coverage.

## Task R16: Operations, OSS inventory, onboarding and release reconciliation

**Phase/dependencies:** Across P11/P12 after all owning tasks; deferred validation starts only after code closure.

**Files and responsibilities:** Modify: planned observability/backup/onboarding UI, README.md, OSS_USED.md, docs/deployment.md, docs/IMPLEMENTATION_STATUS.md, docs/release-checklist.md, docs/performance-report.md, docs/security.md, docs/ARCHITECTURE_DECISIONS.md and EXECUTION.md. Extend production deletion/backup hooks for identities,credential references,MCP grants,replay events,dashboards and observations.

**Interfaces — consumes/produces:** Operations/retention/privacy/backup live under relevant advanced sections of the three Settings groups; account preferences remain user-menu-owned. Release record separates code/build/review,behavioral acceptance,live-provider activation and target-host capacity.

- [ ] **R16.1 — Trace and implement the owning production behavior.** Complete source/gateway/gadget onboarding to Dashboard, no mandatory local model or third-party admin UI. Maintain module disable/data deletion across derived stores and connections. Backups include protected keys/reference dependencies and required n8n/graph state; restores use separate disposable instance. Record actual OSS/upstream revision/license/notices/local paths/modifications and distinguish reference-only WorldMonitor/GOIES from reused code. Preserve existing Phase0 evidence, annotate changed auth/UI needing deferred regression. Update final docs only from commands/evidence actually obtained.

Contract shape (illustrative IDs/values, not credentials or production observations):

```json
{"code_build_complete":false,"behavioral_acceptance":"deferred","provider_activation":"unverified","target_capacity":"unmeasured"}
```

- [ ] **R16.2 — Build.** Run `./scripts/dev.ps1 build` on Windows or `make build` on Linux/macOS; fix production build failures. No tests, lint, standalone typecheck, runtime probes or acceptance checks.
- [ ] **R16.3 — Review and record.** Complete independent source review, fix material findings and rebuild affected deliverables. Record actual files/contracts, exact build result, dependency revision and unresolved gates in `EXECUTION.md`; report task completion immediately. Run GitNexus change detection before scoped commit. Preserve unrelated edits; phase merge follows its completed code/build/review gate.

**Deferred acceptance — not executable during code stage:** Full upgraded product, OAuth/SSE/MCP recovery, secret-safe backup/restore/deletion, onboarding accessibility, license notices and measured 2-core/8-GB workload.

## Deferred validation stage

Start only when all original phase production tasks and R01–R16 code/build/review are complete. Catalog-only providers remain explicitly planned/unavailable and are not counted as implemented provider coverage. Missing mandatory live evidence keeps release acceptance open.

Create/update tests only here, using existing pytest/disposable PostgreSQL/Playwright infrastructure. Planned files:

| Owning tasks | Deferred artifacts and proof |
| --- | --- |
| R01 | `tests/test_module_boundaries.py`; explicit approved projections, no private domain writes; build and deferred CI modes |
| R02 | `tests/integration/test_google_auth.py`, `tests/e2e/google-login.spec.ts`; linked owner, callback validation/replay and lockout; controlled OIDC provider plus separate real Google activation |
| R03/R04 | `tests/integration/test_connector_provisioning.py`, `tests/e2e/connector-settings.spec.ts`; partial activation/retry, disable fencing, OAuth/scopes and in-app setup |
| R05 | Existing gateway/search tests plus `tests/integration/test_gateway_settings.py`; SDK parity, privacy/fallback, drafts and real capability evidence |
| R06 | `tests/integration/test_realtime.py`, `tests/e2e/realtime-recovery.spec.ts`; committed event replay, snapshot race, bounded recovery and auth expiry |
| R07 | Existing P06 tests plus `tests/e2e/chat-history.spec.ts`; ported chat, exact citations, thread/draft sharing and stream/cancel |
| R08 | `tests/integration/test_mcp.py`; inbound/outbound grants, malicious tool/schema/resource, transport failure and collection semantics |
| R09–R12 | `tests/e2e/dashboard-layout.spec.ts`, `tests/e2e/preferences.spec.ts`, `tests/integration/test_dashboards.py`; edit/save/undo/mobile, rules and renderer permissions |
| R13/R14 | `tests/integration/test_provider_adapters.py`, `tests/integration/test_observations.py`; pagination/edits/units/quota and source deletion; controlled/live providers separately |
| R15 | `tests/e2e/maps.spec.ts`, `tests/test_intelligence_evidence.py`; layer parity, uncertainty, exact CII method/coverage or unavailable state |
| R16 | Existing P12 full-product/upgrade/recovery acceptance plus `tests/integration/test_reconciliation_backup.py`; keys/grants/layouts/replay/observations restore and measured host capacity |

Run focused owning suites first, then integration/E2E and the existing lint/typecheck/full-test/build commands. Use disposable projects only for destructive checks. Independent review resolves findings at the owning module and reruns affected validation. Record exact commands, failures and gates; no mock replaces live provider, graph compatibility or target-host capacity evidence.

## Coverage and external gates

| Requirement | Task owner |
| --- | --- |
| AI coding-agent instructions, module ownership, CI stage separation | R01 |
| Google login; separate Gmail permissions | R02 |
| All normal connector setup inside Umwelt-OS, no n8n administration UI | R03/R04 |
| Direct OmniRoute + server-side OpenAI SDK + permitted web search | R05/P06/P07 |
| Dashboard SSE and run-scoped chat streaming | R06/P06-T2/R07 |
| AnythingLLM source port, minimal drawer, full Chat history | R07 |
| MCP client/server and optional MCP collection | R08 |
| Header/login/user preferences/footer, shadcn themes/locales/mobile | R02/R09 |
| Multi-dashboard/group/preset, 20 columns, save/cancel, mobile layout | R10/R11 |
| All content as gadget; templates/highlights/Telegram/Recharts | R12 |
| News/social/research/public/personal provider catalog | R03/R13/R14 |
| globe.gl/deck.gl/shared layers, cross-stream evidence and CII v8 | R15 |
| Tasks/goals/stories/trends/brief/memory/agents/automation | Original P04–P10 with updated presentation |
| Privacy/deletion/observability/backup/export/OSS/README | Feature owners, P11/P12 and R16 |

External gates: pinned n8n provisioning API/credential capabilities; Google app/client/callback; provider access/licensing/quotas; actual OmniRoute capabilities and destination guarantees; MCP server capability/auth contracts; Graphiti/backend compatibility; CII v8 method/license/31-country coverage; target OS/architecture and measured capacity; verified separate-instance restore. Credentials never enter task reports.

## Plan self-review

- [x] Sections 165–166 mapped to tasks, files, contracts and deferred acceptance.
- [x] Completed baseline evidence retained; new scope tracked by distinct task IDs.
- [x] No test/fixture authoring or validation commands in implementation steps.
- [x] Unresolved identity/backend/provider choices are explicit gates with safe behavior, not invented completion.
- [x] Source ownership, migration collisions and stale checkpoint handled.
- [x] Existing subagent-driven execution method preserved; no production execution started by writing this plan.

