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

Langfuse export is off by default and is not part of the base Compose stack. The base `api` and `worker` services explicitly set `LANGFUSE_ENABLED=false`, which overrides any opt-in value retained in `.env`; adding the optional overlay restores the owner-selected value. It sends only OpenTelemetry generation spans for model calls: fixed service/capability names, model identity when safe, outcome, nullable input/output token counts, and UUID request/run correlation IDs. Prompt, message, document, input, output, completion, source, user, and exception content are not exported. There is no content-capture option. The API and worker use a bounded process-local queue; when it is full, the total send-and-cleanup deadline expires, or Langfuse rejects a request, that telemetry is dropped and the knowledge write or agent result continues. The sender has a cooperative two-second overall deadline plus HTTPX per-I/O timeouts and checks only the streamed response status without buffering its body. Export health is not included in application readiness; verify receipt in Langfuse itself.

To opt in, place the endpoint and Langfuse project keys in the protected server `.env`, then explicitly enable both flags:

```dotenv
LANGFUSE_ENABLED=true
TELEMETRY_EGRESS_APPROVED=true
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Use the correct regional Langfuse host or the HTTPS origin of the owner-selected self-hosted service. Credentials are passed only to API and worker containers and are never sent to the browser. The exporter requires an HTTPS origin without userinfo, path, query, or fragment, does not follow redirects, and does not use environment proxy variables. The owner must explicitly approve the destination and protect `.env`; do not put keys in Compose files, command arguments, screenshots, or logs.

Start the optional profile by including the overlay on the normal Compose command, for example:

```powershell
docker compose -f docker-compose.yml -f infrastructure/observability/compose.yml up -d
```

To disable export and return to the base deployment, recreate the services without the overlay:

```powershell
docker compose down
docker compose -f docker-compose.yml up -d
```

The base `api` and `worker` then force the exporter off even if `.env` still contains enabled flags and keys. No Langfuse containers, local database, ClickHouse, or object store are added by this profile. Incomplete settings leave export disabled without affecting startup. The application does not probe the remote endpoint at startup, and an enabled flag does not prove that spans were received.

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
SELECT 'p12_backup_activity' AS t, count(*) FROM p12_backup_activity
UNION ALL SELECT 'event_outbox', count(*) FROM event_outbox
UNION ALL SELECT 'chat_stream_events', count(*) FROM chat_stream_events;
```

Model-gateway breaker state is Redis-only and not exported as a metric: an open breaker shows as `ModelGatewayUnavailable` in logs; see [operations.md](operations.md) for reset and the retention job inventory. Slow statements and lock waits are in the Postgres container log (`docker compose logs postgres`).
