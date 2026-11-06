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

- Cron split and offsets (P5): recurring jobs no longer share ingestion `max_jobs` slots; no more than 4 jobs share a second, so the :00 and hh:00:00 bursts disappear. Lease-guarded runs stop overlapping and replica duplication. Outbox dispatch and indexing react to commits instead of waiting for a 5 s / 60 s tick (safety net 30 s / 60 s).
- Batch embedding (P7): up to 64 chunks per request and per claim/publication transaction instead of one of each per chunk; expect roughly `batch size` fewer gateway requests and claim/publish round trips for the same index. Unmeasured; the old rate was about 50 chunks per minute tick.
- Jittered exponential retry (P11): contention retries stop returning in lock-step; dead letters make exhausted jobs visible.

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


## Locks, chat flush, SSE and gateway client (plan section 5, P4/P6/P8; behaviour changes, not measurements)

No timing was taken; the figures below are statement or lock counts asserted by unit tests, not latency claims.

| Item | Change | Evidence |
| --- | --- | --- |
| P4 privacy lock | `lock_export_privacy(session, workspace_id, *, shared=False)` now keys the advisory lock per workspace (`privacy_lock_key`) instead of one global key. Consent readers/publishers use `pg_advisory_xact_lock_shared`, privacy writers and erasers the exclusive form on the same key; multi-workspace jobs lock sorted keys. Every call site is classified in `docs/privacy.md`. | `tests/unit/modules/memory/test_privacy_lock_scoping.py` (AST classification, key derivation, ordering). |
| P6 token flush | Every flush still takes the live-run fence and key-share locks on the cited evidence, but between full revalidations it reads one content-free projection (`lock_chat_evidence_state`: ids, generation, status, local_only) instead of Document/Source/chunk ORM rows, chunk text and provenance, and reads all source fences with ONE `IN` query (was one query per source). The full revalidation (separate exact-selection pass plus content rows) runs on the first flush and then at most every `CHAT_FLUSH_FULL_REVALIDATE_SECONDS` (default 2, 0 = every flush). | `tests/unit/modules/chat/test_flush_revalidation_budget.py`. |
| P6 revocation bound | A consent change or source deactivation/generation bump/local-only flip/deletion is seen by the lightweight pass on the very next flush, so publication stops within one flush (at most `STREAM_FLUSH_SECONDS`, 0.4 s, plus lock wait). Only the extra heavy checks (separate selection-fence pass, content-row re-read) can lag, by at most the budget (2 s default; the exact-selection check is still repeated inside the locking read). | same test file. |
| P6 SSE | The worker publishes `chat:response:<id>` after each commit that inserted stream events (`modules/chat/signals.py`), and the API cancel route after its terminal commit. The SSE handler subscribes before its first poll and waits on the signal with a 2 s fallback poll; with no Redis it keeps the 0.1-0.5 s backoff. Event order, `seq` cursor, dedup, `Last-Event-ID` resume and the locked read path are unchanged. An idle stream no longer runs the 5-6 query probe every 0.1-0.5 s. Limit: events committed outside a worker run or the cancel route (recovery, answer submission) are delivered by the fallback poll, up to 2 s later. Connection budget: each open SSE stream holds one dedicated pub/sub connection from `app.state.redis` (shared with rate limiting and auth; pool max 100) and at most `MAX_CHAT_STREAMS_PER_API_PROCESS` (64) streams run per API process, so subscriptions can use up to 64 of 100 connections; a failed subscribe degrades that stream to the poll. A single multiplexed listener or a separate subscription pool is deferred. | `tests/unit/modules/chat/test_sse_redis_signal.py`. |
| P8 gateway client | One pinned `httpx.AsyncClient` per process per (origin, approved CIDRs, allow_global, protocol), per event loop, LRU bounded to 32, closed in the API lifespan and worker shutdown (`core/model_gateway/pool.py`). The transport still resolves and validates DNS on every request and dials only the checked literal IP; credentials are per request, never pooled. | `tests/unit/core/test_gateway_client_pool.py`. |
| Gateway slots | `MODEL_GATEWAY_SLOTS` was already enforced by Redis slot keys shared by all processes. New: when Redis cannot serve the lease (non-connection error, or connection errors for more than 1 s) the call degrades with a warning to a per-process semaphore of the same size, so the limit is per process (up to processes x slots overall) until Redis returns, instead of failing every model call. | `tests/unit/core/test_model_gateway.py` (degraded-mode tests). |

## SLO to k6 mapping (P15)

The SLOs and error budget policy live in [slo.md](slo.md). `make load-test` (or `./scripts/dev.ps1 load-test`) runs `tests/load/all.js` against a disposable Compose stack with the fake model and fails (k6 exit 99) when any threshold below is crossed. The "Result" column is filled from `summary.json` of a recorded run; it stays `pending` until someone runs the profile (none has been executed at the time of writing).

| SLO | k6 metric | Threshold | Scenario | Result |
|---|---|---|---|---|
| SLO-1 availability | `http_req_failed` | `rate<0.005` | all | pending |
| SLO-2a dashboard | `http_req_duration{endpoint:dashboard}` | `p(95)<400`, `p(99)<800` ms | `read_mix` | pending |
| SLO-2b search | `http_req_duration{endpoint:search}` | `p(95)<800`, `p(99)<1500` ms | `search` | pending |
| SLO-2c documents | `http_req_duration{endpoint:documents}` | `p(95)<300`, `p(99)<600` ms | `read_mix` | pending |
| SLO-3 chat TTFT | `chat_ttft_ms` | `p(95)<2000`, `p(99)<4000` ms | `chat` (1-token stream) | pending |
| SLO-3b chat send | `chat_send_ms` | `p(95)<500` ms | `chat` | pending |
| SLO-4 ingest lag | `ingest_lag_ms` | `p(95)<30000`, `p(99)<60000` ms | `ingest` | pending |
| SLO-5 SSE stream open (proxy; per-event delivery is a follow-up) | `chat_sse_open_ms` | `p(95)<500` ms | `chat` | pending |
| SLO-6 queue age | `ingest_queue_age_ms` | `p(95)<5000` ms | `ingest` | pending |
| SLO-7 correctness | `checks` | `rate>0.99` | all | pending |

Mapping to the measurement record above: `latency.dashboard_ms`, `latency.search_ms`, `latency.chat_first_token_ms` and `latency.realtime_delivery_ms` take the p50/p95/p99 of SLO-2a, SLO-2b, SLO-3 and SLO-5; `queue_delay_ms` takes SLO-6; `workload.concurrent_clients` is the peak VU count of `read_mix` (10) plus the arrival-rate scenarios; `measurement_commands` is the exact `make load-test` invocation including `LOAD_*`/`SLO_*` variables; `raw_evidence_paths` is the run's output directory (`summary.json`, `environment.txt`, `compose-logs.txt`).

### Load-run results template

Copy per run. Only a run on the target host (2 vCPU / 8 GiB) counts as SLO evidence; runs with `SLO_LATENCY_SCALE` other than 1 do not.

```json
{
  "run_utc": null,
  "source_revision": null,
  "host": {"cpus": null, "memory_gib": null, "target_host": false},
  "command": null,
  "k6_image": "grafana/k6:0.57.0",
  "thresholds_passed": null,
  "metrics": {
    "http_req_failed_rate": null,
    "dashboard_p95_ms": null, "dashboard_p99_ms": null,
    "search_p95_ms": null, "search_p99_ms": null,
    "documents_p95_ms": null, "documents_p99_ms": null,
    "chat_ttft_p95_ms": null, "chat_ttft_p99_ms": null,
    "chat_send_p95_ms": null, "chat_sse_open_p95_ms": null,
    "ingest_lag_p95_ms": null, "ingest_lag_p99_ms": null,
    "ingest_queue_age_p95_ms": null
  },
  "resources": {"peak_container_memory_bytes": null, "cpu_percent": null},
  "notes": null
}
```

## Hot-path micro-benchmarks (baseline)

`tests/benchmarks/test_hot_path_benchmarks.py` times pure functions that run on every request or chunk. The tests are marked `benchmark` and skipped unless `RUN_BENCHMARKS=1`:

```bash
RUN_BENCHMARKS=1 BENCHMARK_OUT=bench.json python -m pytest tests/benchmarks -q -s -p no:cacheprovider
```

Baseline from the implementation container (4 vCPU Intel Xeon @ 2.10 GHz, Python 3.12, shared host, single process, 2026-11-04). It is a development-machine number, not target-host evidence; re-run on the 2 vCPU host before using it for capacity decisions. Times are microseconds per call.

| Function | Input | min | median | p95 |
|---|---|---|---|---|
| `sql_guard.check_select_policy` | one-table aggregate | 97 | 105 | 135 |
| `sql_guard.check_select_policy` | two CTEs, join, ILIKE, ORDER BY | 340 | 378 | 426 |
| `sql_guard.check_select_policy` | rejection (`chat_messages`) | 38 | 45 | 61 |
| `core.chunking.chunk_text` | ~60 KB Markdown with headings | 9149 | 9367 | 16901 |
| `citations.validate_citations` | 5 citations, 20 evidence items | 36 | 38 | 55 |
| `citations.validate_citations` | 20 citations, 100 evidence items | 145 | 153 | 189 |
| `worker._parse_sse_chunk` | content delta | 2.1 | 2.4 | 2.8 |
| `worker._parse_sse_chunk` | reasoning delta | 1.8 | 2.0 | 2.3 |
| `stream.format_sse_event` | `message.delta` frame | 1.8 | 1.9 | 2.1 |

Reading: chunking dominates (about 9 ms per 60 KB document, run in a worker, so it adds to ingest lag but not request latency); the SQL guard costs well under a millisecond even for a join, and per-chunk SSE parsing and formatting are a few microseconds, far below the 0.4 s stream flush interval, so they do not threaten the TTFT SLO.

## k6 load profile baseline (2026-11-05, development host, indicative only)

Not target-host evidence: the record above stays `target_host_verified: false`. This is the first recorded `tests/load/all.js` run (unmodified durations, `LOAD_DURATION_SCALE=1`, 3.5 min, 21 VUs max) at source revision `6fc29ba4` plus the k6 cookie fix in `17c5b1e8`.

- Host: Windows 11, Docker Desktop, 14 CPUs / 31 GiB visible to Docker (SLOs are defined for 2 vCPU / 8 GiB; containers carry the compose memory limits only, no CPU cap). Fake model, `grafana/k6:0.57.0` on the stack network, 8 seed documents, project `bbd-os-test-k6` (MinIO not started). `scripts/load-test.sh` was not used verbatim (no `python3`/`nproc` on the host); its `docker run` was replicated by hand with the same environment.
- A first attempt failed 98 % of requests with 401: `tests/load/lib/auth.js` read `.value` of k6 cookie-jar entries, which are plain strings, so VUs sent empty cookies. Fixed in `17c5b1e8`; the table is from the second run.

| SLO | Metric | Measured | Threshold | Result |
|---|---|---|---|---|
| SLO-1 | `http_req_failed` | 0.00 % (0 of 1118) | < 0.5 % | pass |
| SLO-2a | dashboards p95 / p99 | 21.7 / 28.0 ms | 400 / 800 ms | pass |
| SLO-2b | search p95 / p99 | 36.1 / 52.3 ms | 800 / 1500 ms | pass |
| SLO-2c | documents p95 / p99 | 20.8 / 26.1 ms | 300 / 600 ms | pass |
| SLO-3 | chat TTFT p50 / p95 / p99 | 2422 / 2594 / 2915 ms | p95 < 2000, p99 < 4000 ms | **p95 FAIL**, p99 pass |
| SLO-3b | chat send p95 | 44 ms | 500 ms | pass |
| SLO-4 | ingest lag p95 / p99 | 2252 / 2307 ms | 30 / 60 s | pass |
| SLO-5 | SSE open p95 | 35 ms | 500 ms | pass |
| SLO-6 | ingest queue age p95 | 2225 ms | 5 s | pass |
| SLO-7 | checks | 100 % (1234 of 1234) | > 99 % | pass |

Throughput: 1118 HTTP requests, 5.26 req/s, 539 iterations; `chat_completion_ms` (20 tokens) avg 2.5 s. k6 exit 99 solely because of the TTFT p95 threshold.

Resources at about 100 s into the run (`docker stats --no-stream`): api 17.9 % CPU / 554 MiB of 1 GiB; chat-worker 9.1 % / 269 MiB of 768 MiB; worker 8.3 % / 262 MiB of 1 GiB; postgres 5.1 % / 232 MiB of 2.5 GiB; cron-worker 0.4 % / 264 MiB of 512 MiB; model-worker 0.1 % / 252 MiB of 768 MiB; web 46 MiB; redis 4 MiB. `pg_stat_activity` (one sample): 20 connections, 1 active, 19 idle on `ClientRead`, 0 idle in transaction, no lock or IO waits.

Observation: the only SLO miss is TTFT p95 (2.59 s vs 2 s) with a 2.2 s floor (min 2230 ms) for a one-token fake reply while API reads are about 15 ms. Ingest queue age shows the same 2.2 s floor, so a fixed dispatch delay (about 2 s) shared by the chat and ingestion job paths is the likely cause (hypothesis, not yet traced: check the outbox/arq poll interval and the 0.4 s stream flush). The machine is far from saturated, so this is latency from a tick, not load. Not tuned and no threshold changed; per the SLO policy a threshold is not relaxed to make a run pass.

### Re-run after the SSE terminal-close fix (2026-11-05, same host and procedure)

Root cause of the TTFT miss above (traced, then fixed in `4c61573b`): after the poll that delivered the batch containing `message.done`, the chat SSE handler waited one more `STREAM_SIGNAL_FALLBACK_INTERVAL` (2.0 s) for a wake that never came (the worker had finished) before a second poll saw "terminal and no newer events" and closed the stream. Every finished stream therefore held its concurrency permit and Redis pub/sub subscription 2 s too long, and the k6 `chat_ttft_ms` (measured to stream end) carried that floor. The handler now ends the stream right after yielding a terminal run's final, non-size-capped batch. Same full `all.js` run (`LOAD_DURATION_SCALE=1`, `grafana/k6:0.57.0`, fake model, 8 seed documents, project `bbd-os-test-k6b`, `scripts/load-test.sh` replicated by hand as before).

| SLO | Metric | Baseline (`6fc29ba4`) | After fix | Threshold | Result |
|---|---|---|---|---|---|
| SLO-1 | `http_req_failed` | 0.00 % (0 of 1118) | 0.00 % (0 of 1089) | < 0.5 % | pass |
| SLO-2a | dashboards p95 / p99 | 21.7 / 28.0 ms | 40.9 / 59.9 ms | 400 / 800 ms | pass |
| SLO-2b | search p95 / p99 | 36.1 / 52.3 ms | 48.3 / 80.3 ms | 800 / 1500 ms | pass |
| SLO-2c | documents p95 / p99 | 20.8 / 26.1 ms | 39.0 / 68.3 ms | 300 / 600 ms | pass |
| SLO-3 | chat TTFT p50 / p95 / p99 | 2422 / 2594 / 2915 ms | 544 / 714 / 948 ms | p95 < 2000, p99 < 4000 ms | pass |
| SLO-3b | chat send p95 | 44 ms | 64 ms | 500 ms | pass |
| SLO-4 | ingest lag p95 / p99 | 2252 / 2307 ms | 2357 / 2394 ms | 30 / 60 s | pass |
| SLO-5 | SSE open p95 | 35 ms | 49 ms | 500 ms | pass |
| SLO-6 | ingest queue age p95 | 2225 ms | 2338 ms | 5 s | pass |
| SLO-7 | checks | 100 % (1234) | 100 % (1208) | > 99 % | pass |

All thresholds passed (k6 exit 0). `chat_completion_ms` avg fell from 2.5 s to 0.56 s. The ~2.2 s floor on ingest lag and queue age is unchanged, so it is a separate fixed dispatch delay on the ingestion path (not traced; within its SLO). Read-path latencies are slightly higher than the baseline run, within run-to-run noise on a shared development host. `tests/integration/test_p14_chat_sse.py` (4) and `test_p14_chat_worker.py` (5) passed on the same stack.

### Re-run under a 2 CPU / 8 GiB application-tier budget (2026-11-06, `2176e4d5`)

Same full `tests/load/all.js` run (`LOAD_DURATION_SCALE=1`, `grafana/k6:0.57.0`, fake model, 8 seed documents, 3 min 32 s, 21 VUs max) on the same 14 CPU / 31 GiB Docker Desktop host, but with a throwaway compose override (not committed) applying `deploy.resources.limits` through `scripts/load-test.sh`'s compose file chain (`-f docker-compose.yml -f docker-compose.test.yml -f <override>`; the script was run from a copy because the host has no working `python3`). Still not target-host evidence (`target_host_verified: false`).

| Service | CPU limit | Memory limit |
|---|---|---|
| api | 0.6 | 1.5 GiB |
| postgres | 0.6 | 2.5 GiB |
| worker | 0.3 | 1.5 GiB |
| chat-worker | 0.2 | 1.0 GiB |
| model-worker | 0.1 | 768 MiB |
| cron-worker | 0.1 | 512 MiB |
| redis | 0.1 | 256 MiB |
| Total | 2.0 | about 8.0 GiB |

No pgbouncer in this stack. web, fake-model, fake-local and migrate (test scaffolding, not application tier) were left unconstrained.

| SLO | Metric | Previous re-run | Under 2 CPU / 8 GiB | Threshold | Result |
|---|---|---|---|---|---|
| SLO-1 | `http_req_failed` | 0.00 % (0 of 1089) | 0.00 % (0 of 1139) | < 0.5 % | pass |
| SLO-2a | dashboards p95 / p99 | 40.9 / 59.9 ms | 22.7 / 52.7 ms | 400 / 800 ms | pass |
| SLO-2b | search p95 / p99 | 48.3 / 80.3 ms | 33.3 / 202.3 ms | 800 / 1500 ms | pass |
| SLO-2c | documents p95 / p99 | 39.0 / 68.3 ms | 40.0 / 74.4 ms | 300 / 600 ms | pass |
| SLO-3 | chat TTFT p50 / p95 / p99 | 544 / 714 / 948 ms | 738 / 3020 / 8120 ms | p95 < 2000, p99 < 4000 ms | **p95 FAIL, p99 FAIL** |
| SLO-3b | chat send p95 | 64 ms | 43 ms | 500 ms | pass |
| SLO-4 | ingest lag p95 / p99 | 2357 / 2394 ms | 9130 / 17540 ms | 30 / 60 s | pass |
| SLO-5 | SSE open p95 | 49 ms | 86.7 ms | 500 ms | pass |
| SLO-6 | ingest queue age p95 | 2338 ms | 8960 ms | 5 s | **FAIL** |
| SLO-7 | checks | 100 % (1208) | 100 % (1226) | > 99 % | pass |

k6 exit 99: `chat_ttft_ms` and `ingest_queue_age_ms` crossed. Read-path latencies are unaffected (the 0.6 CPU api handles 10 read VUs fine); the regressions are all in the asynchronous paths (chat worker dispatch, ingestion queue). `chat_completion_ms` avg rose from 0.56 s to 0.76 s.

Peak `docker stats` samples (every 15 s, CPU % is of one core): api 60 % of its 0.6 cap (saturated, throttled), worker 31 % of 0.3 (saturated), chat-worker 20 % of 0.2 (saturated), cron-worker 10 % of 0.1, model-worker 11 % of 0.1, postgres 21 % of 0.6, redis 2 %. Peak memory: api 538 MiB, worker 283 MiB, chat-worker 268 MiB, model-worker 275 MiB, cron-worker 303 MiB, postgres 256 MiB, redis 4 MiB, far below the limits. The budget is CPU-bound, not memory-bound: the 0.2 CPU chat-worker and 0.3 CPU worker were pinned at their caps, which matches the TTFT and queue-age tails. Other splits (for example more CPU to chat-worker/worker, less to postgres) were not tried. Single run, so tail values carry run-to-run noise.

#### Rebalanced split (2026-11-06, `ed65fa7a`)

Same procedure, host and compose-override mechanism, with the CPU moved from postgres and api to the workers: api 0.5 / 1.5 GiB, postgres 0.3 / 2.5 GiB, worker 0.5 / 1.5 GiB, chat-worker 0.4 / 1.0 GiB, model-worker 0.1 / 768 MiB, cron-worker 0.1 / 512 MiB, redis 0.1 / 256 MiB (total 2.0 CPU, about 8.0 GiB). Single full run (`LOAD_DURATION_SCALE=1`); `summary.json` was written this time and the figures below come from it. Still not target-host evidence.

| SLO | Metric | First 2 CPU split | Rebalanced split | Threshold | Result |
|---|---|---|---|---|---|
| SLO-1 | `http_req_failed` | 0.00 % (0 of 1139) | 0.00 % (0 of 1180) | < 0.5 % | pass |
| SLO-2a | dashboards p95 / p99 | 22.7 / 52.7 ms | 25.9 / 80.6 ms | 400 / 800 ms | pass |
| SLO-2b | search p95 / p99 | 33.3 / 202.3 ms | 65.3 / 72.7 ms | 800 / 1500 ms | pass |
| SLO-2c | documents p95 / p99 | 40.0 / 74.4 ms | 40.0 / 68.5 ms | 300 / 600 ms | pass |
| SLO-3 | chat TTFT p50 / p95 / p99 | 738 / 3020 / 8120 ms | 509 / 638 / 1357 ms | p95 < 2000, p99 < 4000 ms | pass |
| SLO-3b | chat send p95 | 43 ms | 103 ms | 500 ms | pass |
| SLO-4 | ingest lag p95 / p99 | 9130 / 17540 ms | 15316 / 34406 ms | 30 / 60 s | pass |
| SLO-5 | SSE open p95 | 86.7 ms | 95.7 ms | 500 ms | pass |
| SLO-6 | ingest queue age p95 | 8960 ms | 15290 ms | 5 s | **FAIL** |
| SLO-7 | checks | 100 % (1226) | 100 % (1240) | > 99 % | pass |

k6 exit 99: only `ingest_queue_age_ms` crossed. Giving chat-worker 0.4 CPU fixed the chat TTFT tail, but the ingest path got worse (queue age p95 8.96 s to 15.3 s) because postgres at 0.3 CPU and the worker share the ingestion bursts.

Peak `docker stats` samples (about 12 s interval, CPU % of one core): api 49 % of 0.5, chat-worker 39 % of 0.4 (near cap), worker 26 % of 0.5, cron-worker 11 % of 0.1, model-worker 11 % of 0.1, postgres 20 % of 0.3, redis 2 %. Peak memory: api 540 MiB, worker 282 MiB, chat-worker 272 MiB, model-worker 289 MiB, cron-worker 273 MiB, postgres 249 MiB, redis 5 MiB, all far below limits. Neither split satisfied all SLOs at 2 CPU; the ingest queue age SLO needs a split that keeps the worker/postgres share from the first run while keeping chat-worker at 0.4 (for example by trimming api below 0.5), which was not tried. Single run, so tail values carry noise.

#### Cron-worker split (2026-11-06)

Hypothesis: the ingest queue age was driven by the cron-worker (outbox dispatcher) sitting at its 0.1 CPU cap in both earlier runs. Same procedure and host, one full run (`LOAD_DURATION_SCALE=1`), only the CPU split changed: api 0.4 / 1.5 GiB, postgres 0.3 / 2.5 GiB, worker 0.4 / 1.5 GiB, chat-worker 0.4 / 1.0 GiB, model-worker 0.1 / 768 MiB, cron-worker 0.3 / 512 MiB, redis 0.1 / 256 MiB (total 2.0 CPU, about 8.0 GiB). k6 exit 0: every SLO threshold passed. Still not target-host evidence.

| SLO | Metric | First split | Rebalanced split | Cron-worker split | Threshold | Result |
|---|---|---|---|---|---|---|
| SLO-1 | `http_req_failed` | 0.00 % | 0.00 % | 0.00 % (0 of 1119) | < 0.5 % | pass |
| SLO-2a | dashboards p95 / p99 | 22.7 / 52.7 ms | 25.9 / 80.6 ms | 90.8 / 220.4 ms | 400 / 800 ms | pass |
| SLO-2b | search p95 / p99 | 33.3 / 202.3 ms | 65.3 / 72.7 ms | 164.2 / 290.2 ms | 800 / 1500 ms | pass |
| SLO-2c | documents p95 / p99 | 40.0 / 74.4 ms | 40.0 / 68.5 ms | 87.1 / 145.0 ms | 300 / 600 ms | pass |
| SLO-3 | chat TTFT p95 / p99 | 3020 / 8120 ms | 638 / 1357 ms | 845 / 1430 ms | p95 < 2000, p99 < 4000 ms | pass |
| SLO-3b | chat send p95 | 43 ms | 103 ms | 110 ms | 500 ms | pass |
| SLO-4 | ingest lag p95 / p99 | 9130 / 17540 ms | 15316 / 34406 ms | 3459 / 3865 ms | 30 / 60 s | pass |
| SLO-5 | SSE open p95 | 86.7 ms | 95.7 ms | 106.6 ms | 500 ms | pass |
| SLO-6 | ingest queue age p95 | 8960 ms | 15290 ms | 2733 ms | 5 s | pass |
| SLO-7 | checks | 100 % | 100 % | 100 % (1233) | > 99 % | pass |

Peak `docker stats` samples (8 s sleep between samples, CPU % of one core): chat-worker 40 % of 0.4 (at cap), api 40 % of 0.4 (at cap), worker 39 % of 0.4 (at cap), cron-worker 31 % of 0.3 (at cap), model-worker 11 % of 0.1, postgres 7 % of 0.3, redis 1 % of 0.1. Peak memory all far below limits (api 540 MiB, cron-worker 290 MiB, chat-worker 276 MiB, postgres 261 MiB, model-worker 259 MiB, worker 248 MiB).

Verdict: the hypothesis is supported. Raising cron-worker from 0.1 to 0.3 CPU (paid for by api and worker at 0.4) cut queue age p95 from 15.3 s to 2.7 s and ingest lag p95 from 15.3 s to 3.5 s, and this is the first 2 CPU split that passes every SLO. The cron-worker used its whole 0.3 allowance, so the dispatcher was CPU-starved, not postgres. This is a single run and the split was changed in several services at once (api and worker went 0.5 to 0.4), so the cron-worker share is the strongest explanation rather than isolated proof; dashboards and search latencies rose, still well within thresholds.

Dispatcher cadence (from code): `dispatch_pending_work` runs as a cron safety net every 30 s (`apps/worker/main.py:474`, `CronSpec(dispatch_pending_work, 30, run_at_startup=True)`) and is also woken on commit of every published outbox event (`core/events/publisher.py:79`, `notify_on_commit(session, "dispatch_pending_work")`). Each pass reserves one page of at most 100 outbox identities (`core/events/dispatch.py:208` and `:219`, `.limit(100)`). Queue age therefore has no config-set floor beyond the commit wake; the 30 s period only bounds recovery when a wake is lost, so queue age under load is set by how fast the cron-worker can drain 100-row pages, which is CPU-bound.

Reproduce the passing cron-worker split: `LOAD_TEST_LIMITS=1 scripts/load-test.sh` layers the committed `docker-compose.limits.yml` (api 0.4 CPU / 1.5 GiB, postgres 0.3 / 2.5 GiB, worker 0.4 / 1.5 GiB, chat-worker 0.4 / 1 GiB, model-worker 0.1 / 768 MiB, cron-worker 0.3 / 512 MiB, redis 0.1 / 256 MiB) after `docker-compose.test.yml`. Outside the script: `docker compose -f docker-compose.yml -f docker-compose.limits.yml up -d`. The caps only model the 2 CPU / 8 GiB budget on a larger host; they are not target-host evidence.
