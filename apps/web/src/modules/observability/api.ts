import { apiRequest, csrfHeaders } from '@/core/api';
import type { ApiBody, ApiResponse } from '@/core/api-contract';

export type ObservedRun = {
  kind: 'ingestion' | 'agent' | 'automation' | 'chat';
  id: string;
  status: string;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  usage: { model_identity: string | null; tokens_in: number | null; tokens_out: number | null; estimated_cost: number | null } | null;
  trace: { request_id: string | null; ingestion_run_id: string | null; agent_run_id: string | null; tool_call_id: string | null };
};

/** Process-local counters and histograms; derived from the backend OpenAPI schema (reference typed contract). */
export type Metrics = ApiResponse<'/api/v1/system/metrics'>;

export type Quality = {
  document_count: number;
  duplicate_rate: number;
  unresolved_entities: number;
  failed_ingestion: number;
  failed_extraction: number;
  stale_sources: number;
  orphan_chunks: number;
  graph_sync_lag_seconds: number | null;
  generated_at: string;
};

export type QueueSummary = {
  ingestion_stages: Record<string, number>;
  retryable_ingestion_stages: number;
  event_delivery: Record<string, number>;
  connector_provisioning: Record<string, number>;
  payloads_included: false;
};

export type SystemHealth = {
  overall: string;
  components: Record<string, { status: string; connectivity?: string; latency_ms?: number | null }>;
};

export type HistoryRange = '1h' | '24h' | '7d' | '30d' | '90d';

/** Per-component sample tallies for one history bucket; `total` 0 means the component did not apply. */
export type HistoryComponent = { up: number; total: number; latency_ms_avg: number | null; latency_ms_max: number | null };

export type HistoryBucket = {
  start: string;
  samples: number;
  overall_healthy: number;
  queue_backlog_max: number;
  /** Database and Redis aggregates; absent on samples taken before database stats existed. */
  db?: HistoryDb | null;
  components: Record<string, HistoryComponent>;
};

export type HistoryDb = {
  pg_size_bytes_max: number | null; pg_connections_avg: number | null; pg_connections_max: number | null; pg_cache_hit_ratio_avg: number | null;
  pg_tps_avg: number | null; redis_memory_bytes_max: number | null; redis_ops_per_sec_avg: number | null; redis_hit_ratio_avg: number | null;
};

export type SystemHistory = {
  range: HistoryRange;
  bucket_seconds: number;
  retention_days: number;
  generated_at: string;
  oldest_sample_at: string | null;
  buckets: HistoryBucket[];
};

/** History retention/collection settings plus the revision fencing the next write; derived from the OpenAPI schema. */
export type HistorySettings = ApiResponse<'/api/v1/system/history/settings'>;
/** Fields a settings PATCH may change; only the supplied ones are sent. */
export type HistorySettingsPatch = Omit<ApiBody<'/api/v1/system/history/settings', 'patch'>, 'expected_revision'>;

/**
 * Fetch the owner-protected newest-run window, capped at 100 metadata-only summaries.
 * An omitted or `all` kind leaves the request unfiltered; the server owns its bounded
 * newest-window semantics and response payload omission.
 */
export function listRuns(kind?: string) {
  const query = new URLSearchParams({ limit: '100' });
  if (kind && kind !== 'all') query.set('kind', kind);
  return apiRequest<{ items: ObservedRun[]; limit: number }>(`/api/v1/system/runs?${query}`);
}

/** Fetch one owner-protected metadata-only detail projection using the run's kind and ID. */
export function getRunDetail(run: ObservedRun) {
  return apiRequest<ObservedRun>(`/api/v1/system/runs/${run.kind}/${encodeURIComponent(run.id)}`);
}
/** Fetch process-local counters and histograms; absent processes or measurements remain unknown. */
export function getMetrics() { return apiRequest<Metrics>('/api/v1/system/metrics'); }
/** Fetch bounded quality counts, preserving unavailable graph lag as `null` rather than zero. */
export function getQuality() { return apiRequest<Quality>('/api/v1/system/quality'); }
/** Fetch queue counts only; the owner API explicitly excludes queued payload contents. */
export function getQueueSummary() { return apiRequest<QueueSummary>('/api/v1/system/queue'); }
/** Fetch the owner system-health projection; component states are reported independently. */
export function getSystemHealth() { return apiRequest<SystemHealth>('/api/v1/system/health'); }

/** Fetch bucketed health history for the range; only buckets that hold samples are returned, ascending. */
export function getSystemHistory(range: HistoryRange) { return apiRequest<SystemHistory>(`/api/v1/system/history?range=${range}`); }
/** Fetch the history retention setting with the revision that fences the next write. */
export function getHistorySettings() { return apiRequest<HistorySettings>('/api/v1/system/history/settings'); }
/** Save history retention (1..90 days) and/or collection interval (0 = off); 409 when `expectedRevision` is stale. */
export function saveHistorySettings(csrfToken: string, expectedRevision: number, patch: HistorySettingsPatch) {
  return apiRequest<HistorySettings>('/api/v1/system/history/settings', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ expected_revision: expectedRevision, ...patch }),
  });
}

export type PgTable = {
  name: string; total_bytes: number | null; live_rows: number | null; dead_rows: number | null; dead_ratio: number | null;
  seq_scans: number | null; index_scans: number | null; last_autovacuum: string | null; last_autoanalyze: string | null;
};

export type PgStats = {
  status: string; version: string | null; uptime_seconds: number | null; database_size_bytes: number | null;
  connections: { active: number | null; idle: number | null; idle_in_transaction: number | null; waiting: number | null; total: number | null; max: number | null } | null;
  cache_hit_ratio: number | null; index_hit_ratio: number | null; transactions: { commit: number | null; rollback: number | null } | null;
  deadlocks: number | null; temp_bytes: number | null; locks_waiting: number | null; longest_transaction_seconds: number | null; tables: PgTable[] | null;
};

export type RedisStats = {
  status: string; version: string | null; uptime_seconds: number | null; used_memory_bytes: number | null; max_memory_bytes: number | null;
  fragmentation_ratio: number | null; connected_clients: number | null; ops_per_sec: number | null; keyspace_hit_ratio: number | null;
  evicted_keys: number | null; expired_keys: number | null; keys_total: number | null; last_save_status: string | null;
};

export type DatabaseStats = { generated_at: string; postgres: PgStats | null; redis: RedisStats | null };

/** Fetch live Postgres and Redis statistics; every figure may be null when the server could not read it. */
export function getDatabaseStats() { return apiRequest<DatabaseStats>('/api/v1/system/database'); }
