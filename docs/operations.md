# Operations

## Retention and module lifecycle

Agent trace retention defaults to 90 days and can be changed in Settings → advanced operations. The hourly worker redacts only terminal runs past the cutoff. It preserves run IDs, status/outcome metadata, effect and approval identifiers, idempotency records, source records, document revisions, and Chat messages. Runs with unresolved approvals or reserved, in-flight, or review-required effects are skipped. Checkpoints and run trace payloads are cleared as one bounded batch.

Expired browser page evidence is removed in bounded batches after its job expiry. Browser job rows remain as idempotency and outcome tombstones. Maintenance reports the last counts and next eligible time in Settings.

Module disables are stored as explicit owner choices; dependencies determine effective availability transitively. Owner request gates verify owner and CSRF authorization before returning module state. Collector bearer routes, OAuth callbacks, GitHub webhooks, and inbound MCP authentication retain their existing credential checks. Source/document/chat/memory deletion, agent cancellation, approval decisions, memory privacy purge, and connector security revocation paths remain available during a module disable. Worker dispatch and native tool calls read current persisted availability before acting; recoveries, expiry cleanup, and privacy maintenance continue to run.

## Observability and troubleshooting

Use the request ID from an API error response or its `X-Request-ID` header to find the matching API log. For accepted asynchronous work, keep the durable `run_id` and inspect it in Settings → advanced operations; request IDs and run IDs are different identifiers, and unavailable correlations remain unknown. No-change polls are intentionally silent; cleanup activity is debug logged and actionable failures are warned. See [observability and Langfuse](observability.md) for health boundaries, privacy, and the optional remote profile.
