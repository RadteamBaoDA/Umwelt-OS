# W2a complete scope-conversion contracts

2026-10-07. Architecture handoff for `D:/Project/Umwelt-OS-free-data-pilot`; baseline `512a3fe68895a54f418a85a0ad5e4df3ccad8144`. Authority: W2/W3/W4 briefs and controller rulings R1–R16. This document and `docs/workspace-access-matrix.md` are the only authored files. No production edits, tests, build, lint, typecheck, migration, commit or branch integration. Concurrent W1a/C1 work is outside this report's implementation claim.

## Decisions implementers can use now

1. **One schema/context integrator.** I owns all mapped model declarations, workspace/auth dependencies, shared core boundaries, API/worker composition and the sole W2 migration. Domain workers propose their column/index manifests to I; they never edit another module's ORM or create a competing migration. W1 identity tables are a prerequisite, not part of the original 132-table count.
2. **Required keyword scope on existing public seams.** Preserve existing public function names and detached result DTOs. Add `*, scope: WorkspaceContext` on user-facing calls; dual-purpose source/ingestion/evidence functions use `Scope = WorkspaceContext | InternalJobScope` only where explicitly described below. No omitted/None/default-owner scope, global current workspace, RLS, policy plugin framework or copy of foreign ORM. Pure formatting/hash/schema helpers do not need scope.
3. **Root scope and retained scope, not every child column.** Add `workspace_id` to independent roots and detached journals/tombstones. A mandatory cascading parent is sufficient for ordinary children if every read joins the authorized parent. Where a row stores scope plus live parent, use `(workspace_id,parent_id)` consistency FK; retain deliberately non-cascading detached IDs without fabricating live FKs.
4. **Actor-default Chat only.** Chat, Memory, Agent private activity and Chat checkpoints always belong to actor plus actor's owned default workspace. Selecting an invited workspace does not move or broaden Chat. R6 overrides W4's older shared-document-Q&A wording; cross-workspace shared-document Q&A is excluded from this pilot.
5. **One short workspace lock is the revocation/publication serialization boundary.** Complete lock order below. Never hold it during gateway/provider/graph I/O. Reacquire and revalidate before durable publication and actual ASGI send. Q3 bounds a send to 2 seconds and always releases transaction state.
6. **Members read explicit Document grants and immutable saved Brief grants only.** Source credentials/config, entity graph, tasks/goals/timeline, dashboards/settings, Chat/Memory and aggregation over hidden documents remain unavailable. Membership itself never authorizes content. Shareable Briefs require every captured fact to be story/document-backed, not merely valid displayed citations.
7. **Unconverted routes remain denied to nonbootstrap accounts.** `require_owner` remains bootstrap-operator-only permanently. A route migrates to workspace/account admission only after its entire reachable public/read/enrichment/publish chain is scoped. Flag stays false until W4 code review AND V1 security acceptance; code readiness is not enablement permission.

## 1. Frozen detached contracts

### W1a-to-W1b adoption ruling (source checked after W1a freeze)

W1a currently defines a seven-field `WorkspaceContext(actor_user_id, workspace_id, owner_user_id, role, configuration_revision, membership_revision, is_default)`. **W1b is the sole owner of the atomic transition to the approved four-field context below.** Update `core/workspaces/schemas.py`, `public.py::resolve_workspace_context`, dependencies and every foundation consumer together. Keep resolver's existing parameter `actor_user_id` if useful; its result field is `user_id`. No second context type, compatibility alias, dual constructor or domain dependency on the extra fields. Existing `WorkspaceRead` retains workspace metadata; `AccessFence` below carries configuration revision.

The current `core/auth/public.py::read_active_account` default check becomes: resolved context exists, `role == 'owner'`, `user_id == owner.id`, and `workspace_id == owner.default_workspace_id`. `resolve_workspace_context` must continue proving `Workspace.owner_user_id == actor_user_id` and matching owner membership marker before returning owner role. This preserves default ownership without `scope.owner_user_id` or `scope.is_default`. Domain code uses the canonical four fields from its first edit.

Current W1 table names are verified: `workspaces`, `workspace_memberships`, `workspace_invitations`; W1a defines composite workspace/owner identity and owner-marker membership constraints. W1b owns identity migration/schema/lifecycle changes. Nonidentity schema integrator must not edit these files concurrently.

`core/workspaces/schemas.py` owns these plain immutable values. They contain no ORM objects, request/session references, SQL expressions, lazy relationships or secrets.

```python
from dataclasses import dataclass
from typing import Literal, TypeAlias
from uuid import UUID

@dataclass(frozen=True)
class WorkspaceContext:
    user_id: int
    workspace_id: UUID
    role: Literal["owner", "member"]
    membership_revision: int

@dataclass(frozen=True)
class InternalJobScope:
    workspace_id: UUID
    actor_user_id: int
    membership_revision: int
    source_id: UUID | None = None
    source_generation: int | None = None

Scope: TypeAlias = WorkspaceContext | InternalJobScope

@dataclass(frozen=True)
class AccessFence:
    workspace_id: UUID
    user_id: int
    membership_revision: int
    configuration_revision: int

@dataclass(frozen=True)
class ResourceAccessProjection:
    workspace_id: UUID
    resource_id: UUID
    resource_revision: int
    available: bool

@dataclass(frozen=True)
class DocumentGrantRef:
    document_id: UUID
    grant_revision: int

@dataclass(frozen=True)
class AuthorizedDocumentPage:
    document_ids: tuple[UUID, ...]
    next_cursor: str | None
    fence: AccessFence
```

`InternalJobScope` is an internal trusted value, **not proof simply because a caller constructed it**. Only an owner module that has loaded its durable job/source record may construct it; workspace public admission rechecks active account, default owner relationship, workspace and current owner membership. `source_id` and `source_generation` are both present or both absent. They restrict a source job further; they never widen workspace access. A cleanup receipt may refer to a deleted source: validate the retained scoped operation and exact tombstone instead of requiring a live source. Collectors never act as a member or borrow the HTTP request's selected workspace.

Existing job payload schema retains its exact job ID, claim/generation/token and resource revisions. Add `workspace_id`, `actor_user_id`, `membership_revision`; source jobs also carry source identity/generation. The payload is only a claimed identity: compare all values with the durable row on each admission. Reject missing scope after cutover except recovery via proven legacy lineage. Invalid/missing lineage is terminal quarantine/action-required, never user1 fallback. A long-running poll may enumerate bounded job identities across workspaces internally, but each selected job gets a separate admitted scope before reading content or executing effects.

Freeze these public signatures (existing parameter order otherwise stays stable):

```python
# core/workspaces/dependencies.py
async def require_workspace_read(request: Request, session: AsyncSession) -> WorkspaceContext: ...
async def require_workspace_write(request: Request, session: AsyncSession) -> WorkspaceContext: ...
async def require_default_workspace_read(request: Request, session: AsyncSession) -> WorkspaceContext: ...
async def require_default_workspace_write(request: Request, session: AsyncSession) -> WorkspaceContext: ...

# core/workspaces/access.py / public.py
def can_read_resource(scope: WorkspaceContext, resource_workspace_id: UUID,
                      shared_to_actor: bool) -> bool: ...
async def assert_resource_access(session: AsyncSession, scope: WorkspaceContext,
                                 kind: Literal["document", "brief"], resource_id: UUID,
                                 expected_revision: int | None = None) -> None: ...
async def read_access_fence(session: AsyncSession, *, scope: Scope) -> AccessFence: ...
async def lock_access_fence(session: AsyncSession, *, scope: Scope,
                           expected: AccessFence | None = None) -> AccessFence: ...
async def authorize_internal_job(session: AsyncSession, *, scope: InternalJobScope) -> AccessFence: ...
async def list_document_grants(session: AsyncSession, *, scope: WorkspaceContext,
                               after_id: UUID | None = None, limit: int = 500) -> tuple[DocumentGrantRef, ...]: ...
async def read_resource_grants(session: AsyncSession, *, scope: WorkspaceContext,
                               kind: Literal["document", "brief"], resource_ids: tuple[UUID, ...]
                               ) -> tuple[tuple[UUID, int, int], ...]: ...
```

`read_resource_grants` returns only `(resource_id, grant_revision, resource_revision)` for active grants to the actor, max500 IDs per call. Workspace grant queries know only grants; they do not import Document/Brief ORM or claim source/deletion validity. Resource owner modules supply `ResourceAccessProjection` through their existing public modules (new names `read_document_access_projection` and `read_brief_access_projection`, each `(session, resource_id, *, scope: Scope) -> ResourceAccessProjection | None`). Workspace share lifecycle calls those hooks; document reads call grant-only helpers, avoiding recursive owner authorization. Brief owner separately checks complete captured dependencies. No cross-module ORM/query-builder escape hatch is introduced.

`lock_access_fence` locks/reloads workspace and membership under the ordering in §4, compares expected configuration and membership revisions, and never commits. Auth public code owns account/session validation/locks; workspace code cannot query AuthSession ORM. Account-wide preference calls take `actor_user_id` obtained from `require_account[_write]`, not selected workspace. Operator calls keep `require_owner[_write]`; HTTP callers cannot manufacture an operator scope.

Headers: ordinary requests resolve `X-Workspace-ID`; SSE resolves `workspace_id`. Invalid UUID is400, absent context is400 `workspace_required` except bootstrap user1 mapped to its verified default. Unknown workspace, absent/revoked membership and invisible resource are404; authenticated visible stale revision is409. Default-workspace dependency derives actor default from auth public DTO. If a caller explicitly supplies another workspace to a Chat/Memory route, reject409 `default_workspace_required` rather than silently answering from a different scope. Frontend must send its separate Chat default context.

Owner membership is immutable in role and account binding. Role is never accepted from a body, token claim or queued JSON. Active-account/feature-gate checks apply again on session validation, worker claim, before_send, publication and SSE heartbeat/delivery. Backup admission and normal CSRF remain mandatory for writes; anonymous invitation acceptance uses the separate W1 origin/token/admission contract.

## 2. Minimal query/public conversion and collector preservation

The matrix's root/lineage predicate is the mandatory base predicate in `SELECT`, `UPDATE`, `DELETE`, count, cursor page and eager enrichment; resource ID alone is never authorization. For ordinary children join their owning root before LIMIT/ranking. Compare all supplied IDs within the same scope before mutation, including source+document, dashboard+group+definition, conversation+message+run, task+goal, entity endpoints/evidence, and MCP connection+discovery+grant. Hidden IDs must not affect counts, error detail or partial success metadata.

Existing functions such as `sources.get_connector_source`, `get_source_fence`, `lock_source`, `lock_retained_evidence_source`, `ingestion_lifecycle_projection`, `documents.get_ready_version_ref`, `read_extraction_input`, `upsert_normalized_document`, `ingestion.lock_news_document_ready_event`, `mark_news_document_ready_event_delivered` and `fail_news_document_ready_event` remain in their owner `public.py`. Add explicit scope and preserve their detached DTOs, source generation and caller-owned transaction behavior. A public SQL projection already approved by AGENTS may remain narrowly scoped if its signature takes scope and its emitted SQL is constrained; do not add new generic projection machinery. Existing routes that call an ORM-returning public function may continue inside the owning module; cross-module callers use the existing detached alternative or a narrow new DTO.

Ingestion credential endpoint path/source must agree with the validated token's Source. Receipt recovery loads the ingestion-owned row and derives its scope **before** refetch; accepted collection request UUID and receipt are committed in the same Ingestion transaction (R7). `sources.get_connector_source(session, source_id, *, scope: Scope)` accepts verified internal scope so C1 collectors do not need foreign Source/Workspace ORM or artificial browser sessions. Connector scheduler performs internal bounded enumeration, then calls source public resolution and workspace admission per selected Source. C1a's typed ingestion worker allowlist remains; preserve C2's exact lease/backend/config fences, max5 attempts and provider quota reservations. No runtime signature transition is complete until internal workers, recovery scanners and event consumers are updated together.

Document owner exports `authorized_document_page(session, *, scope: WorkspaceContext, cursor: str | None = None, limit: int = 500) -> AuthorizedDocumentPage`. It streams authorized Document IDs using workspace-root constraints and grant pages plus existing availability/deletion rules. Search owner inserts those IDs into its own query using bounded batches/VALUES (max500 each), filters lexical/vector candidates **before ORDER BY/LIMIT**, then merges per-batch top-k globally with a bounded top-k heap. Do not retrieve an unfiltered top-k and discard hidden hits afterwards. Owner queries can use the already-approved scoped Document projection to avoid enumerating all IDs. Member ranking uses per-document scoring/vector distance; disable global-corpus statistics/reranking that would reveal hidden corpus effects until an authorized-corpus implementation exists. Candidate enrichment, citations and final count/page cursors use the same fence; if changed, return stale/retry rather than a partially rebuilt page. Do not persist an unbounded list of authorized IDs in a session or cache.

Cursor fingerprints include actor, workspace, membership revision, workspace configuration revision, request filters, sort/snapshot and domain generation. Decode under current scope before using offsets. UUID-only locators such as version cursor need parent+actor+workspace binding. If legacy cursor has no scope, reject and require first page after cutover.

## 3. Member Document, News and Brief projection

Document grant is to canonical Document identity and includes its current and retained readable versions while active, subject to existing source/privacy/deletion policy. It does not authorize Source enumeration, configuration, credentials, owner interactions or graph/entity information. Raw downloads, provider snapshots, citation-target, versions and content-derived translation must use the same Document predicate. A member can read authorized documents but cannot mutate owner document interactions in v1; personal interaction endpoints stay owner-only unless a separately specified actor-private interaction contract is implemented.

News clusters are workspace-local. For members, determine authorized versions first, then build story title/excerpt from an authorized representative and report only authorized observation/source counts. Do not reuse a private canonical title, keywords, correlations, trend, relevance explanation or entity names computed with hidden observations. Dedup, pagination and total count occur over this projection. Owner-defined topic configuration and global trend/correlation/CII endpoints remain owner-only for v1; member News navigation uses the safe story projection and no private topic facets. A story with zero authorized documents is invisible, including via guessed UUID. A shared Document does not grant all sibling documents in the same story.

Brief grant binds a saved immutable `DailyBrief` row/revision. Existing capture fields (`evidence_capture_version=1`, status `captured`, positive `evidence_fact_count`, fact_ref/support_index, fact_kind, fact_hash, source/document/version/chunk identity, source generation and exact prompt-input fingerprint) remain authoritative. Owner may retain mixed Briefs. Share grant requires:

1. Complete numbered fact coverage `1..evidence_fact_count`, consistent hash/kind per fact, contiguous support indices and existing capture bounds (max100 distinct evidence references; preserve existing MAX_FACTS).
2. **Every captured fact** is `stories`, lineage `supported`, with nonempty exact document-backed support. Reject tasks, goals, events (including document-backed events in this v1 story-only contract), independent/manual facts, missing/legacy manifests and any unsupported fact—even if it is absent from displayed citations.
3. Every support's canonical Document, immutable version/chunk and source generation resolves under workspace/deletion/privacy checks. Recipient has active Document grant for every dependency. Story support must still match the complete captured title/source/support fingerprint. Do not auto-grant dependencies or silently regenerate member prose.
4. Saved text, annotations and translations are visible only while both Brief grant and **all** captured dependencies remain authorized/current under existing read validity. Dependency revoke/delete/privacy change hides the entire Brief and translations, invalidates cached output, and fences in-flight publication. Return409 `brief_evidence_not_shared` on an otherwise visible failed sharing attempt; invisible reads are404.

R16 repair snapshots exact fact input and generation claim under a short transaction, releases locks for gateway I/O, then compares the complete capture/policy fingerprint and claim to publish. A legacy citation-only Brief cannot become shareable by reconstructing guessed inputs. Member Brief lists cannot disclose hidden revisions, dates, counts or stale text; list only granted currently admissible saved revisions.

## 4. Lock order and revoke/publication fence

All paths first perform nonlocking identity resolution (IDs only), then acquire locks in this order; re-read after locking:

1. Existing instance backup admission registration completes in its own short transaction before domain locks. Global heavy/provider/MCP admission leases remain independent bounded capacity, never authorization.
2. Auth-owned account rows ascending numeric user_id, then auth-session rows ascending token hash. Existing auth owner->session order is preserved. Read/publication uses `FOR SHARE` where available (not KEY SHARE, which permits non-key state updates); auth mutations use UPDATE locks. Account suspension/logout must conflict with these locks. When more than one account is involved in an invite/share, collect actor+target IDs first and lock in sorted order.
3. Existing workspace rows ascending UUID using `FOR UPDATE`. This deliberately serializes only short same-workspace authorization/commit sections. Owner account disable can lock all owned/member workspace rows in sorted order after account locks. No content operation spans workspaces in v1.
4. Membership keys `(workspace_id,user_id)`, then invitation IDs, then share keys `(resource_type,resource_id,member_user_id)`, each sorted. Create/edit checks body `expected_revision`; revoke/remove checks If-Match. Missing revision428, stale409. Effective mutation increments workspace configuration revision and affected membership/share revision, including regrant; an idempotent already-effective mutation does not invent an authorization grant.
5. Sources sorted UUID through Sources public locks, Documents sorted UUID through Documents public locks, then owner-domain roots (stable `(domain,id)` order), evidence/claims/outboxes and finally replay head. Preserve stricter existing local order within a domain. Because the workspace lock is held, same-workspace writers cannot invert competing domain locks. Batch workers finish one workspace transaction before entering another.

No path holding a later lock may call an earlier-lock helper. Discovery of another required identity restarts the short transaction with its complete sorted lock set; do not acquire it opportunistically. Invitation acceptance performs digest lookup without lock, resolves existing account first or stages an uncommitted new account/default workspace, then locks the invited existing workspace and token row and rechecks token/revision/email. New rows in that one transaction are not visible/contended; account/email uniqueness conflict rolls back everything and requires authentication of the winner. Do not commit provision then consume.

`read_access_fence` captures revisions for query/network preparation. `lock_access_fence(expected=...)` verifies current role, membership existence/revision and workspace configuration revision; caller also compares source generations, resource/grant/privacy/config/model revisions, job claim, deletion tombstones and exact evidence fingerprints via owner public hooks. An unchanged membership revision alone is insufficient when a Document grant changed.

Gateway before_send: briefly acquire auth/workspace/resource authorization in the same order, validate the exact outgoing dependency snapshot, release transaction, then transmit. Each gateway retry/fallback repeats it; gateway owns its existing2 transport attempts, callers must not multiply by another2. A revoke after authorization cannot unsend bytes already dispatched; no returned stale result may become durable output or be sent to a client. After remote I/O, take the same locks, compare the captured fence and domain claim, persist output/outbox atomically or discard/cancel as permission loss. Never hold SQL locks through gateway/provider HTTP, graph adapter calls or browser tools.

Final HTTP/SSE/download publication must serialize against revoke too, not only database writes. W4 owns a narrow shared ASGI publication gate supplied with the already computed detached fence/resource refs; it wraps actual `http.response.start` and each nonempty `http.response.body` send. It opens a fresh short transaction, locks auth->workspace, revalidates dependencies, and awaits that one actual send for at most2s; release/rollback in `finally`, including disconnect/cancellation/timeout. Never retain a transaction across stream waits or send an entire unbounded file under one lock. Revoke waits for an already-authorized bounded send, then commits; no new protected body send can start after revoke commits. A precomputed generator/yield check is insufficient. Worker outputs that only commit use the same fence without an ASGI gate. SSE rechecks on each event/replay and15s heartbeat; heartbeat carries no private counters.

Member generic replay receives only document/brief invalidations authorized to that actor. Source/index/graph/dashboard events are owner-only. Simplest v1 member handling: emit a generic `authorized_content_changed` invalidation with no hidden IDs/counts when an authorized projection changes; reconnect obtains a scoped snapshot. Avoid exposing global sequence gaps: I stores replay head per `(workspace_id,user_id)` and replay event PK `(workspace_id,user_id,sequence)`; append only events that actor may see. Epoch identifies that actor/workspace stream, cursor additionally binds membership/configuration revisions. Rotate/reset member epoch on share/membership revision changes; never use global replay as fallback. Preserve MAX_REPLAY_BATCH100, payload16KiB and retention bounds per stream with an instance retention cap to prevent multiplied unbounded storage. This per-principal stream is chosen over adding cursor cryptography/key management.

## 5. Schema and cutover contract

The access matrix lists each of the original132 mapped tables and separately external LangGraph tables. New W1 `Workspace`, membership and invitation tables and W3 grants are additive; I records their actual names after W1 integration, not by guessing an applied schema. One unpublished `p14_workspace_scope.py` follows the actual W1 identity head. C2 collector quota/receipt tables and W3 grants must reserve DDL through I before publication; never mutate an applied migration. All Python model edits, including `news/topics.py::Topic`, `core/realtime.py` mapped declarations and `core/demo_seed.py`, are I-owned. A domain worker can edit non-model behavior in these mixed files only after I's serialized handoff.

Expand nullable root/retained scope -> backfill from proven Source/parent/account lineage -> report orphan table/count and fail -> enforce NOT NULL/check/unique/composite FK -> deploy only same-version workers/API. Source-local identity keys (`source_id,external_id`) remain valid without redundant workspace prefixes. Scope global identities: story identity+algorithm, topic names if unique, active search generation partial unique, Agent profile PK/revisions, recovery cursors, replay stream keys, singleton settings/schedules. Where PK remains owner_id because v1 has one owned workspace, add workspace_id plus composite account/default-workspace consistency rather than duplicating settings under arbitrary selected workspaces.

Detached operations keep scope after canonical deletion (Source purge, Document cleanup/evidence, Agent effect/cleanup, OAuth uncertainty, timeline suppression/audits, temporal allocation/support/operation/dispatch/receipt/change/reconcile journals). For existing detached rows with no live parent, prove legacy scope from a retained receipt/source ID or from the verified singleton database cutover manifest; record that specific provenance. Do not execute an unqualified blanket owner1 update. Ambiguous multi-scope support is a migration failure, not a union grant.

Deliberate instance globals remain global: backup controls/activity/operations, heavy guard, maintenance summary, worker heartbeat/metrics, provider/IP capacity, GitHub webhook receiver/delivery/digest/fanout before Source binding, MCP shared operation limits. Global GitHub fanout pages can hold several source bindings: each binding carries workspace/actor/source generation and each dispatch revalidates separately; never assign a single workspace to a global delivery and authorize its entire fanout. Account OAuth coordinator remains actor-scoped; peers only from the same actor/workspace and grant scope.

LangGraph uses existing UUID `checkpoint_thread_id` as an opaque unique key tied to scoped AgentRun; widening the36-char field to embed IDs is unnecessary. Every saver read/write/resume/delete first verifies the parent run's actor/workspace and exact checkpoint_thread_id. Add/check `checkpoint_ns = 'w:' + workspace_uuid + ':u:' + actor_id` where the saver contract supports it; run mapping remains authoritative. Existing namespace-empty records are migrated/copied under drain or retained under a verified legacy mapping until terminal cleanup, never read by arbitrary supplied thread_id. External `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` content is resource-private; `checkpoint_migrations` is instance metadata. Check actual installed saver schema in V1; do not import external DDL into ORM metadata merely to satisfy count.

Graphiti keeps existing per-source-generation partition UUID group_id. Add workspace lineage to every partition/mapping and authorize **allowed partition IDs before querying**; the effective namespace is `(workspace_id, partition_id)` with global-unique partition IDs, not a rename of every Graphiti group. This preserves exact deletion receipts. Member graph endpoints stay unavailable. Vector generations become workspace-local; index items authorize their chunk/document before rank. New uploads use `workspaces/<workspace_uuid>/documents/<document_uuid>/...`; existing raw_uri references remain unchanged and authorized through Documents. Orphan/backup readers recognize both layouts. Never copy/duplicate blobs merely for key aesthetic changes.

Controlled cutover: enter maintenance admission; stop new writes/jobs; drain or durably fence remote attempts; inventory pending payloads/storage/checkpoints; apply migration; verify lineage/constraints; start same-version API+worker with flag still false; invalidate all sessions; require fresh login. Legacy scope-less jobs resolve only through proven lineage or quarantine. Downgrade refuses nonbootstrap/scoped data that singleton cannot represent, reporting table/count; no silent deletes. Redis is disposable coordination/cache, PostgreSQL remains job authority.

## 6. Disjoint next-wave production ownership

### Immediate dispatch split, superseding the combined I scheduling below

Controller may start **W1b** and **I-schema** concurrently. W1b owns `core/auth/**`, `core/workspaces/**`, `p14_identity.py`, its existing identity configuration/gate callers and any API identity-router registration. It adopts the canonical context above and publishes complete lifecycle/lock helpers. **I-schema** owns only nonidentity mapped declarations listed in the132-row matrix (excluding `core/auth/models.py`), and `infrastructure/postgres/migrations/versions/p14_workspace_scope.py`. It does not edit auth/workspaces, migration env, API/worker composition, `core/config.py` or `.env.example` during W1b. It may edit only mapped-class blocks in mixed `core/realtime.py`, `core/demo_seed.py`, `modules/news/topics.py`; runtime writers wait for handoff on those files. **I-runtime** follows W1b/I-schema and owns the remaining combined I shared runtime files below by explicit transfer. This is one schema/migration integrator with staged responsibilities, not concurrent integrators or multiple heads.

I-schema first deliverable is the complete declarative model/migration change from the matrix, including per-table scope/backfill checks, constraints and downgrade guards. It may proceed before domain public-function edits because all behavior stays gated. It must not claim operational compatibility until I-runtime/domain integration is complete. Use actual `p14_identity` revision identifier as `down_revision` (read the declared revision, not filename); reserve C2/W3 DDL with controller and leave migration unpublished until reservation integration is complete.

Concrete DDL recipe for every matrix row:

* `Add workspace_id` means PostgreSQL UUID, initially nullable for expand/backfill and final NOT NULL, FK `workspaces.id ON DELETE RESTRICT` for roots/retained records, unless an existing intentional cascade parent owns the row. Preserve retained parent IDs without inventing cascading canonical FKs. Add nonunique `(workspace_id, id)` query index, or `(workspace_id, <existing PK columns>)` for non-id keys; add UNIQUE `(workspace_id,id)` only when needed as a composite FK target. Existing PK UUIDs remain unchanged.
* Existing `owner_id`/`actor_id` stays the actor field. Only rows explicitly marked `actor_user_id` and lacking an existing actor field get `actor_user_id INTEGER NOT NULL`; derive it from exact root/account receipt. Root owner/default consistency uses FK `(workspace_id,owner_id)` -> `workspaces(id,owner_user_id)` where owner semantics apply. Member-target notifications and explicit grants use membership/account identity, not the owner composite FK.
* Root list/work indexes start with workspace then existing sort/filter columns, e.g. `(workspace_id,created_at,id)` or `(workspace_id,status,next_attempt_at,id)`, preserving existing due/claim indexes for bounded internal scans. Do not add speculative indexes for every column. Child joins retain indexed parent FK; cross-parent consistency is checked before insert/update; if child carries workspace_id, add composite FK to every scoped live parent whose ID it retains.
* Replace exact singleton identities: `realtime_replay_head` PK `(workspace_id,user_id)`; `realtime_replay_events` PK `(workspace_id,user_id,sequence)` plus matching head identity; `news_recovery_checkpoints` PK `workspace_id` (drop id1 CHECK); `automation_cursors` PK `(workspace_id,name)`; `agent_profiles` PK `(workspace_id,profile_id)`; `agent_profile_revisions` UNIQUE `(workspace_id,profile_id,revision)`.
* `news_story_identities` UNIQUE `(workspace_id,identity_key,algorithm_version)` replaces global clustering uniqueness; `search_index_generations` active partial UNIQUE on `workspace_id WHERE status='active'` replaces global active index. Settings/Memory privacy/Brief schedule keep owner_id PK in v1 and add workspace+owner composite ownership; remove only their owner1 CHECKs. `daily_briefs` revision uniqueness becomes `(workspace_id,owner_id,brief_date,timezone,revision)`; Notifications dedup becomes `(workspace_id,owner_id,dedupe_key)`.
* Existing globally unique resource UUID, provider effect key, credential hash, source+external_id and source+generation keys remain unchanged where already isolated. `github_webhook_capacity`, delivery/outbox receiver uniqueness, backup, maintenance and heavy-guard singletons remain global. Source/Document child PKs do not need gratuitous workspace prefixes.
* Backfill roots from legacy owner's verified default, account-owned rows from owner.default_workspace_id, source rows from scoped Source, children from named scoped parent, detached rows from exact retained receipt or verified singleton cutover manifest. Validate every multi-parent row agrees; abort with table/count for ambiguous/orphan lineage. No schema-owner import of domain ORM in migration; use explicit Alembic SQL/table definitions. Downgrade refuses any scope/nonbootstrap state or duplicate newly scoped natural keys that singleton cannot represent.

Schema integrator records exact generated constraint names/column manifests in its handoff. Domain owners consume these columns and this public contract; they do not independently add migrations. The matrix is sufficient to start root/retained columns now; domain-specific existing uniqueness expressions must be copied from source and changed only by the recipes above, not guessed from names.

All slices first run GitNexus per-symbol impact plus source callsite tracing; graph DI omissions are not a LOW-risk waiver. Named changed production functions/classes get actual contract docstrings. Tests/lint/typecheck remain deferred; controller serializes permitted builds. No slice commits, merges or enables multiworkspace.

| Slice | Exclusive production writer | Consumes / concrete done condition |
| --- | --- | --- |
| **I — context/schema/integration** | All existing `models.py` mapped declarations; mapped blocks in `core/realtime.py`, `core/demo_seed.py`, `modules/news/topics.py`; `core/workspaces/**`; `core/auth/dependencies.py` and auth public scope hooks by serialized handoff from W1; `core/database.py`, `core/events.py`, `core/realtime.py`, `core/realtime_routes.py`, `core/storage.py`, `core/pagination.py`, `core/modules.py`, `core/config.py`, `.env.example`, `apps/api/main.py`, `apps/worker/main.py`, migrations/env + sole W2 migration | Deliver frozen DTO/dependency/admission signatures first, then owner manifests/models. Integrate all replay/event signatures, worker scope gating, storage prefix and route readiness deny defaults. W1b owns identity lifecycle but cannot concurrently edit this seam group; controller serializes. |
| **S — Sources/Connectors/Ingestion** | Non-model `modules/sources/**`, `modules/connectors/**`, `modules/ingestion/**` | Wait C1a completion; scope owner routes, token Source binding, provisioning/OAuth/native/n8n/manual/recovery callbacks, retained purge/receipt DTOs. Preserve C2 job/receipt/lease interfaces and R7–R11. Output scoped SourceFence/ConnectorSource/ReadyDocumentProvenance and source-scoped internal public calls. |
| **D — Documents/Search/Graph** | Non-model `modules/knowledge/documents/**`, `modules/knowledge/observations/**`, `modules/knowledge/temporal/**`, `modules/search/**` | Consume Source DTO + frozen scope; own Document grant projection hooks, raw/download/provider snapshot/citation/export, cleanup/outbox evidence, vector prefilter and temporal partition isolation. Coordinate storage function callsites with I; no Source ORM import. |
| **K — Entities/Relationships/Timeline/Tasks/Goals** | Non-model `modules/knowledge/entities/**`, `modules/knowledge/relationships/**`, `modules/timeline/**`, `modules/tasks/**`, `modules/goals/**` | Owner-only root and cross-reference scope, source/document evidence DTOs, extraction/recovery/cursors, retained audit/suppression. No member entity enrichment. Return complete detached support facts to N. |
| **N — News/Dashboard/Brief** | Non-model `modules/news/**`, `modules/dashboard/**`; `news/topics.py` behavior only after I hands off mapped block | Workspace-local clustering/recovery, authorized member story projection, saved Brief shareability/read hooks over every captured fact; schedules scoped. R16 generation claim/lock repair is an explicit follow-on owned by N under W4, not silently marked done by scope columns. |
| **P — Private Chat/Memory/Agents** | Non-model `modules/chat/**`, `modules/memory/**`, `modules/agents/**` | Actor/default-workspace root, retained run/effect/checkpoint/history/citation/stream/cleanup scope; no invited-context retrieval. Consume ToolExecutionPrincipal from T; Q1 durable dispatch, Q2 latest20 logical history, Q3 bounded send remain separately tracked sequential release repairs after scope conversion. I owns shared ASGI publication wrapper; P integrates Chat callsites. |
| **T — Tools/Automations/Notifications** | Non-model `modules/tools/**`, `modules/automations/**`, `modules/notifications/**`, `core/tools/**` | Workspace+actor ToolExecutionPrincipal, per-connection exact runtime registry bindings, inbound bearer/source restrictions, browser callback durable identity, automation trigger/schedule/action scope and notification recipient isolation. Preserve global admission slots. |
| **O — Settings/Gateway/Operator/Export** | Non-model `modules/settings/**`, `modules/model_gateway/**`, `core/model_gateway/**`, `modules/export/**`, `modules/backup/**`, `modules/observability/**`, `core/system/**`, `core/telemetry.py`, `core/langfuse_export.py` | Split account preferences/onboarding from workspace AI/privacy/retention/module config and instance control. Scope capability caches/gateway settings; before_send adapter and export chunk fence. Backup/system/observability operator-only. Process registries/config may not mutate with last-request workspace. |

Files in a directory ownership glob exclude every model declaration and the explicitly I-owned files above. `schemas.py`/public DTOs belong to their domain. Each domain updates its **own caller** when calling another public module; callee owner publishes signature/DTO first. I updates app composition/shared imports, not domain query internals. S and C2 cannot edit connector code simultaneously; P and Q1–Q3 cannot edit Chat simultaneously; W3 modifies workspace sharing through I plus D/N hooks, never by a third writer in D/N files. W5 owns frontend context/client/store/cache changes separately after these backend contracts.

Practical dispatch order: I publishes schema/context skeleton -> S and O -> D/K/T in bounded independent slices -> N/P -> I integration + W3 grants -> W4 network/publication + R16/Q1–Q3 -> W5 frontend -> V1–V5. More parallelism is allowed only when providers have published detached signatures and file ownership remains disjoint. No domain becomes nonbootstrap-admissible merely because its local build passed.

## 7. Acceptance boundary and handoff checks

Code-ready requires: every matrix row assigned and converted or explicitly instance-only; no missing-context default; every route and worker reachable dependency converted; typed scope in retained outboxes; same-context joins/unique constraints; source-generation/privacy/claim fences preserved; public-only cross-module access; review of singleton guards/owner1; independent review; controller build. None is a runtime claim.

V1 later compares live SQLAlchemy metadata, Alembic tables and external saver tables against the matrix, imports the real application route graph including mounted MCP, and checks worker registrations. Exercise two owners with same external_id/title/query/cursor/alias; invited member with no shares; raw/provider snapshots; current and historical Document versions; full captured Brief facts and dependency revoke; before_send retry; stale job/source generation; checkpoint guessing; graph partition/query prefilter; opaque/per-principal replay cursors; actual2s ASGI-send race; account/flag disable; instance backup/export; legacy and clean bootstrap migrations; rollback rejection; mixed-version/payload quarantine. V4/V5 cover provider/capacity/restore and slow-client/crash/history cases. No tests were created or run in W2a.

Static inventory is finite and executable but is not deployed-schema proof. Read-only AST/source inventory found original132 declarations across28 files; dynamic MCP registrations and collector service routes are included separately. GitNexus query returned definitions but no matching processes for the collector-public query; source tracing supplied the actual contract details. No production symbols were edited, so no symbol impact edit claim is made.
