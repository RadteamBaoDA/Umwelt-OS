# P02-RN1 bounded owner ruling

Date: 2026-10-01. Source inspected in read-only prep tree `C:/Users/doana/.codex/worktrees/bbd-p02-normalize/Umwelt-OS`, controller base `876aa77`. Existing RN1 brief/plan remains authoritative; this pins down its ordering/deletion contracts, not a new architecture. No production edit, Git/index, build/test/lint/typecheck, migration/runtime/DB/provider call or agent was run. Only this scratch ruling is written.

## Entry dependency

Independent backend normalization production can start on integrated R06 while R04 editor repair closes. Source fence, receipt/run/stage/outbox, document/chunk, retry/dispatcher, search and replay owners are present. R04's configuration/history DTO reads are not required to persist normalized versions. Final R04 integration must consume actual normalize progress/run status/query keys and preserve guard + realtime provider composition; require final combined build/source review before RN1 commit. Record this as scheduling clarification, retaining the approved full scope and original P02 history.

## 1. Minimum persisted identities and detached contracts

Current `SourceObservation` uniqueness is `(batch_id,provider_id,record_hash,observed_at)`, so overlapping batches can contain distinct observation UUIDs for one logical record. Current receive/crawl record_hash already hashes canonical version/content/metadata, not content alone. Retain the accepted hash; do not redefine it from a lossy normalized text projection.

Use these concrete owner responsibilities:

- **Ingestion owns per-observation progress**, unique `(observation_id, normalization_version)`: pending/normalized/skipped/failed disposition, bounded error, captured source generation, actual document/version refs when present, and durable timestamps. Same row identity resumes after retries; no fresh UUID for already processed observation. A mapped version FK may become null after document deletion, but the disposition must not revert to pending merely because it is null.
- **Documents owns normalized version provenance**, unique `(document_id, accepted_record_hash, normalization_version)`; document identity already uniquely maps `(source_id,external_id)` where external_id is provider_id. Accepted hash includes opaque provider version, so no nullable provider-version unique-key trick is needed. Persist provider_id/version explicitly for provenance, immutable observed/received/collected/published times, source generation, title/URL/content type and allowlisted metadata. Logical duplicate observations attach to the same immutable revision even across batches.
- **Documents owns the persistent identity tombstone**, unique `(source_id,external_id)`, with tombstoned flag/deleted_at and optional live document ID. It survives deletion of Document/versions, so it must not FK-CASCADE away with the deleted document. Source lock serializes absent identity-row/document creation; identity row plus existing uniqueness prevents accidental duplicate materialization.

Concrete document public input DTO:

`NormalizedDocumentInput {source_id, expected_source_generation, observation_id, provider_id, provider_version|null, accepted_record_hash, normalization_version:int>=1, observed_at:UTC, received_at:UTC|null, collected_at:UTC|null, title, canonical_url|null, published_at:UTC|null, content_type|null, content, provenance}`.

Enforce the existing ingestion limits (provider_id<=512, provider_version<=255, content<=1,000,000 characters), bounded UTF-8 work bytes and metadata allowlist before materialization; title<=500/content_type<=64 match document storage. Publication time, observation time, collection time and server receipt time remain distinct. Add actual observation received_at at receipt persistence; IngestionBatch.created_at is **not** universally receipt time because crawl batches are queued before HTTP. Legacy unknown received/collected values stay null. REST raw row metadata is not an allowlist; RSS must retain distinct supplied item URL/publication/collection fields without turning opaque IDs into guessed URLs.

Public command `documents.public.upsert_normalized_document(session,input)` returns detached:

`NormalizedDocumentResult {disposition:normalized|duplicate|tombstoned, document_id|null, document_version_id|null, version_number|null, created_version:bool, selected_current:bool, chunk_count:int}`.

It validates the actual source fence/identity, creates/reuses document/version/provenance/chunks and **does not commit**. Ingestion sets its progress/result mapping and publishes work/replay at its outer finalizer. No document ORM crosses this boundary; do not use the committing create_document/append_content HTTP-style owners.

## 2. Delayed provider order and current selection

Current record.version is opaque. For existing RSS/web/REST, use **observation order**, not lexicographic/numeric provider-version guesses:

1. A new logical revision's fixed selection rank is `(original observed_at in UTC, accepted_record_hash)`; hash supplies deterministic tie resolution, not a claim of greater semantic freshness. Preserve that rank in immutable provenance when the logical revision first materializes.
2. Promote Document.current_version and its mutable title/URL/content hash/status projection only when the new rank is greater than the currently selected normalized revision's rank. Older/equal conflicting observations become immutable history and leave the current projection unchanged. Do not use received_at or worker completion order to promote a delayed provider record.
3. A duplicate matching accepted hash + normalization version attaches to the existing version. A later polling/received timestamp does not refresh its rank or promote an older version back to current. A legitimate provider reversal needs a distinct accepted version/metadata hash, not merely repeat delivery of old bytes. This is conservative fallback policy for opaque versions; a future provider-specific ordered field requires a documented adapter contract.
4. Changed provider version/metadata/title/URL creates a new immutable provenance revision even when text bytes match. Create corresponding immutable chunks with existing chunk_text; do not mutate old chunks or collapse identity to content_hash.
5. **Allocate `max(stored version_number)+1` under document lock**, not current_version+1. Current may point behind the highest stored revision after delayed history. Update every existing revision allocator that can touch such a document: append_content (`documents/public.py:282–319`) and save_extraction (`160–221`) currently assume current+1. Retain their current-version CAS semantics and source-before-document lock order; do not renumber old versions.

Keep unknown/mixed legacy provenance honest. Do not guess an ordering key for a manually authored revision from provider metadata; when a normalized identity collides with an existing non-normalized document lacking a comparable selection baseline, reject as an explicit identity/ordering conflict rather than overwrite owner-authored current content. This does not add a merge framework or change manual/file behavior.

## 3. Single-document deletion is authoritative

Default: deleting a normalized document tombstones its `(source_id,provider_id)` identity and blocks **all future automatic versions of that identity**, including changed provider versions/hashes. Exact replay, stale retries and future collection record observations may remain durably accepted, but normalization records terminal `tombstoned`/skipped disposition, returns no materialized version and never recreates it.

This policy is stronger and simpler than marking only already-seen observation UUIDs, which would allow a fresh overlapping-batch UUID or new provider version to resurrect deleted data. There is no implicit restore via Collect now, retry, pause/resume or null FK. Any later owner-approved reimport must explicitly clear that tombstone in a consumed owner operation; do not add an unused restore UI/endpoint in RN1. A genuinely different provider_id is a different identity.

`documents.public.delete_document:260–274` must become source lock -> identity/document lock -> set identity tombstone and terminalize associated ingestion materializations through an ingestion public command -> compose P04 cleanup where present -> delete document/versions/chunks -> flush -> final R06 replay/commit. Keep tombstone in the same transaction as deletion. Existing receipt observations cannot revive it after FK cascade.

Source purge composes existing `delete_source_documents:277–279` and `ingestion.cancel_and_purge_source_ingestion:127–141` inside the source-locked purge transaction. Extend both to new provenance/identity/progress/stage/work cleanup. Source generation/status rejects every old work payload; source purge need not retain single-document identities after removing the entire generation's accepted data. No receipt cursor rewind.

## 4. Source generation, transactions and retry hooks

Every bounded normalization transaction locks and checks the **captured** source generation, active status and actual accepted batch/source membership. Pause/reconfigure/archive/purge makes stale work terminal/visibly blocked; retry must not upgrade its captured generation to make it valid. Already accepted content is normalized through supported local APIs without another collector credential send; provider/model permission is separately enforced by downstream indexing/extraction.

Consistent transaction order:

`source -> run -> normalize stage -> bounded observation/progress -> document identity/document -> version/provenance/chunks -> progress/results + durable work notifications -> flush -> replay head/rows -> immediate commit`.

All current RN1 writes/deletion share the source lock, serializing absent-document creation and preventing source purge races. Document commands never acquire source after first taking a document lock. No HTTP/model send belongs in normalization. Use stable sorted document/provider identity order if a transaction processes more than one identity; bound record count and UTF-8 bytes per transaction. Resume from persisted progress, not only an in-memory loop counter.

Actual integration hooks:

- `ingestion.public.receive_batch:225–280` creates receive stage, observations and work/cursor in one transaction. Add/schedule the concrete normalize stage after accepted textual observations, preserving durable cursor acceptance.
- `_collect_web_job:165–200` persists canonical crawl observations after HTTP; compose normalize scheduling after real observations, not the empty queue-crawl batch. Its generic completion owner must no longer claim library-ready solely from count.
- `process_ingestion_event:323–336` currently only counts observations and later completes run. Receive/crawl stage can succeed while normalize remains pending; run succeeds only after all required receipt/normalize stages are terminal successfully or explicitly skipped with visible disposition. Optional embedding/entity gating does not fail textual normalization.
- Add explicit normalize event dispatch in `ingestion.dispatcher.dispatch_pending_work` and a registered ingestion worker function in WorkerSettings. Unknown events currently fall through to process_ingestion_event and expect run/stage IDs. P04 document.version.ready needs its explicit P04 consumer mapping before delivery; do not send it into that fallback. Retain durable ready identifiers for actual new nonempty ready chunks only, not tombstoned/duplicate notifications.
- `ingestion.public.retry_run:462–529` currently selects one arbitrary stage and overwrites event source_generation with current source.generation. For RN1, lock ordered stages, choose the actual retryable failed receive/crawl or normalize stage by explicit stage_key/dependency, preserve completed observation progress, and reuse the original captured fence. Reject stale-fence retry rather than blessing it. Publish the exact event type/payload for that stage; do not rerun succeeded receive and rewind acknowledged cursors.
- R04 list_source_runs/StageRead and SyncHistory must expose normalize counts/dispositions and keep polling until required stages complete. Existing SyncHistory stops on terminal run status, so correct run aggregation is load-bearing. Keep current status literals where adequate; add literals only when actually consumed and migrated.
- Existing search.indexing.index_pending_chunks polls current ready chunks; lexical search already selects current revisions. Reuse these consumers. New version/history/current-change knowledge notifications go through integrated R06 terminal finalizer after every domain lock/write/flush; no second vector queue or pipeline DSL.

## Verification boundary

These choices are production implementation directions for the existing RN1 plan. They do not establish ordering/deletion under live concurrency, migrations, provider mapping or capacity. Controller owns scope/impact review, affected builds, final R04/R06 integration and frozen source review. Tests/fixtures/lint/standalone typecheck and all runtime acceptance remain deferred.
