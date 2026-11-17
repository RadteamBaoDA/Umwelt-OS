// Thresholds that encode docs/slo.md. Keep the two in sync: every entry names the SLO it enforces.
import { LATENCY_SCALE } from './config.js';

/**
 * Scales a latency bound by SLO_LATENCY_SCALE.
 * @param {number} ms Documented bound in milliseconds.
 * @returns {number}
 */
function scaled(ms) {
  return Math.round(ms * LATENCY_SCALE);
}

/** k6 thresholds for the documented SLOs (target host: 2 vCPU / 8 GiB). */
export const sloThresholds = {
  // SLO-1 availability: fewer than 0.5 % failed (non-2xx/3xx) requests across read and write scenarios.
  http_req_failed: ['rate<0.005'],
  // SLO-2 read latency.
  'http_req_duration{endpoint:dashboard}': [`p(95)<${scaled(400)}`, `p(99)<${scaled(800)}`],
  'http_req_duration{endpoint:search}': [`p(95)<${scaled(800)}`, `p(99)<${scaled(1500)}`],
  'http_req_duration{endpoint:documents}': [`p(95)<${scaled(300)}`, `p(99)<${scaled(600)}`],
  // SLO-3 chat time to first token (fake model, 1-token stream) and SLO-5 stream open latency.
  chat_ttft_ms: [`p(95)<${scaled(2000)}`, `p(99)<${scaled(4000)}`],
  chat_sse_open_ms: [`p(95)<${scaled(500)}`],
  chat_send_ms: [`p(95)<${scaled(500)}`],
  // SLO-4 ingest lag (upload accepted -> searchable) and SLO-6 worker queue age (queued -> running).
  ingest_lag_ms: [`p(95)<${scaled(30000)}`, `p(99)<${scaled(60000)}`],
  ingest_queue_age_ms: [`p(95)<${scaled(5000)}`],
  // Scenario correctness: every business check must pass at least 99 %.
  checks: ['rate>0.99'],
};
