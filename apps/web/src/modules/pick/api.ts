import { apiRequest, csrfHeaders } from '@/core/api';

/** Desktop view of a pick session. `prompt` is set exactly once, on the poll that consumes it. */
export type PickPoll = { state: 'waiting' | 'scanned' | 'picked'; prompt: string | null };
/** Created session: id, lifetime and the host LAN IPv4 (null when none was found). */
export type PickCreated = { session_id: string; expires_in: number; lan_ip: string | null };
/** One option offered to the phone; only `index` ever travels back. */
export type PickOption = { index: number; label: string };

/** Asks whether the feature is enabled (404 while PICK_ENABLED is off). */
export function pickEnabled(signal?: AbortSignal): Promise<{ enabled: boolean }> {
  return apiRequest('/api/v1/pick/status', { signal });
}

/** Creates a QR session under CSRF protection. */
export function createPickSession(csrfToken: string): Promise<PickCreated> {
  return apiRequest('/api/v1/pick/sessions', { method: 'POST', headers: csrfHeaders(csrfToken) });
}

/** Polls a session; the server deletes it once a picked prompt has been returned. */
export function pollPickSession(id: string, signal?: AbortSignal): Promise<PickPoll> {
  return apiRequest(`/api/v1/pick/sessions/${encodeURIComponent(id)}`, { signal });
}

/** Phone: marks the session scanned and returns the three offered options. */
export function scanPickSession(id: string): Promise<{ options: PickOption[] }> {
  return apiRequest(`/api/v1/pick/phone/${encodeURIComponent(id)}`, { method: 'PUT' });
}

/** Phone: submits the chosen option index (never text). */
export function submitPick(id: string, index: number): Promise<void> {
  return apiRequest(`/api/v1/pick/phone/${encodeURIComponent(id)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index }),
  });
}
