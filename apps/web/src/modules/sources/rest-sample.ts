import { apiRequest } from '@/core/api';
import type { DraftValidationRequest } from './api';

/** Response of the REST sample preview endpoint. */
export type RestSample = { status_code: number; truncated: boolean; sample: unknown };

/**
 * Fetches one bounded sample of a draft REST connector's endpoint (same request body as validate-draft).
 * Side effects: a server-side outbound GET; nothing is persisted. Errors surface as ApiError whose message is the server code.
 */
export function restSample(id: string, settings: DraftValidationRequest, signal?: AbortSignal) {
  return apiRequest<RestSample>(`/api/v1/connectors/${id}/rest-sample`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings), signal });
}
