// Custom k6 metrics. They are declared in one module so every entry script (single scenario or all.js) defines the
// complete set that lib/thresholds.js refers to; k6 rejects a threshold on a metric that does not exist.
import { Trend, Counter } from 'k6/metrics';

/** POST /conversations/{id}/messages round trip (accepted -> 202). */
export const chatSendMs = new Trend('chat_send_ms', true);
/** Time until the SSE response headers arrive (stream open), ms. */
export const chatSseOpenMs = new Trend('chat_sse_open_ms', true);
/**
 * Chat time to first token, ms: send start -> end of a 1-token fake-model stream. With one token the first token IS the
 * whole stream, so this is an upper bound on TTFT that includes the worker's stream flush interval (docs/slo.md).
 */
export const chatTtftMs = new Trend('chat_ttft_ms', true);
/** Send start -> `message.done` for the multi-token variant, ms. */
export const chatCompletionMs = new Trend('chat_completion_ms', true);
/** Upload start -> the unique document token is returned by search, ms. */
export const ingestLagMs = new Trend('ingest_lag_ms', true);
/** Upload accepted -> ingestion run observed out of the `queued` state, ms (worker queue age, 1 s polling resolution). */
export const ingestQueueAgeMs = new Trend('ingest_queue_age_ms', true);
/** Ingest runs that did not become searchable before the scenario timeout. */
export const ingestTimeouts = new Counter('ingest_timeouts');
