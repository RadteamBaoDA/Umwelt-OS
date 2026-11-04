# Umwelt-OS target performance report

Status: measurement pending. Target: 2 CPU cores, 8 GiB RAM, remote inference. No target-host benchmark, workload, OOM or deployment operation was executed during code implementation. Development build success is not capacity evidence.

## Measurement record

Copy and complete this record with actual target-host evidence during deferred acceptance. Unknown values stay null; record enabled optional services and remote placement explicitly. Attach raw measurements and exact source/deployment configuration, without secrets or private dataset content.

```json
{
  "target_host_verified": false,
  "measured_at_utc": null,
  "source_revision": null,
  "hardware": {"cpu_model": null, "cpu_cores": null, "ram_bytes": null, "storage_type": null},
  "platform": {"os": null, "architecture": null, "docker_version": null, "compose_version": null},
  "versions": {"application": null, "postgres": null, "redis": null, "graph": null, "n8n": null, "browser": null},
  "profiles": {"enabled": null, "service_placement": null, "service_limits": null},
  "ai": {"gateway_location": null, "provider_location": null, "model_ids": null},
  "workload": {"dataset_description": null, "document_count": null, "event_count": null, "observation_count": null, "concurrent_clients": null, "collector_concurrency": null, "duration_seconds": null},
  "resources": {"peak_rss_bytes": null, "peak_container_memory_bytes": null, "cpu_percent": null, "disk_peak_bytes": null, "disk_free_bytes": null},
  "queue_delay_ms": {"p50": null, "p95": null, "p99": null},
  "latency": {"provider_ms": null, "application_ms": null, "dashboard_ms": null, "search_ms": null, "chat_first_token_ms": null, "realtime_delivery_ms": null},
  "latency_distribution_ms": {"p50": null, "p95": null, "p99": null},
  "oom_events": null,
  "disk_errors": null,
  "measurement_commands": null,
  "raw_evidence_paths": null,
  "acceptance_result": "pending"
}
```

## Required workloads

Record collection, indexing, lexical/semantic search, dashboard map/chart/feed rendering, realtime reconnect/replay and grounded Chat with actual enabled profiles. Include service placement, workload overlap, queue delay, provider latency separately from application latency, and resource pressure during recovery/backup if enabled. Do not infer off-host graph/browser placement or silently reduce approved capabilities to obtain a passing result.

## Budget decisions

Keep current deployment limits until measurements justify changes. Record each proposed limit with measured workload, observed peak/headroom and behavior at overload. Worker concurrency or a successful container startup alone cannot prove the target budget. If target hardware or required integration is unavailable, retain this gate as pending and state the missing evidence.

See [release checklist](release-checklist.md) and [deployment](deployment.md). Production report preparation is a P12-T4 artifact; measured capacity and release acceptance remain incomplete.

## Measurement plan (2026-10-13, not executed)

All values in the record above stay null until measured on the target host. Targets (p95 first-token latency, concurrent streams, RAM/CPU ceilings) are TBD by the owner; none is asserted here. Run each scenario before and after the 2026-10-13 hardening where the old revision is still buildable, on the same data set.

Scenarios:
1. N concurrent chat streams (N stepped up until p95 first-token or error rate degrades), with a mock or fixed remote provider so provider latency is separable.
2. Several SSE tabs (`/api/v1/realtime/events`) open for a long period with reconnect via `Last-Event-ID`, while writes occur.
3. Search (lexical and semantic) during a full index build or re-index.
4. Ingestion and extraction (entity, timeline) running during chat sends.
5. Provider outage: breaker opens, then recovers via Test connection (confirm no request storm and that chat fails fast with `ModelGatewayUnavailable`).

Metrics: p50/p95/p99 first-token latency; event-loop lag; DB pool wait time and connections in use against the budget (84/100); `pg_stat_statements` top by total time before/after; lock waits from `log_lock_waits` and `pg_stat_activity` idle-in-transaction age; container RSS/CPU; table and index sizes of `chat_stream_events`, `source_observations`, `event_outbox`, `p12_backup_activity` over a multi-day run; Redis memory (`noeviction`, 256 MB).

## Changes expected to affect results (hypotheses, not measurements)

- Breaker and jittered retry: fewer retries against a failing provider; chat fails fast while the breaker is open.
- Offloaded upload validation and XML/HTML parsing, bounded DNS wait: less event-loop blocking during ingestion.
- Drain-before-lock and extraction/retrieval lock release: shorter workspace-lock hold, so chat sends should wait less behind extraction.
- Observation dedupe, delta purge, 0.4 s stream flush, retention jobs and superseded-embedding purge: lower write volume and slower table growth.
- r19 indexes (FK cascade paths, dedupe, GIN), dropped unused indexes, keyset indexing, vector query rewrite using the partial HNSW index, batched reads: lower query counts and delete/index cost.
- Postgres `jit=off` and larger `max_wal_size`: effect on a 2-core host is unmeasured.

## Event-loop offload (plan section 5A.2, A1-A12)

Behaviour-preserving moves of CPU-bound work off the API/worker event loop. Nothing here was benchmarked end to end; the numbers are the plan's estimates and the guard test below.

| Item | Change |
|---|---|
| A1 | `_run_web` parses each page through `_parse_page` in `to_thread_joined`. |
| A2 | `live_map._parse` runs in `to_thread_joined`. `_item()` construction is not cut at `MAX_ITEMS`: a malformed row past the cap raises today (`provider_response_invalid`), so stopping early would change behaviour. |
| A3 | `_prepare_crawl_records` (decode, validate, dump, canonical and per-record hashes) runs in one thread; SQL and the naive-timestamp check stay on the loop in the original order. |
| A4 | `json.loads` of REST, Hugging Face, GitHub, Google and local-service pages, plus REST `map_page`, run in threads. |
| A5 | Provider body decode/map calls in `collection.py` go through `_payload_async`. Request-builder calls stay inline (microseconds). |
| A6 | Small-text parsing uses `asyncio.timeout` around `to_thread_joined`, so the parser thread is joined before the heavy slot is released. A timeout now takes effect after the thread returns; the exception type is unchanged. |
| A7 | `content_hash_async` hashes content above 64 Ki characters in a thread; `crawl.py` offloads page digest/base64, result digest and envelope sizing. |
| A8 | `get_auth_redis` is `async def`. |
| A9 | Not offloaded (documented in code). The filesystem lease, mkstemp and `os.replace` are microsecond-scale on local disk. A thread hop would let a cancellation land after publication and change the atomic-publish and cleanup contract. Erasure interleaves fs calls with SQL under the lease. Revisit with the StorageBackend work. |
| A10 | SSRF lookups use `core.async_runtime.run_dns_lookup`, a dedicated 8-thread pool, not `loop.getaddrinfo`: `loop.getaddrinfo` itself runs in the default executor, so it would not fix the starvation. The module-level `getaddrinfo` names stay patchable. The default executor is sized explicitly to asyncio's own `min(32, cpu + 4)` (override with `THREAD_POOL_WORKERS`; not enlarged, so concurrent 19 MiB argon2 hashes stay bounded on a 2-core host) in the API lifespan and the three worker startups. |
| A11 | `docker/api.Dockerfile` bakes the `cl100k_base` BPE into `/app/.tiktoken-cache` at build time and sets `TIKTOKEN_CACHE_DIR`; `core/chunking.py` is unchanged. |
| A12 | uvicorn stays on `--loop asyncio`. uvloop needs a benchmark (P15 k6 scenarios) before switching; no measurement was taken here. |

Known limit: `json.loads` and pydantic-core validation hold the GIL for the whole native call, so a thread keeps the loop responsive only between native calls. A single 10 MB `json.loads` can still stall the loop for roughly its own duration. Only a process pool would remove that, and it is not part of this package.

Guardrail: the opt-in `loop_block_guard` fixture in `tests/conftest.py` runs a test loop in asyncio debug mode with `slow_callback_duration = 0.1` and fails on any callback of 100 ms or more. `tests/unit/test_event_loop_offload.py` uses it with large inputs and negative controls. Ruff `ASYNC` was checked on the touched files only (global config untouched): the remaining `ASYNC240` hits are the A9 sites above and one `Path.stat` in `parse_file_bounded`.

## Database schema and lifecycle (plan section 4.1 items 1, 2, 5, 6; P2, P3, P9)

Nothing below was benchmarked; these are structural changes with unit tests and an integration test (`tests/integration/test_db_schema_perf.py`, run by the orchestrator).

| Item | Change |
|---|---|
| P2 migration safety | Migration connections get `lock_timeout` 5 s and `statement_timeout` 1 h; index DDL uses `CONCURRENTLY` in `autocommit_block` via `core/migration_support.py`; the `0001` baseline downgrade refuses without `UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE=1`; `scripts/lint_migrations.py` runs in `tests/test_migration_safety.py`. |
| P9 indexes | 0014 drops duplicate/superseded indexes (two identical `document_chunks` expression GINs, one duplicate `relationship_evidence` index), adds 23 FK indexes for delete/cascade/erasure paths, and converts three low-selectivity `status` indexes to partial ones. Effect on delete/purge latency is unmeasured. |
| Lexical search | `document_chunks.content_tsv` stored generated column + GIN; `modules/search/public.py` matches and ranks through it. Same expression, so identical match sets and `ts_rank_cd` scores (verified against the inline expression in the integration test); candidates are no longer re-parsed at query time. Cost: one table rewrite at upgrade (see operations.md). |
| P3 HNSW | The per-generation HNSW index is built with `CREATE INDEX CONCURRENTLY` on a separate autocommit connection before activation, so activation no longer blocks writes to `search_index_items` for the build duration. |
| Outbox retention | `purge_delivered_outbox` now covers every event type (delivered/failed rows only; stage events 7 days, others `OUTBOX_RETENTION_DAYS`, default 30), still bounded to 1000 rows per hourly call. |
| Autovacuum | Scale factors lowered on `event_outbox`, `chat_stream_events`, `realtime_replay_events`, `source_observations`, `system_health_samples`. |
| Partitioning | Not implemented: unique keys on `chat_stream_events` / `realtime_replay_events` cannot carry the partition key (see operations.md). |
| Entity context N+1 | `_resolve_entity_context` (chat retrieval) read about 4 statements/awaits per entity for up to 100 entities. It now calls `entities.get_entities`, `entities.list_entity_evidence_bulk` and `relationships.get_neighbors_bulk` (3 owner calls; redirect resolution is one query round per hop, evidence refs are read in batches of 100, neighbour entity refs in batches of 100), with a per-entity fallback if the batch fails. `entities.get_entity_refs` read mode also resolves redirects in bulk. Results and ordering match the singular contracts (unit tests compare redirect resolution against `resolve_canonical_entity_id`). |

