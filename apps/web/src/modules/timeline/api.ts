import { apiRequest } from '@/core/api';

/** Carries the occurrence, observation, validity, and evidence fields owned by the timeline API. */
export type TimelineEvent = {
  id: string;
  source_id: string | null;
  type: string;
  subtype: string | null;
  title: string;
  summary: string | null;
  importance_score: number | null;
  confidence: number | null;
  metadata: Record<string, unknown>;
  origin: 'manual' | 'derived';
  date_precision: 'timed' | 'date' | 'unknown';
  started_at: string | null;
  ended_at: string | null;
  occurred_date: string | null;
  end_date: string | null;
  occurrence_timezone: string | null;
  observed_at: string;
  valid_from: string | null;
  valid_to: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
  participants: { entity_id: string; role: string; metadata: Record<string, unknown> }[];
  evidence: {
    source_id: string;
    document_id: string;
    document_version_id: string;
    version_number: number;
    chunk_id: string;
    observed_at: string;
    title: string;
    canonical_url: string | null;
    metadata_is_version_snapshot: boolean;
    excerpt: string;
  }[];
};

export type TimelineQuery = {
  date_from: string;
  date_to: string;
  timezone: string;
  source_id: string;
  entity_id: string;
  precision: 'all' | 'timed' | 'date' | 'unknown';
  type?: string;
};

export type TimelinePageResult = {
  items: TimelineEvent[];
  next_cursor: string | null;
  partition_order: string[];
};

/**
 * Fetches one bounded page with normalized filters; the API cursor is bound to this query.
 * @throws {RangeError} When the type filter exceeds the API's 64-character bound.
 */
export function listTimeline(query: TimelineQuery, cursor?: string) {
  const params = new URLSearchParams({ limit: '50', timezone: query.timezone, precision: query.precision });
  if (query.date_from && query.date_to && query.precision !== 'unknown') {
    params.set('date_from', query.date_from);
    params.set('date_to', query.date_to);
  }
  if (query.source_id) params.set('source_id', query.source_id);
  if (query.entity_id) params.set('entity_id', query.entity_id);
  const type = query.type?.trim() ?? '';
  if ([...type].length > 64) throw new RangeError('Timeline type filter exceeds 64 characters');
  if (type) params.set('type', type);
  if (cursor) params.set('cursor', cursor);
  return apiRequest<TimelinePageResult>(`/api/v1/timeline?${params}`);
}
