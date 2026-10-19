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
| `event_outbox` | delivered `ingestion.stage.requested`/`ingestion.normalize.requested` older than 7 days; other event types kept | same job | 1000 per call |
| `automation_runs` | terminal runs finished more than 30 days ago, newest hour kept for rate caps | `run_retention_maintenance`, hourly | 1000 per call |
| `search_index_items` of superseded document versions | removed when the version is superseded (hook registered by search) | event-driven | per document |
| Agent traces, browser evidence | see Retention and module lifecycle above | hourly | as above |

Schedules and bounds are read from `apps/worker/main.py` and the cited modules; none has been observed running against a live database.

## Reconciler cadence

The 5-second reconcilers (outbox dispatch, connectors, document cleanup stages, graph recovery, agent dispatch, automation runs) are separate cron jobs on purpose. Do not merge them into one sequential job: a tick cannot overlap itself, so one slow reconciler would delay all the others (measured regression, see [ARCHITECTURE_DECISIONS](ARCHITECTURE_DECISIONS.md)).

