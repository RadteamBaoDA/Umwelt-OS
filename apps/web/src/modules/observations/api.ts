import { apiRequest } from '@/core/api';

/** Owner-readable derived measurement with its source timezone and immutable evidence identity. */
export type WorldObservation = {
  id: string;
  source_id: string;
  provider: string;
  external_id: string;
  revision: number;
  metric: string;
  symbol: string | null;
  region: string | null;
  latitude: number | null;
  longitude: number | null;
  observed_at: string;
  published_at: string | null;
  collected_at: string;
  /** FX reference day (YYYY-MM-DD) or null. */
  reference_date?: string | null;
  /** Annual macro period (e.g. 2024) or null. */
  period?: string | null;
  value: number | null;
  unit: string;
  currency: string | null;
  timezone: string | null;
  quality: 'provider_reported' | 'forecast' | 'missing';
  missing_reason: string | null;
  provider_delay_seconds: number | null;
  document_id: string;
  document_version_id: string;
  document_version_number: number | null;
};

/** One bounded observation page and its continuation cursor. */
export type WorldObservationPage = { items: WorldObservation[]; next_cursor: string | null; truncated: boolean };

/** One page of current provider-coordinate points with unsupported source coverage. */
export type GeospatialObservationPage = WorldObservationPage & { omitted_source_count: number };

/** Fetch a source-authorized observation page; no provider request or credential is made in the browser. */
export function listWorldObservations(
  filters: { sourceIds: string[]; metrics?: string[]; symbols?: string[]; regions?: string[]; from: Date; to: Date; limit?: number; cursor?: string },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ from_at: filters.from.toISOString(), to_at: filters.to.toISOString(), limit: String(filters.limit ?? 100) });
  for (const sourceId of filters.sourceIds) query.append('source_ids', sourceId);
  for (const metric of filters.metrics ?? []) query.append('metrics', metric);
  for (const symbol of filters.symbols ?? []) query.append('symbols', symbol);
  for (const region of filters.regions ?? []) query.append('regions', region);
  if (filters.cursor) query.set('cursor', filters.cursor);
  return apiRequest<WorldObservationPage>(`/api/v1/observations?${query.toString()}`, { signal });
}

/** Fetch one bounded owner-public point page; source/provider/evidence fences stay server-owned. */
export function listGeospatialObservations(
  filters: { sourceIds: string[]; regions?: string[]; from: Date; to: Date; limit?: number; cursor?: string },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ from_at: filters.from.toISOString(), to_at: filters.to.toISOString(), limit: String(filters.limit ?? 100) });
  for (const sourceId of filters.sourceIds) query.append('source_ids', sourceId);
  for (const region of filters.regions ?? []) query.append('regions', region);
  if (filters.cursor) query.set('cursor', filters.cursor);
  return apiRequest<GeospatialObservationPage>(`/api/v1/observations/geospatial?${query.toString()}`, { signal });
}

/** Fetch at most five owner-authorized pages and report either server or client truncation. */
export async function listWorldObservationWindow(
  filters: Omit<Parameters<typeof listWorldObservations>[0], 'cursor' | 'limit'>,
  signal?: AbortSignal,
): Promise<{ items: WorldObservation[]; truncated: boolean }> {
  const items: WorldObservation[] = [];
  let cursor: string | undefined;
  for (let pageNumber = 0; pageNumber < 5; pageNumber += 1) {
    const page = await listWorldObservations({ ...filters, limit: 100, cursor }, signal);
    items.push(...page.items);
    if (!page.next_cursor) return { items, truncated: page.truncated };
    cursor = page.next_cursor;
  }
  return { items, truncated: true };
}
