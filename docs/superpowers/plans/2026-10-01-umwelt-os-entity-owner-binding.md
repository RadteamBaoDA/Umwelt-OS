# P04-T1 owner binding ruling — actual R06 replay and document evidence

Date: 2026-10-01. Bounded owner decision for P04-T1 on controller-supplied integrated base `6f7d50a`, tree `C:/Users/doana/.codex/worktrees/bbd-p04-entities/Umwelt-OS`. Existing P04 entry/migration rulings and T1–T4 scope stand. This fills the pending R06 binding; it is not a phase replan or production/runtime approval. API additions below are proposed T1 contracts, not claims they already exist.

## 1. Minimal consumed replay branch

**Decision:** Extend the existing typed `core.realtime.KnowledgeChanged`/`knowledge.changed` payload with one **`scope="graph"`** branch and two optional UUID fields, `entity_id` and `relationship_id`. Graph validation requires **exactly one** of those two fields. Keep the existing `schema_version=1`, `type`, and boolean `deleted` (false default). Do not add a new replay table, notification registry, transport, ARQ dispatch event or model-job framework.

Graph branch examples describe actual mutation results, with identifiers obtained from persisted rows:

```text
entity create/update/delete/alias change:
  knowledge.changed, scope=graph, entity_id=<actual entity UUID>, relationship_id=null
relationship create/update/delete/support change:
  knowledge.changed, scope=graph, entity_id=null, relationship_id=<actual relationship UUID>
```

Use `deleted=true` for deletion of the identified row; otherwise false. An identifier captured before deletion remains its real identity. Alias changes identify their parent entity, rather than inventing a document or standalone alias notification ID. These are invalidation hints, not authoritative entity DTOs or an audit record.

The graph validator forbids `source_id`, `document_id`, `version`, and all index generation/status/count fields. The existing source/index validators must also forbid the two new graph fields. Preserve old source defaults and index requirements. A graph mutation with attached evidence still uses this graph branch: its document was not mutated. Add one small typed constructor at the existing owner if helpful; no generic future event interfaces.

Manual entities and relationships can lack any document/source. Never select an arbitrary supporting source, fabricate a UUID, reuse the replay cursor/event record as a domain identifier, or send a graph update through the source branch merely to make validation pass. The preserved draft's plain commits must become actual atomic domain-write/replay finalization where they mutate visible graph state.

## 2. Actual provider/query ownership

In `apps/web/src/core/realtime-provider.tsx`, extend `KnowledgeEvent` and its **existing** `knowledge.changed` handler. Validate the graph branch's one UUID and forbidden fields, then directly invalidate the entity/relationship roots and the actual search root:

```text
['entities']
['relationships']
['search']
```

Return from that branch **before** `queueDocumentUpdate`. Do not touch document counts, queued document IDs, unknown-document revisions or document refresh-required flags for graph-only mutations. Malformed graph payload uses the existing controlled resync path. Retain the handler's existing stream/attempt/auth-generation ownership wrapper and cursor behavior.

The current base has real `['search', ...filters]` queries and document query keys; it has no entity/relationship UI query owner yet. T1 introduces the branch for its actual manual mutation producers. P04-T4 must place its real entity list/detail/alias/evidence/neighbor/resolution queries under `['entities', ...]` and its relationship list/detail/evidence queries under `['relationships', ...]`, so these prefix invalidations have concrete consumers. An entity/relationship update must refetch/reset affected graph continuation, rather than appending onto a changed page. Broad invalidation of these two bounded owner roots is sufficient; endpoint-ID arrays, revisions, a query registry and a new query-key module are unnecessary for T1. Add named keys to the existing frontend owner API only when T4 creates the consumers.

Also invalidate those graph/search roots for a real source-scope `knowledge.changed` with `deleted=true`, before preserving its existing document notification behavior. Actual document deletion/source purge can remove graph support, and those producers already carry real document/source IDs. This supplies cleanup notification without enumerating every affected graph row or exceeding `MAX_REPLAY_BATCH`. Do not send one graph event per row of an unbounded purge. Existing snapshot/resync invalidation must include the graph roots once this branch is bound, so reconnect does not retain stale graph data.

`EntityUpdated`/`KnowledgeChanged` in the original plan are domain concepts; the concrete browser binding here is the existing versioned replay notification. Do not insert these hints into `EventOutbox`/the ingestion worker fallback, which expects work payloads. T2's document-ready extraction work retains its separate deliberate dispatcher contract.

## 3. Document-owner exact evidence DTO needed now

Implement/consume **`modules.knowledge.documents.public.read_evidence_refs(session, refs, *, for_write=False)`**, where `refs` are exact `(document_version_id, chunk_id)` pairs, hard maximum 100, rejecting duplicate inputs for a mutation. Return detached records keyed by the exact pair:

```text
document_id, source_id, document_version_id, version_number,
chunk_id, observed_at, title, canonical_url, excerpt
```

The document owner joins/checks chunk -> exact version -> exact document -> existing source. A chunk belonging to a different requested version must not pass because both IDs happen to appear somewhere in two independent IN lists. Use immutable version observation time; client `observed_at` cannot substitute for the owner's value. Bound excerpt to 1000 characters without exposing full metadata/raw storage. RN1's actual `NormalizedVersionProvenance` stores immutable version title/URL, accepted hash/rank and timing: the document owner should select that version's stored provenance when present after integration. For legacy/manual revisions without that snapshot, current `Document.title/canonical_url` remain current metadata and must not be labeled historical; distinguish that fallback in the DTO/consumer instead of inventing a snapshot. This contract adds no normalization schema and exposes no foreign provenance ORM. P04 must preserve RN1's real immutable provenance fields during combination.

Owner-authenticated reads follow existing library access policy, including retained historical evidence; do not invent an active-only rule that makes ordinary source archive erase retained support. Physically deleted/missing/mismatched evidence is unavailable. Every write attaching evidence must match every requested pair to a valid result or reject atomically; a read of historical invalid support must not return a cached deleted excerpt. Entity/relationship owners persist their own membership/support/provenance, using the validated DTO's document/source identities for cleanup.

For `for_write=True`, the document owner obtains **sorted source locks first, then sorted document locks**, and revalidates identities/membership after lock acquisition. Unlocked identity hints may discover the lock set; hints alone are not acceptance. Retain those locks through the caller's commit. This prevents accepting evidence while source purge or single-document deletion removes it. Call this owner operation before graph domain locks. The relationship owner must then resolve/validate endpoints through the entity public detached reference contract (with locking where needed), never import foreign `Document`, `DocumentVersion`, `DocumentChunk`, `Entity`, `_aliases` or `_entity_read`.

## 4. Concrete deletion composition, with RN1 ownership retained

The real base entry points are `documents.public.delete_document`, noncommitting `documents.public.delete_source_documents`, and `sources.worker.process_source_purge`. T1 provides small **noncommitting owner commands**:

```text
relationships.public.remove_document_support(session, document_id)
entities.public.remove_document_support(session, document_id)
relationships.public.remove_source_support(session, source_id)
entities.public.remove_source_support(session, source_id)
```

Names may follow the implementer's existing owning equivalents; the behavior is fixed. Each command writes only its owner's support/membership/alias/relationship data. Store the validated document/source identities on the support records (or use an implemented document-owner identity query), rather than introducing foreign-ORM cleanup joins. These fields have immediate cleanup consumers. Historical unresolved support is handled by the existing compatibility ruling, never backfilled with guessed IDs.

Acquire affected entity locks in stable UUID order, then relationship locks in stable order, before membership/support changes. Coordinate the two owner commands to preserve that common lock order. Remove relationship supports before deleting entity memberships that they reference; recompute maximum remaining derived confidence; remove unsupported derived relationships/aliases and deleted-source text. Retain independently supported facts and genuinely owner-authored corrections/identity. Neither cleanup command commits, acquires replay-head locks, nor calls a global unbounded sweep as a substitute for the deletion predicate.

Single-document deletion's source/document locks protect the identity while these commands run **before** physical version/chunk deletion. RN1's actual document deletion calls `ingestion.public.tombstone_document_materializations(session, document.id)` before cascade; preserve that call and its document-owned tombstone work, with P04 cleanup composed before cascade in the same transaction. Source purge calls the source-scoped owner cleanup inside its already source-locked transaction, before document deletion. Cleanup, document deletion, RN1 tombstone/progress terminalization where applicable, and final replay commit are one atomic transaction. The existing real document/source deleted event supplies the provider invalidation described above; no large affected-ID event payload is required. Keep cleanup selection/materialization bounded with source/document predicates and deterministic continuation; do not partially commit a deletion or silently truncate its support cleanup. A closure beyond an implemented capacity bound fails explicitly before destructive publication, rather than leaving stale support.

RN1 owns normalization/provenance/version allocation and persistent tombstone semantics. P04 must add these cleanup composition calls without replacing RN1's deletion/tombstone logic or refreshing captured generations. Both branches must retain all owner operations when integrated. Record the overlapping document-public/deletion and source-purge sections for controller reconciliation; final combined source review/build is required. This ruling does not move those responsibilities into P04 or authorize edits to RN1's active tree.

## 5. Transaction suffix and authenticated actor

Graph mutations finish owner validation, all source/document/entity/relationship locks, writes, detached response/audit projection and flush **before** `core.realtime.commit_with_replay(session, drafts)`. That finalizer alone takes replay head last, appends and commits with rollback handling. No HTTP/provider call, new domain lock/query/write or nested public commit after replay-head acquisition. If refreshing a response after commit, that refresh belongs to the next transaction. Composed cleanup helpers return without finalizing; only the outer mutation/deletion transaction does so.

Evidence-backed writes follow source -> document -> entity -> relationship -> memberships/support -> flush -> replay head -> commit. Manual graph writes with no source/document support start at their real entity/relationship domain locks; do not fabricate earlier locks. Entity deletion must not acquire source locks after holding entity locks. Acquire any needed source/document lock set first when attaching/rewriting evidence.

`require_owner_write` returns the existing **`AuthSession`**, whose durable actor is **`auth_session.owner_id` (integer)**. Pass that value from the protected route into owner commands and persist it with server UTC mutation time, operation/reason and affected identities/revisions. `AuthSession` has `token_hash` as its session primary key and no generic actor UUID/`id`; neither token hash, CSRF hash, client actor field nor fabricated singleton UUID is provenance. Existing single-owner ID 1 is a schema invariant, not a reason to bypass the authenticated route/session value. Automated extraction records its actual automated/model provenance separately and must not masquerade as an owner session.

## Evidence and limits

Read the actual integrated typed payload, replay finalizer/transport, provider knowledge handler and resync roots; document models/public deletion and source-purge transaction; protected auth model/dependencies; real frontend query keys; preserved relationship/entity draft boundaries; and existing P04 plan/entry rulings. A bounded read of the parallel RN1 tree confirmed its actual `NormalizedVersionProvenance` and `tombstone_document_materializations` binding; that evolving tree is not reviewed for completion here. Source base identity is controller supplied. No production edits, Git/index, builds/tests/lint/typecheck, runtime/provider/services/browser calls, installs or agents. Only this authorized scratch ruling is written.

Existing migration compatibility stands: preserve exact historical `0007_entities`; refresh the actual merge parent to the final RN1-integrated head before the final phase gate. This ruling does not establish migration application history or runtime acceptance.
