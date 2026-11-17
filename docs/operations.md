# Operations

## Retention and module lifecycle

Agent trace retention defaults to 90 days and can be changed in Settings → advanced operations. The hourly worker redacts only terminal runs past the cutoff. It preserves run IDs, status/outcome metadata, effect and approval identifiers, idempotency records, source records, document revisions, and Chat messages. Runs with unresolved approvals or reserved, in-flight, or review-required effects are skipped. Checkpoints and run trace payloads are cleared as one bounded batch.

Expired browser page evidence is removed in bounded batches after its job expiry. Browser job rows remain as idempotency and outcome tombstones. Maintenance reports the last counts and next eligible time in Settings.

Module disables are stored as explicit owner choices; dependencies determine effective availability transitively. Owner request gates verify owner and CSRF authorization before returning module state. Collector bearer routes, OAuth callbacks, GitHub webhooks, and inbound MCP authentication retain their existing credential checks. Source/document/chat/memory deletion, agent cancellation, approval decisions, memory privacy purge, and connector security revocation paths remain available during a module disable. Worker dispatch and native tool calls read current persisted availability before acting; recoveries, expiry cleanup, and privacy maintenance continue to run.

## Observability and troubleshooting

Use the request ID from an API error response or its `X-Request-ID` header to find the matching API log. For accepted asynchronous work, keep the durable `run_id` and inspect it in Settings → advanced operations; request IDs and run IDs are different identifiers, and unavailable correlations remain unknown. No-change polls are intentionally silent; cleanup activity is debug logged and actionable failures are warned. See [observability and Langfuse](observability.md) for health boundaries, privacy, and the optional remote profile.

## Model gateway circuit breaker

Each upstream (provider URL plus key, identified by `gateway_identity`) has a Redis breaker (`bbd:model-gateway:breaker:{identity}:fail` and `:open`). Five counted failures within 60 seconds open it for 30 seconds; a 429 with `Retry-After` of 2 seconds or more opens it for that duration (cap 300 seconds). While open, model calls raise `ModelGatewayUnavailable` (carries `retry_after`) without contacting the provider, so Chat and extraction fail fast and retry later. Counted failures are timeouts, connection errors and HTTP 408/425/429/5xx (streams only before the first token); internal capacity or lease waits, other 4xx, unsupported capability and privacy denials are not counted. Retries use 0.25-1 second jitter. If Redis is unreachable the breaker is skipped (fails open).

Reset: a successful Test connection in Settings (probe) bypasses the open flag and clears the failure count; discovering models does the same. Waiting out the TTL also works. Changing the provider URL or key changes the identity, so the new configuration starts closed. Connectors, n8n, Langfuse and Graphiti have separate backoff and are not covered.

## Alerts and signals to watch

- Repeated `ModelGatewayUnavailable` in API or worker logs: breaker open; check the provider, then Test connection.
- Sizes of `event_outbox`, `chat_stream_events`, `source_observations`, `realtime_replay_events` and `connector_*` tables (queries in [observability](observability.md#database)); steady growth means a retention job below is failing.
- Long `idle in transaction` sessions or `log_lock_waits` entries: a lock held across external I/O is a regression against the no-lock-across-I/O rule.

## Retention jobs

| Table | Rule | Schedule | Bound |
| --- | --- | --- | --- |
| `chat_stream_events` | `message.delta` of terminal runs older than 1 day (`message.done` keeps the full text) | `purge_expired_chat_runs`, every 15 minutes | 5000 rows per call |
| `source_observations` | Not stored twice: records with an existing `(source_id, provider_id, record_hash)` in the same source generation are skipped at intake (measurement providers excluded) | at collection | not applicable |
| `connector_collection_requests`, `connector_provider_sends`, expired quota windows | terminal requests older than 30 days; sends older than 7 days; windows past their end with no debits | `purge_collection_receipts`, minute 7 hourly | bounded batches (1000 per call for connector history) |
| `event_outbox` | `delivered` and `failed` rows only, never `pending`/`queued`: `ingestion.stage.requested`/`ingestion.normalize.requested` older than 7 days, every other event type older than `OUTBOX_RETENTION_DAYS` (default 30, min 7) | same job | 1000 per call |
| `automation_runs` | terminal runs finished more than 30 days ago, newest hour kept for rate caps | `run_retention_maintenance`, hourly | 1000 per call |
| `search_index_items` of superseded document versions | removed when the version is superseded (hook registered by search) | event-driven | per document |
| Agent traces, browser evidence | see Retention and module lifecycle above | hourly | as above |

Schedules and bounds are read from `apps/worker/main.py` and the cited modules; none has been observed running against a live database.

## Reconciler cadence

The reconcilers (outbox dispatch, connectors, document cleanup stages, graph recovery, agent dispatch, automation runs) are separate cron jobs on purpose. Do not merge them into one sequential job: a tick cannot overlap itself, so one slow reconciler would delay all the others (measured regression, see [ARCHITECTURE_DECISIONS](ARCHITECTURE_DECISIONS.md)).

## Database migrations (online safety)

Applies to revision `0014_perf_indexes_and_safety` and every later revision.

- **Timeouts.** `infrastructure/postgres/migrations/env.py` sets `lock_timeout` (default 5 s, `UMWELT_MIGRATION_LOCK_TIMEOUT_MS`) and `statement_timeout` (default 1 h, `UMWELT_MIGRATION_STATEMENT_TIMEOUT_MS`; `0` disables) on the migration connection. A DDL step that cannot get its lock in 5 s fails and can simply be re-run; revisions are written to be idempotent (`IF [NOT] EXISTS`).
- **Indexes are online.** Use `core.migration_support.create_index_concurrently` / `drop_index_concurrently` (`CREATE/DROP INDEX CONCURRENTLY IF [NOT] EXISTS` inside `autocommit_block`; an invalid leftover from an interrupted build is dropped first). These helpers commit the surrounding transaction, so put transactional DDL before them in a revision.
- **Linter.** `scripts/lint_migrations.py` (also `tests/test_migration_safety.py`, part of `make test`) renders `alembic upgrade 0001_init:head --sql` and rejects, for revisions after 0013: non-CONCURRENT `CREATE`/`DROP INDEX` on existing tables, CONCURRENTLY inside a transaction, volatile column defaults, stored generated columns, `NOT NULL` columns without default, column type changes, `SET NOT NULL`, and FK/CHECK constraints without `NOT VALID`. A deliberate exception must be listed in `ALLOWED` with its reason.
- **Guarded baseline downgrade.** `alembic downgrade` through `0001_init` drops the whole `public` schema and now refuses unless `UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE=1` is set. Disposable-database tests set it explicitly; production downgrades are not a supported rollback path (restore from the provider backup).
- **Unindexed foreign keys.** `python scripts/list_unindexed_fks.py` prints FK column sets with no leading index, computed from the migration DDL alone. `workspace_id` is ignored in composite FKs (one workspace per deployment) and FKs to the one-row `workspaces`/`owner` parents are skipped (deleted only by account erasure). After 0014 the only remaining entry is `workspace_shares (workspace_id, member_user_id) -> workspace_memberships` (membership removal is rare and the share table is tiny).

### Chat SQL reader login (revision 0017)

Migration `0017_chat_sql_login_role` creates the passwordless `umwelt_chat_sql_login` role and the `chat_sql` views. Set `CHAT_SQL_READER_DATABASE_URL` (`postgresql+asyncpg://umwelt_chat_sql_login:<secret>@<postgres-host>:5432/<db>`, direct, not PgBouncer; `make setup` generates it) and run `python -m core.chat_sql_provision` after `alembic upgrade head` (the `migrate` service does) to apply the password; rerun after changing the secret. The script computes a SCRAM-SHA-256 verifier client-side and sends only `ALTER ROLE ... PASSWORD 'SCRAM-SHA-256$...'`, so the plaintext never appears in server logs, SQLAlchemy errors or echo output (the password must be printable ASCII without spaces; `make setup` tokens are). Failures print only the exception class and SQLSTATE. Leaving it unset keeps the chat SQL mode disabled. The `migrate` command is `alembic upgrade head && python -m core.chat_sql_provision`, so when the URL is set but wrong (names another user, unreachable host, unusable password) provisioning exits non-zero and every service waiting on `migrate` stays down. This is deliberate fail-closed behaviour: a misconfigured secret must be loud, not silently leave the SQL tool without a working role. Fix or unset `CHAT_SQL_READER_DATABASE_URL` and rerun `docker compose up`. The role is limited to 8 connections; the API/chat worker pool uses 2 per process. Downgrade drops the role, schema and views.

### Revision 0014 lock and rewrite cost (read before upgrading a large database)

`document_chunks.content_tsv` is a stored generated column (`to_tsvector('simple', content)`). PostgreSQL adds it with a **full table rewrite under `ACCESS EXCLUSIVE`**: chunk reads and writes (search, ingestion, chat retrieval) block for the duration (roughly 1-2 minutes per million average-size chunks, plus WAL), and free disk of about the table size is needed temporarily. This is accepted for this release; run the upgrade in a maintenance window with workers stopped and raise `UMWELT_MIGRATION_STATEMENT_TIMEOUT_MS` for very large libraries. The GIN index on the column and all other index work in 0014 is `CONCURRENTLY` and does not block writes. If the migration is interrupted, re-run it: the column add, index creates and drops are idempotent and an invalid half-built index is replaced.

0014 also: drops the identical expression GIN indexes `ix_document_chunks_simple_fts` and `ix_document_chunks_content_tsv` (superseded by `ix_document_chunks_content_tsv_gin`) and the duplicate `ix_relationship_evidence_document`; the redundant `chat_stream_events` indexes on `(response_id)` / `(response_id, seq)` and the `realtime_replay_events` indexes were already removed by the 0001 baseline. It adds `ix_fk_*` indexes for FK columns on source/document/entity/run/dashboard delete and purge paths, replaces three low-selectivity status indexes with partial ones (`ix_chat_response_runs_active_status`, `ix_temporal_operations_open_status`, `ix_temporal_reconcile_runs_open_status`), adds `ix_event_outbox_failed_created`, and sets per-table autovacuum/analyze scale factors on `event_outbox`, `chat_stream_events`, `realtime_replay_events`, `source_observations` and `system_health_samples`. The `ix_w2_*_scope` indexes that duplicate `uq_w2_*_id` unique constraints are model-declared and left for a separate cleanup.

### Why `chat_stream_events` and `realtime_replay_events` are not partitioned

Declarative time partitioning needs the partition key in every primary/unique key. `chat_stream_events` relies on `UNIQUE(event_id)` and `UNIQUE(response_id, seq)` for idempotent writes and `realtime_replay_events` on its `(workspace_id, user_id, sequence)` primary key; none can include `created_at` without weakening those guarantees, so a swap-in migration is not safe. Both tables stay bounded by retention (`purge_expired_chat_runs`, realtime replay trimming) plus the tightened autovacuum settings above. Revisit if retention cannot keep up.

### Why `event_outbox` is not partitioned

Producers use deterministic `uuid5` event ids and rely on the primary-key `IntegrityError` to deduplicate (`modules/knowledge/documents/worker.py`, `_cleanup.py`); a partitioned `(id, created_at)` primary key would silently stop enforcing that uniqueness. About 28 lookups go by `id` alone and cannot prune partitions. Single-user volume is small and the hourly purge (1000 rows per run, 24k/day) keeps up. Revisit when `umwelt_outbox_rows_total` exceeds about 5M or `UmweltOutboxPurgeBacklog` persists; first add a bounded drain loop to the purge before considering partitioning.

Known debt: `set_event_delivery` (`modules/ingestion/_outbox.py`) is an unconditional status update, so a late duplicate job could flip a quarantined `failed` row to `delivered`. The effect stays idempotent, so this is not a correctness loss.

### Search HNSW indexes

`activate_generation` no longer builds the per-generation HNSW index inside the activating transaction (which blocked writes to `search_index_items`). The generation is finalized in this order: commit the pruning transaction, `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_search_vector_<generation>` on a separate autocommit connection (`statement_timeout` 2 h via `HNSW_BUILD_STATEMENT_TIMEOUT_MS`, `lock_timeout` lifted), then re-lock (access fence, then generation row) and activate. A failed or cancelled build drops its own invalid index; a retry also drops any invalid leftover first. If the generation changed during the build, nothing is activated and the next indexing pass re-finalizes (the built index is reused).

`CREATE INDEX CONCURRENTLY` waits for every transaction that was open when it started. A long `idle in transaction` session therefore stalls the build until it ends; after 2 h the build fails on the statement timeout, its invalid index is dropped and the next indexing pass retries. If builds time out repeatedly, find and end the idle session (`pg_stat_activity` where `state = 'idle in transaction'`).

## Cron worker, offsets and leases

All recurring jobs run on the `cron-worker` service (`apps.worker.main.CronWorkerSettings`, queue `arq:cron`), not on the ingestion worker, so polls never occupy its `max_jobs` slots. The schedule is the `CRON_SPECS` table in `apps/worker/main.py`.

- **Offsets.** Each job keeps its period but its phase comes from a SHA-256 of the job name (`core.cron.stable_offset`): 5 s jobs fire at `phase, phase+5, ...`; per-minute jobs at a hashed second; hourly jobs at a hashed minute and second (`purge_collection_receipts` stays at :07, `expire_translations` at 03:17). At most 4 jobs can share a second (was 8 at :00 and about 24 at hh:00:00). `tests/unit/test_cron_schedule.py` pins this.
- **Single-flight lease.** Every cron run takes the Redis lease `cron:lease:<job>` (`SET NX PX`, random token, TTL = 2 x period clamped to 30-300 s, renewed every TTL/3 while running, compare-and-delete release in `finally`). A tick that finds the lease held returns immediately, so a run longer than its period and multiple worker replicas never run one cron concurrently. A crashed holder is replaced when its TTL lapses.
- **Run record.** `core.cron.record_cron_run(name, duration, ok)` keeps last success/failure time, duration and success/failure/skip counters in process memory (`cron_status()` is the read API for the observability package); successful runs are mirrored to the Redis hash `cron:last_success` (`<epoch>:<duration ms>`). The same call also publishes the Prometheus cron series (`core.metrics.record_cron`: duration histogram, last-success gauge, failure counter, labelled by job name), so every scheduled tick, wake-up and catch-up run is measured once; lease-held skips are not runs and record nothing.
- **Catch-up.** On cron-worker start, every job without `run_at_startup` whose last success is missing or older than 2 x its period is enqueued once (`catchup:<job>:<minute>`, stable 0-29 s defer) through the same lease.
- **Event-driven pollers.** `dispatch_pending_work` (outbox) and `index_pending_chunks` are woken right after the transaction that publishes an outbox event / a `document.version.ready` commits (`core.cron.notify_on_commit`: one `after_commit`/`after_soft_rollback` listener pair per session, so a rollback never leaves a stale listener; the hook is installed in the API and both full workers). A wake enqueues the plain arq function `run_cron_job` (not a `CronJob`) under a unique id `wake:<job>:<token>`, debounced by `SET cron:wake:<job> NX PX 5000` (cleared when the wake job starts). Fixed ids were rejected on purpose: arq keeps CronJob in-progress keys for 60 s and failed results for the result TTL, so wakes would have been slower than the safety net; `CronWorkerSettings.keep_result = 0` for the same reason. A wake that meets a held lease sets `cron:rerun:<job>`; the holder checks it after releasing the lease and runs again (at most 3 extra times), so a commit during a run is never lost. Wake debounce: at most one wake per job per 5 s (`WAKE_FLAG_TTL_MS`), so a burst of commits inside that window shares a single run; the periodic safety net covers anything missed. Periodic runs are now a 30 s / 60 s safety net. Ingestion continuations (normalization slices with rows left, transient stage retries, observation deferral) return the existing outbox row to `pending` without a new event, so they call `_wake_dispatcher_after_commit` themselves; the wake delay follows the row's backoff, capped at the 5 s debounce window. Cron jobs and the wake function run with `max_tries = 5` (`CRON_MAX_TRIES`) so a heavy-slot `Retry` (indexing while ingestion holds the slot) re-runs within the tick; plain exceptions are never retried by arq.
- **Pollers that stay at 5 s (reviewed, no wake wired).** `reconcile_connectors`, the three document cleanup reconcilers, `recover_graph_work`, `reconcile_agent_dispatch` and `reconcile_automation_runs` find work by an age/lease predicate (stalled, expired or unclaimed rows) rather than by a freshly committed row, and their rows are written from many call sites across modules without one shared commit point; waking them would add noise without removing the poll, so they keep their period with hashed offsets. Add `notify_on_commit(session, "<job>")` at a producer if a specific one needs lower latency.

## Retries, dead letters and replay

- **Backoff.** Retries use `core.worker_retry.backoff_delay`: ceiling `min(cap, base * 2^(try-1))`, delay uniformly in `[ceiling/2, ceiling]`. Heavy-slot contention: base 5 s, cap 15 s (jobs keep 60 tries); remote-guard blocks: base 30 s, cap 120 s. Provider `Retry-After` delays in ingestion stay as computed.
- **Dead letters.** (Recorded: any ordinary exception on any try, since arq never retries those; a job timeout; and a `Retry`/`RetryJob`/shutdown cancellation on the final try.) Whenever arq will not run an ingestion-worker or model-worker job again, one row is upserted into `worker_dead_letters` (migration `0015_worker_dead_letters`): queue, job name, job id, SHA-256 of the arguments, attempts, first/last failure time, error class and a scrubbed 300-character message. Arguments are never stored; only when every argument is an identifier-shaped string are the ids kept in `args_ref` and the row marked replayable. Job-specific recovery crons remain the first line; the table is the audit trail for what they did not recover.
- **CLI (operator, run in the api image).** `python -m core.worker_dlq list [--limit N]`, `python -m core.worker_dlq replay [--id UUID] [--execute]` (dry run unless `--execute`; re-enqueues under `dlq:<id>:<n>` on the original queue), `python -m core.worker_dlq purge --older-than-days N [--execute]`. Rows hold no workspace or user data, but purge old ones with the CLI after review.

- **Cron jobs and the dead-letter table.** The `worker_dead_letters` DLQ only records ingestion-worker and model-worker jobs. Cron jobs (scheduled, wake and catch-up runs) are never dead-lettered: a failed run is visible only through the cron failure counter/metrics and logs, and the next tick or the safety-net poll retries the work.

## Search indexing batches

`SEARCH_EMBED_BATCH_SIZE` (default 64, max 128) chunks are claimed per keyset query, embedded in one gateway request, revalidated in one short session (access -> Source -> chunk) before the send and again under locks (Sources in sorted order) at publication, and written with one transaction per batch. A malformed response or a source that changed mid-batch falls back to the per-chunk path for that batch only. The run loops while work remains for up to 45 s (2048 chunks); `document.version.ready` also wakes it. Index contents are identical to the one-chunk path (`tests/unit/modules/search/test_embed_batch.py`).


## Object storage operations (S3-compatible)

**Migrate from local files to S3.** Do this in a maintenance window or with uploads quiet; the tool is resumable and never modifies the source.

1. Create the bucket and set the `S3_*` variables, but keep `STORAGE_BACKEND=local`. Run the database migration (`make migrate`) so `workspace_storage_state` exists.
2. Dry run from the API or worker container: `python -m core.storage.migrate --from local --to s3 --dry-run`. It lists every original, reports what would be copied and verifies objects already present.
3. Copy: `python -m core.storage.migrate --from local --to s3`. Each object is streamed through the destination's `put_stream` (so size bound, never-overwrite and the erasure fence apply), re-read from S3 and compared by SHA-256 and size; a mismatch deletes the copy and is reported. Exit status 1 means failures or skipped keys (legacy `documents/...` or stray temp files are not copied; review them manually). Re-run until it reports `failures=0`.
4. Set `STORAGE_BACKEND=s3`, restart API and worker, upload and download a test file.
5. Keep the local `DATA_DIR/workspaces` tree until you are satisfied, then remove it manually. Rolling back is the reverse command (`--from s3 --to local`) before step 4's uploads diverge.

**Orphans.** The worker's orphan sweep lists `workspaces/` (and the legacy `documents/` and `staging/` prefixes) with paginated `ListObjectsV2`, and deletes upload objects older than `STORAGE_ORPHAN_GRACE_SECONDS` that no document or cleanup receipt references, exactly like the local sweep. Unrelated keys in the bucket are never touched.

**Bucket lifecycle rules (recommended).** Publication never leaves objects under `staging/` unless a process dies at the wrong moment (only with `S3_CONDITIONAL_PUT=false`), and an interrupted multipart upload leaves invisible parts. Add both rules (with `S3_PREFIX`, prefix the filter accordingly):

```json
{"Rules": [
  {"ID": "expire-staging", "Status": "Enabled", "Filter": {"Prefix": "staging/"}, "Expiration": {"Days": 1}},
  {"ID": "abort-stale-multipart", "Status": "Enabled", "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}
]}
```

Apply with `aws s3api put-bucket-lifecycle-configuration --bucket <bucket> --lifecycle-configuration file://lifecycle.json` (RustFS: use its console or `mc ilm rule add` against the S3 endpoint). Do not add an expiration rule for `workspaces/`; deletion of originals is owned by the application. Backups and bucket versioning/replication are the provider's responsibility.

### Database role model

- Migration user (= database owner in the default setup): owns the schema and the `chat_sql` views.
- `umwelt_app` (NOLOGIN group role, Alembic `0020`): holds database `TEMPORARY` and `pg_*advisory*` `EXECUTE`, which `0019` revoked from PUBLIC. It is granted to the migration user and the database owner. When the runtime role (`DATABASE_URL`) differs from the migration/admin role (`DATABASE_DIRECT_URL`), `python -m core.chat_sql_provision` (run by `migrate`) executes `GRANT umwelt_app TO <runtime user>` idempotently; do it by hand if you do not run that script.
- `umwelt_chat_sql_login`: reader login, never a member of `umwelt_app`, so it has no temp tables or advisory locks.
- The legacy `umwelt_chat_sql_reader` role (0013) is dropped by `0020`; the downgrade recreates it.
