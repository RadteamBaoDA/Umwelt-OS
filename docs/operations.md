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

The 5-second reconcilers (outbox dispatch, connectors, document cleanup stages, graph recovery, agent dispatch, automation runs) are separate cron jobs on purpose. Do not merge them into one sequential job: a tick cannot overlap itself, so one slow reconciler would delay all the others (measured regression, see [ARCHITECTURE_DECISIONS](ARCHITECTURE_DECISIONS.md)).

## Database migrations (online safety)

Applies to revision `0014_perf_indexes_and_safety` and every later revision.

- **Timeouts.** `infrastructure/postgres/migrations/env.py` sets `lock_timeout` (default 5 s, `UMWELT_MIGRATION_LOCK_TIMEOUT_MS`) and `statement_timeout` (default 1 h, `UMWELT_MIGRATION_STATEMENT_TIMEOUT_MS`; `0` disables) on the migration connection. A DDL step that cannot get its lock in 5 s fails and can simply be re-run; revisions are written to be idempotent (`IF [NOT] EXISTS`).
- **Indexes are online.** Use `core.migration_support.create_index_concurrently` / `drop_index_concurrently` (`CREATE/DROP INDEX CONCURRENTLY IF [NOT] EXISTS` inside `autocommit_block`; an invalid leftover from an interrupted build is dropped first). These helpers commit the surrounding transaction, so put transactional DDL before them in a revision.
- **Linter.** `scripts/lint_migrations.py` (also `tests/test_migration_safety.py`, part of `make test`) renders `alembic upgrade 0001_init:head --sql` and rejects, for revisions after 0013: non-CONCURRENT `CREATE`/`DROP INDEX` on existing tables, CONCURRENTLY inside a transaction, volatile column defaults, stored generated columns, `NOT NULL` columns without default, column type changes, `SET NOT NULL`, and FK/CHECK constraints without `NOT VALID`. A deliberate exception must be listed in `ALLOWED` with its reason.
- **Guarded baseline downgrade.** `alembic downgrade` through `0001_init` drops the whole `public` schema and now refuses unless `UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE=1` is set. Disposable-database tests set it explicitly; production downgrades are not a supported rollback path (restore from the provider backup).
- **Unindexed foreign keys.** `python scripts/list_unindexed_fks.py` prints FK column sets with no leading index, computed from the migration DDL alone. `workspace_id` is ignored in composite FKs (one workspace per deployment) and FKs to the one-row `workspaces`/`owner` parents are skipped (deleted only by account erasure). After 0014 the only remaining entry is `workspace_shares (workspace_id, member_user_id) -> workspace_memberships` (membership removal is rare and the share table is tiny).

### Revision 0014 lock and rewrite cost (read before upgrading a large database)

`document_chunks.content_tsv` is a stored generated column (`to_tsvector('simple', content)`). PostgreSQL adds it with a **full table rewrite under `ACCESS EXCLUSIVE`**: chunk reads and writes (search, ingestion, chat retrieval) block for the duration (roughly 1-2 minutes per million average-size chunks, plus WAL), and free disk of about the table size is needed temporarily. This is accepted for this release; run the upgrade in a maintenance window with workers stopped and raise `UMWELT_MIGRATION_STATEMENT_TIMEOUT_MS` for very large libraries. The GIN index on the column and all other index work in 0014 is `CONCURRENTLY` and does not block writes. If the migration is interrupted, re-run it: the column add, index creates and drops are idempotent and an invalid half-built index is replaced.

0014 also: drops the identical expression GIN indexes `ix_document_chunks_simple_fts` and `ix_document_chunks_content_tsv` (superseded by `ix_document_chunks_content_tsv_gin`) and the duplicate `ix_relationship_evidence_document`; the redundant `chat_stream_events` indexes on `(response_id)` / `(response_id, seq)` and the `realtime_replay_events` indexes were already removed by the 0001 baseline. It adds `ix_fk_*` indexes for FK columns on source/document/entity/run/dashboard delete and purge paths, replaces three low-selectivity status indexes with partial ones (`ix_chat_response_runs_active_status`, `ix_temporal_operations_open_status`, `ix_temporal_reconcile_runs_open_status`), adds `ix_event_outbox_failed_created`, and sets per-table autovacuum/analyze scale factors on `event_outbox`, `chat_stream_events`, `realtime_replay_events`, `source_observations` and `system_health_samples`. The `ix_w2_*_scope` indexes that duplicate `uq_w2_*_id` unique constraints are model-declared and left for a separate cleanup.

### Why `chat_stream_events` and `realtime_replay_events` are not partitioned

Declarative time partitioning needs the partition key in every primary/unique key. `chat_stream_events` relies on `UNIQUE(event_id)` and `UNIQUE(response_id, seq)` for idempotent writes and `realtime_replay_events` on its `(workspace_id, user_id, sequence)` primary key; none can include `created_at` without weakening those guarantees, so a swap-in migration is not safe. Both tables stay bounded by retention (`purge_expired_chat_runs`, realtime replay trimming) plus the tightened autovacuum settings above. Revisit if retention cannot keep up.

### Search HNSW indexes

`activate_generation` no longer builds the per-generation HNSW index inside the activating transaction (which blocked writes to `search_index_items`). The generation is finalized in this order: commit the pruning transaction, `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_search_vector_<generation>` on a separate autocommit connection (timeouts lifted), then re-lock (access fence, then generation row) and activate. A failed or cancelled build drops its own invalid index; a retry also drops any invalid leftover first. If the generation changed during the build, nothing is activated and the next indexing pass re-finalizes (the built index is reused).

