import { apiRequest } from '@/core/api';

/** Evidence coverage for one declared R15 correlation domain. */
export type CorrelationCoverage = {
  signal_count: number;
  available: boolean;
  truncated: boolean;
  omitted_source_count: number;
};

/** One UTC hour of deterministic co-occurrence in a recorded region. */
export type CorrelationBucket = {
  region: string;
  window_start: string;
  window_end: string;
  signal_count: number;
  domain_counts: Record<string, number>;
  domains_present: string[];
  signal_ids: string[];
  event_ids: string[];
  observation_ids: string[];
  document_ids: string[];
  document_version_ids: string[];
  event_evidence_ids: string[];
  omitted_document_ids: number;
  omitted_document_version_ids: number;
  omitted_event_evidence_ids: number;
};

/** Explicitly bounded temporal co-occurrence and source coverage DTO. */
export type CorrelationResult = {
  method_version: 'co_occurrence_v1';
  from_at: string;
  to_at: string;
  regions: string[];
  groups: CorrelationBucket[];
  coverage: Record<'military' | 'economic' | 'disaster' | 'escalation', CorrelationCoverage>;
  included_domains: string[];
  missing_domains: string[];
  uncertainty_reasons: string[];
  interpretation: 'temporal_co_occurrence_only';
};

/** CII v8 projection that preserves the owner-requested countries and never substitutes a score. */
export type CiiAvailability = {
  method_version: 'v8';
  requested_countries: string[];
  score: null;
  band: null;
  movement_24h: null;
  as_of: null;
  availability: 'method_data_license_unverified';
  reason: string;
};

/** Request one bounded correlation projection over the saved region, source, and date scope. */
export function getCorrelations(
  filters: { sourceIds: string[]; regions: string[]; from: Date; to: Date },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ from_at: filters.from.toISOString(), to_at: filters.to.toISOString() });
  for (const sourceId of filters.sourceIds) query.append('source_ids', sourceId);
  for (const region of filters.regions) query.append('regions', region);
  return apiRequest<CorrelationResult>(`/api/v1/intelligence/correlations?${query.toString()}`, { signal });
}

/** Read the consumed unavailable CII v8 method state for the exact requested country selectors. */
export function getCiiAvailability(countries: string[], signal?: AbortSignal) {
  const query = new URLSearchParams();
  for (const country of countries) query.append('countries', country);
  return apiRequest<CiiAvailability>(`/api/v1/intelligence/cii?${query.toString()}`, { signal });
}
