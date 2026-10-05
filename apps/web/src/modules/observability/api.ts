import { apiRequest } from '@/core/api';

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

export type Metrics = {
  generated_at: string;
  processes: string[];
  counters: { name: string; labels: Record<string, string>; value: number }[];
  histograms: { name: string; labels: Record<string, string>; count: number; sum_ms: number; p95_ms: number | null }[];
};

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
  components: Record<string, { status: string }>;
};

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
