import { apiRequest, csrfHeaders } from '@/core/api';

/** One saved brief revision; `stale` means a cited source has since been removed. */
export type DailyBriefRevision = {
  id: string;
  brief_date: string;
  timezone: string;
  revision: number;
  status: 'current' | 'stale';
  content: string;
  citations: { ref: number; kind: string; id: string; title: string; source_ids: string[] }[];
  model_alias: string;
  generated_at: string;
};

/** Current-record projection of one day widget (never a historical state snapshot). */
export type DailyWidgetData = {
  id: string;
  module: string;
  title_key: string;
  status: 'ok' | 'empty' | 'not_applicable' | 'unavailable';
  items: Record<string, unknown>[];
  updated_at: string | null;
  source_status: string | null;
  history_mode: 'current_records';
};

/** Selected-day context: the saved brief plus widgets built from current records. */
export type DailyContextData = {
  selected_date: string;
  timezone: string;
  relation: 'past' | 'today' | 'future';
  generated_at: string;
  brief: DailyBriefRevision | null;
  brief_revisions: number;
  widgets_updated_at: string;
  history_mode: 'saved_brief_current_records';
  unread_notifications: number;
  widgets: DailyWidgetData[];
};

/** Query keys for selected-day data so day switches never share cache entries. */
export const dailyKeys = {
  context: (date: string, timezone: string) => ['daily-context', date, timezone] as const,
  briefs: (date: string, timezone: string) => ['daily-briefs', date, timezone] as const,
};

/** Loads the saved brief and current-record widgets for one local date. */
export function getDailyContext(date: string, timezone: string, signal?: AbortSignal): Promise<DailyContextData> {
  const query = new URLSearchParams({ date, timezone });
  return apiRequest<DailyContextData>(`/api/v1/context/daily?${query}`, { signal });
}

/** Requests a new brief revision; rejects with ApiError 409 (no inputs) or 503 (model unavailable). */
export function generateBrief(date: string, timezone: string, csrfToken: string): Promise<DailyBriefRevision> {
  return apiRequest<DailyBriefRevision>('/api/v1/briefs/generate', {
    method: 'POST',
    headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
    body: JSON.stringify({ brief_date: date, timezone, force: true }),
  });
}

/** Lists saved brief revisions for a day; for members this is only the briefs shared with them. */
export function listBriefRevisions(date: string, timezone: string, signal?: AbortSignal): Promise<DailyBriefRevision[]> {
  const query = new URLSearchParams({ date, timezone });
  return apiRequest<DailyBriefRevision[]>(`/api/v1/briefs?${query}`, { signal });
}
