# Observability and Langfuse

The built-in Operations view is the source for bounded run summaries, token usage, quality counts, and process-local metrics. It is owner-protected and remains available without a telemetry sink. Missing provider usage or dependency probes stay unknown; they are not reported as zero or healthy.

## Follow an error to a run

1. For an API error, copy `error.requestId` from the response (or the `X-Request-ID` response header) and search the API logs for `request_id=<value>`. Error responses omit submitted values and exception details.
2. For work accepted for background processing, retain the returned durable `run_id`. Open Settings → advanced operations, filter to the run type, and open its row. The detail shows the run ID, outcome, timestamps, available usage, and any persisted correlation IDs. Use the run ID in worker logs and the owning run-detail view.
3. A request ID identifies the HTTP request; it does not prove which later worker execution ran unless the durable run ID is also available. Correlation fields that were not persisted are shown as unknown.
4. A run or request error does not mean the whole system is unhealthy. `/health` is the process check, `/api/v1/system/ready` is the bounded database readiness check, and the authenticated System health view reports component-specific checks. An unconfigured optional service has unknown/unavailable state; it is not evidence of failure or health.

## Logs and polling

No-change maintenance and polling cycles are silent. Maintenance writes a durable summary only when it performs work or the next summary is due; actual cleanup is debug logged, while actionable failures are warned. A quiet log during an unchanged poll is expected. Run records and retained request IDs are the durable investigation path; Redis metrics snapshots are short-lived and best effort.

## Optional remote Langfuse profile

Langfuse export is off by default and no Langfuse container is part of the Compose stack. `.env` is the only switch: the services read `LANGFUSE_ENABLED` from it, and it stays off unless you set it to `true` together with `TELEMETRY_EGRESS_APPROVED=true`. It sends only OpenTelemetry generation spans for model calls: fixed service/capability names, model identity when safe, outcome, nullable input/output token counts, and UUID request/run correlation IDs. Prompt, message, document, input, output, completion, source, user, and exception content are not exported. There is no content-capture option. The API and worker use a bounded process-local queue; when it is full, the total send-and-cleanup deadline expires, or Langfuse rejects a request, that telemetry is dropped and the knowledge write or agent result continues. The sender has a cooperative two-second overall deadline plus HTTPX per-I/O timeouts and checks only the streamed response status without buffering its body. Export health is not included in application readiness; verify receipt in Langfuse itself.

To opt in, place the endpoint and Langfuse project keys in the protected server `.env`, then explicitly enable both flags:

```dotenv
LANGFUSE_ENABLED=true
TELEMETRY_EGRESS_APPROVED=true
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Use the correct regional Langfuse host or the HTTPS origin of the owner-selected self-hosted service. Credentials are passed only to the API and the four worker containers and are never sent to the browser. The exporter requires an HTTPS origin without userinfo, path, query, or fragment, does not follow redirects, and does not use environment proxy variables. The owner must explicitly approve the destination and protect `.env`; do not put keys in Compose files, command arguments, screenshots, or logs.

Enable export by setting the values above in `.env` and recreating the services:

```powershell
docker compose up -d
```

To disable export, set `LANGFUSE_ENABLED=false` (or remove it) in `.env` and recreate the services the same way.

With the flag off, the exporter stays disabled even if `.env` still contains keys. No Langfuse containers, local database, ClickHouse, or object store are added by this profile. Incomplete settings leave export disabled without affecting startup. The application does not probe the remote endpoint at startup, and an enabled flag does not prove that spans were received.

The exporter uses Langfuse's current OpenTelemetry HTTP/JSON endpoint (`POST /api/public/otel/v1/traces`) with Basic authentication and ingestion version 4. It does not use the legacy v3 batch-ingestion API. See [Langfuse OpenTelemetry integration](https://langfuse.com/integrations/native/opentelemetry) and the [Langfuse API reference](https://api.reference.langfuse.com/); the v3 ingestion endpoint is deprecated and scheduled to stop accepting trace events on Langfuse Cloud on 2026-11-16. The application does not expose remote delivery state in its health or metrics APIs, so absent spans mean delivery is unknown until checked in Langfuse.

No with/without performance measurements have been recorded. Measure both profiles on the target 2-core/8-GB host during deferred acceptance; this implementation-stage build is not capacity evidence.

## Database

Postgres runs with `pg_stat_statements` preloaded, slow statements (>500 ms), autovacuum runs (>10 s) and lock waits logged. Run these through `psql` as the application owner (read-only).

```sql
-- Dead tuples and last autovacuum per table
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20;

-- Largest tables including indexes and TOAST
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total
FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 20;

-- Top statements by total execution time
SELECT calls, round(total_exec_time::numeric) AS total_ms, round(mean_exec_time::numeric, 2) AS mean_ms, left(query, 120) AS query
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
```

`pg_stat_statements` is created by migration only when the library is preloaded; reset counters with `SELECT pg_stat_statements_reset();`. Row retention is enforced by the worker retention jobs (expired chat runs, receipts, replay window, backup activity); a growing `n_dead_tup` or table size on those tables means a job is not running.

### Additional monitoring (2026-10-13)

```sql
-- Long idle-in-transaction sessions (a lock held across external I/O would show up here)
SELECT pid, now() - xact_start AS age, state, left(query, 100) AS last_query
FROM pg_stat_activity WHERE state = 'idle in transaction' ORDER BY xact_start LIMIT 20;

-- Invalid indexes (an interrupted CREATE INDEX CONCURRENTLY)
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;

-- Row counts of tables that must stay bounded
SELECT 'event_outbox' AS t, count(*) FROM event_outbox
UNION ALL SELECT 'chat_stream_events', count(*) FROM chat_stream_events;
```

Model-gateway breaker state lives in Redis; each process exports it as `umwelt_model_breaker_open_until_timestamp_seconds` (updated when a failure trips the breaker and when a call is rejected; see the `UmweltModelBreakerOpen` runbook) and an open breaker also shows as `ModelGatewayUnavailable` in logs; see [operations.md](operations.md) for reset and the retention job inventory. Slow statements and lock waits are in the Postgres container log (`docker compose logs postgres`).

## Prometheus metrics, OpenTelemetry traces and alerts

Production telemetry is additive: it never changes business behaviour, every recording helper swallows its own failures, and each part is off unless configured. The Operations view and Langfuse above are unchanged.

### Enabling

| Setting | Default | Effect |
| --- | --- | --- |
| `METRICS_TOKEN` | empty | API `GET /metrics` requires `Authorization: Bearer <token>` (constant-time compare, any network location). Generate a long random value; keep it only in `.env` and the Prometheus secret file. |
| *(no token)* | | Note: private-range peers include other LAN devices, not only the compose bridge; set `METRICS_TOKEN` on shared networks. In `development`/`test` (or `auto` with a local origin) `/metrics` answers only direct loopback/private peers without `X-Forwarded-*`/`Forwarded` headers. In `production` it is disabled (404). |
| `METRICS_WORKER_PORT` | `0` (off) | Starts a plain HTTP metrics server in each worker process: worker on the port, chat worker on port+1, model worker on port+2, cron worker on port+3 (`METRICS_PORT_OFFSETS` in `apps/worker/main.py`). |
| `METRICS_WORKER_BIND` | `127.0.0.1` | Bind address for the worker servers. The monitoring overlay sets `0.0.0.0` so Prometheus can reach them on the compose network; the ports are never published to the host. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty (off) | OTLP/HTTP base URL (for example `http://otel-collector:4318`). Empty means no OpenTelemetry module is imported or patched: zero overhead. |
| `OTEL_TRACES_SAMPLE_RATIO` | `1.0` | Head sampling ratio for new traces (a sampled parent is always honoured). |

`/metrics` is served by middleware, not a route, so it is absent from the route table and OpenAPI. Queue, outbox and event-loop samplers run in the API process only when the endpoint can be served.

### Monitoring profile

```powershell
# .env: METRICS_TOKEN=<secret>  GRAFANA_ADMIN_PASSWORD=<secret>
# infrastructure/monitoring/metrics_token.local: same token, no trailing newline (git-ignored)
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml --profile monitoring up -d
```

Prometheus (`127.0.0.1:9090`) and Grafana (`127.0.0.1:3001`, dashboard folder "Umwelt-OS", "Umwelt-OS overview") bind to loopback only; use an SSH tunnel for remote access. Rules live in `infrastructure/monitoring/alerts.yml`; no Alertmanager is bundled, so alerts are visible in the Prometheus UI and Grafana until you add a receiver. Including the overlay file without `--profile monitoring` only switches on the worker metrics ports (internal to the compose network).

### Metrics catalogue

Labels are bounded: route templates (never raw paths or ids), fixed job/queue names, provider display names, enumerated outcomes. No user ids, URLs, prompts or document text are ever labels.

| Metric | Type | Labels | Process | Meaning |
| --- | --- | --- | --- | --- |
| `umwelt_http_requests_total` | counter | `method`, `route`, `status` | api | Requests; unmatched paths share `route="unmatched"`. |
| `umwelt_http_request_duration_seconds` | histogram | `method`, `route` | api | Request latency. |
| `umwelt_db_pool_size` / `_checked_out` / `_overflow` / `_max_connections` | gauge | `pool` (engine index) | all | SQLAlchemy pool occupancy, read at scrape time. |
| `umwelt_arq_queue_depth` | gauge | `queue` | api | Jobs in `arq:queue`, `arq:model`, `arq:chat`, `arq:cron`. |
| `umwelt_arq_queue_oldest_job_age_seconds` | gauge | `queue` | api | Age of the oldest *due* job (deferred jobs excluded). |
| `umwelt_outbox_pending_count` | gauge | none | api | `event_outbox` rows in `pending`/`queued`. |
| `umwelt_outbox_oldest_pending_age_seconds` | gauge | none | api | Outbox lag. |
| `umwelt_outbox_rows_total` | gauge | none | api | Estimated `event_outbox` rows (`pg_class.reltuples`, not `count(*)`). |
| `umwelt_outbox_purge_backlog` | gauge | none | api | `delivered`/`failed` rows already past retention (capped at 100k per status); 0 when the hourly purge keeps up. |
| `umwelt_cron_job_duration_seconds` | histogram | `job` | cron-worker, chat-worker | Scheduled-job run time (scheduled ticks, wake-ups and catch-up runs, recorded once per lease-guarded run by `core.cron.record_cron_run`; lease-held skips are not recorded). |
| `umwelt_cron_job_last_success_timestamp_seconds` | gauge | `job` | cron-worker, chat-worker | Unix time of last success. |
| `umwelt_cron_job_failures_total` | counter | `job` | cron-worker, chat-worker | Failed runs. |
| `umwelt_jobs_total`, `umwelt_job_duration_seconds` | counter, histogram | `job`, `outcome` | all workers | Event-driven (instrumented) jobs. |
| `umwelt_ingestion_stage_total` | counter | `stage` (`upload`, `normalize`, `ingest`), `outcome` | worker | Ingestion stage executions. |
| `umwelt_model_breaker_open_until_timestamp_seconds` | gauge | `provider` | workers, api | Per-process view: unix time until which a provider breaker was last seen open (set on trip and on rejection); open now means value > `time()`. |
| `umwelt_model_breaker_rejections_total` | counter | `provider` | workers, api | Calls rejected by an open breaker. |
| `umwelt_event_loop_lag_seconds`, `umwelt_event_loop_lag_histogram_seconds` | gauge, histogram | none | api, workers with metrics on | Sleep overshoot of a 0.5 s timer. |
| `umwelt_chat_time_to_first_token_seconds` | histogram | `capability` | chat worker | Model request to first streamed token. |
| `umwelt_metrics_sample_errors_total` | counter | `sampler` | api | Failed periodic samples (Redis or database unreachable). |

The existing process-local JSON snapshots used by the Operations view are unchanged; `umwelt_job*` mirrors them for Prometheus.

### Tracing

With `OTEL_EXPORTER_OTLP_ENDPOINT` set, the API (FastAPI, SQLAlchemy, httpx, Redis) and each worker export spans. A request that enqueues a job passes a W3C `traceparent` in the job kwargs (`_trace_carrier`); the worker pops it before calling the job, so job functions never see it, and runs the job in a consumer span of the same trace. A worker with tracing off still strips the carrier. Jobs enqueued from inside a worker start a new trace.

Exported spans are scrubbed before leaving the process: request/response bodies and headers, URL query strings, fragments and userinfo, bound SQL parameters, exception messages and stack traces (only `exception.type` is kept) and span status descriptions, SQL literals (replaced by `?`) and Redis arguments (only the command name is kept) are never exported. If scrubbing fails the span is dropped.

### SLO hooks

Suggested objectives to track from these series (adjust to the owner's baseline):

* Availability: `1 - sum(rate(umwelt_http_requests_total{status=~"5.."}[30d])) / sum(rate(umwelt_http_requests_total[30d]))` at or above 99.5%.
* Latency: `histogram_quantile(0.95, sum by (le) (rate(umwelt_http_request_duration_seconds_bucket[7d])))` under 1 s for non-streaming routes.
* Freshness: `umwelt_outbox_oldest_pending_age_seconds` under 60 s for 99% of samples; `umwelt_arq_queue_oldest_job_age_seconds` under 120 s.
* Chat responsiveness: p95 of `umwelt_chat_time_to_first_token_seconds` under 5 s.

### Alert runbooks

Each alert's `runbook` annotation points at the heading below.

### UmweltApi5xxRateHigh

More than 5% of requests return 5xx for 10 minutes. Break down `umwelt_http_requests_total{status=~"5.."}` by `route`; take a `request_id` from the logs of a failing call (see "Follow an error to a run"). Check `/api/v1/system/ready` (database), the DB pool and event-loop alerts, then the latest deploy. Roll back if it started with a release.

### UmweltApiLatencyP95High

p95 above 2 s on non-streaming routes. Find the slow `route` on the dashboard, then use the Database queries above (`pg_stat_statements`, idle-in-transaction) and the pool panel. A coinciding event-loop lag points at blocking work on the API loop; a coinciding pool alert points at connection starvation.

### UmweltQueueDepthHigh

More than 200 jobs waiting for 15 minutes. Identify the `queue`: `arq:queue` (main worker: ingestion and other event-driven jobs), `arq:cron` (cron worker: wake-ups and catch-up runs), `arq:model` (remote model jobs), `arq:chat`. Confirm the matching worker container is up and its `umwelt_jobs_total` is moving; a slow upstream shows as long `umwelt_job_duration_seconds` and an open breaker. Restart a stuck worker; do not flush Redis.

### UmweltQueueOldestJobTooOld

The oldest due job has waited over 5 minutes: workers are stuck or down even if depth is small. Check worker health (`docker compose ps`), the worker health keys in the System health view, and heavy-slot contention (`process_uploaded_file` / `process_graph_operation` retries).

### UmweltOutboxLagHigh

Undelivered outbox events are older than 5 minutes: the `dispatch_pending_work` cron is not running or is failing. Check `UmweltCronJobFailing` / `UmweltCronJobStale` for `dispatch_pending_work`, the queue age alert, and the database. Events are durable and at-least-once; recovery after the worker returns is automatic.

### UmweltOutboxPurgeBacklog

Terminal `event_outbox` rows past their retention cutoff stayed above zero for 6 hours. The hourly purge deletes at most 1000 rows per run (24k/day), so either `purge_delivered_outbox` is failing (check `UmweltCronJobFailing` for it) or the inflow exceeds that bound. See [operations](operations.md#why-event_outbox-is-not-partitioned) for the revisit trigger and the bounded drain loop to add first.

### UmweltCronJobFailing

A scheduled job failed three or more times in 15 minutes. Read the worker log for the `job` name (exceptions are logged by arq). If it is `dispatch_pending_work`, `reconcile_*` or `recover_*`, durable work is delayed but not lost. Fix the cause (database, Redis, provider) and the next tick recovers.

### UmweltCronJobStale

A frequently scheduled job has produced no success for 15 minutes while its schedule is still being driven (a stopped worker raises `UmweltScrapeTargetDown` instead). Typical causes: the job hangs past the 180 s job timeout, or every run raises. Compare `umwelt_cron_job_failures_total` and duration for the job.

### UmweltDbPoolSaturated

More than 90% of the pool plus overflow is checked out for 5 minutes. Look for idle-in-transaction sessions and long statements (Database section); a leaked session or an external call made while holding a transaction is the usual cause. Raising `DB_POOL_SIZE` is a stop-gap and must stay within Postgres `max_connections` (see `docs/deployment.md`).

### UmweltModelBreakerOpen

A model provider rejected calls through its circuit breaker for 2 minutes. Check the provider status and the key/endpoint in Settings; the breaker closes by itself after its open window (30 s default, up to 5 min on a long `Retry-After`) once a call succeeds. Chat and extraction jobs fail fast meanwhile and retry from their durable state.

### UmweltEventLoopLagHigh

The process event loop was blocked for more than 0.5 s: blocking CPU or synchronous I/O in async code (parsing, hashing, large JSON). Correlate with the slow route or job; trace spans (if enabled) show the long segment. Offload to a thread or the heavy-work slot.

### UmweltScrapeTargetDown

Prometheus cannot scrape an API or worker target. API: the token in `metrics_token.local` must equal `METRICS_TOKEN` (401 means mismatch, 404 means no token configured in production). Workers: `METRICS_WORKER_PORT` must be set (the overlay does this) and the container running. A restart resets in-process counters; PromQL `rate()` handles that.

## Approved cross-module read projection

Per AGENTS.md (narrowly scoped read projections): `modules/observability/metrics_sampler.py` reads the ingestion-owned `event_outbox` table with one aggregate query. Owner: `modules/ingestion`. Fields: `count(*)` and `min(created_at)` filtered on `status IN ('pending','queued')`. No payload, workspace or user identity is read, so there is nothing to permission-check or erase beyond the owner's own deletion (erased rows simply leave the aggregate). Read-only; never used for writes. Recorded as approved by this change; replace it with an ingestion `public.py` query if the table is renamed.
