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
