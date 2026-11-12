import { BROWSER_SOURCE_BY_ID, type LiveFeedScope } from './sources';
import type { LiveEvent } from '../live-map-data';

/** Hard response size cap; a larger body is aborted rather than buffered. */
export const MAX_BODY_BYTES = 5 * 1024 * 1024;
/** Per-request timeout. */
export const FETCH_TIMEOUT_MS = 15_000;
const MAX_BACKOFF_MS = 3_600_000;

/** Messages the page sends to the worker. */
export type WorkerInbound =
  | { type: 'config'; providers: string[]; scope: LiveFeedScope; workspaceId: string }
  | { type: 'visibility'; visible: boolean }
  | { type: 'refresh' };

/** Manual refresh skips a provider fetched this recently, so repeated clicks cannot exhaust anonymous rate limits. */
export const REFRESH_MIN_GAP_MS = 30_000;

/**
 * Providers a manual refresh should fetch now: not already in flight and not fetched within `REFRESH_MIN_GAP_MS`.
 * @param lastRun Epoch ms each provider last started a fetch.
 */
export function refreshable(
  providers: string[],
  inFlight: ReadonlySet<string>,
  lastRun: ReadonlyMap<string, number>,
  now: number,
): string[] {
  return providers.filter((id) => !inFlight.has(id) && now - (lastRun.get(id) ?? 0) >= REFRESH_MIN_GAP_MS);
}

/** Messages the worker posts back; `changed` means new rows were written to the store. */
export type WorkerOutbound = { type: 'status'; provider: string; ok: boolean; error?: string; fetchedAt: string } | { type: 'changed' };

/**
 * Delay before the next fetch of a source: the normal interval, doubled per consecutive failure up to one hour.
 * Applies to every failure kind including HTTP 429, so a rate-limited provider is never hammered.
 */
export function nextDelayMs(intervalMs: number, failures: number): number {
  return Math.min(intervalMs * 2 ** failures, MAX_BACKOFF_MS);
}

/** Read a response body as text, aborting once it exceeds `max` bytes. */
async function readCapped(response: Response, max: number): Promise<string> {
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > max) throw new Error('too_large');
  const reader = response.body?.getReader();
  if (!reader) return response.text();
  const decoder = new TextDecoder();
  let received = 0;
  let text = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > max) {
      void reader.cancel();
      throw new Error('too_large');
    }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}

/**
 * Fetch and parse one browser source. Requests are anonymous (no cookies, no referrer) and time-limited.
 * @param fetchFn Injected for tests; defaults to the global `fetch`.
 * @returns Parsed events.
 * @throws Error whose message is a short code: `scope`, `http_<status>`, `too_large`, `parse` or the network error name.
 */
export async function fetchSource(
  provider: string,
  scope: LiveFeedScope,
  now: number,
  fetchFn: typeof fetch = fetch,
): Promise<LiveEvent[]> {
  const source = BROWSER_SOURCE_BY_ID[provider];
  const url = source?.url(scope, now);
  if (!source || !url) throw new Error('scope');
  let response: Response;
  try {
    response = await fetchFn(url, { credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  } catch (error) {
    throw new Error(error instanceof Error && error.name === 'TimeoutError' ? 'timeout' : 'network');
  }
  if (!response.ok) throw new Error(`http_${response.status}`);
  const body = await readCapped(response, MAX_BODY_BYTES);
  try {
    return source.parse(JSON.parse(body));
  } catch {
    throw new Error('parse');
  }
}
