// Ingest scenario: upload a small Markdown document, poll the ingestion run, then poll lexical search until the
// document's unique token is found. Measures ingest lag (upload -> searchable) and worker queue age (queued -> running).
// Run alone: k6 run -e API_URL=... -e PUBLIC_ORIGIN=... tests/load/ingest.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { API_URL, dur } from './lib/config.js';
import { sloThresholds } from './lib/thresholds.js';
import { setupRun, ensureVuSession, writeHeaders } from './lib/run.js';
import { ingestLagMs, ingestQueueAgeMs, ingestTimeouts } from './lib/metrics.js';

const POLL_SECONDS = 1;
const TIMEOUT_MS = 120000;

/** Scenario definitions, exported so all.js can compose them. */
export const ingestScenarios = {
  // The upload budget is 20 per minute (require_budget("ingest_upload", 20, 60)); six per minute is a heavy single-user load.
  ingest: {
    executor: 'constant-arrival-rate',
    exec: 'ingestExec',
    startTime: dur(30), // scaled with the other scenarios; after the seed uploads, so both stay inside the 20/min upload budget
    rate: 6,
    timeUnit: '1m',
    duration: dur(180),
    preAllocatedVUs: 4,
    maxVUs: 12,
  },
};

// noCookiesReset keeps the session cookies installed once per VU across iterations.
export const options = { scenarios: ingestScenarios, thresholds: sloThresholds, noCookiesReset: true };

/** k6 setup: logs in once. */
export function setup() {
  return setupRun();
}

/**
 * Builds a small Markdown document containing one unique, search-safe token.
 * @param {string} token Unique alphanumeric token.
 * @returns {string}
 */
function documentBody(token) {
  return `# Load test note ${token}\n\nThis fictional note exists only to measure ingestion lag. Marker: ${token}.\n\n`
    + 'Umwelt-OS stores documents, chunks them, and indexes them for lexical and semantic search.\n';
}

/**
 * Polls the ingestion run until it succeeds, recording the first time it is seen out of `queued`.
 * @param {string} runId Run id from the upload receipt.
 * @param {number} started Date.now() at upload start.
 * @returns {boolean} true when the run succeeded before the timeout.
 */
function waitForRun(runId, started) {
  let queueRecorded = false;
  while (Date.now() - started < TIMEOUT_MS) {
    const res = http.get(`${API_URL}/api/v1/ingestion/runs/${runId}`, { tags: { endpoint: 'ingest_run_poll' } });
    const status = res.status === 200 ? res.json('status') : 'error';
    if (!queueRecorded && status !== 'queued' && status !== 'error') {
      ingestQueueAgeMs.add(Date.now() - started);
      queueRecorded = true;
    }
    if (status === 'succeeded') return true;
    if (status === 'failed' || status === 'needs_ocr') return false;
    sleep(POLL_SECONDS);
  }
  return false;
}

/**
 * Polls lexical search until the token is returned.
 * @param {object} auth Session material.
 * @param {string} token Unique token.
 * @param {number} started Date.now() at upload start.
 * @returns {boolean} true when found before the timeout.
 */
function waitForSearch(auth, token, started) {
  const headers = writeHeaders(auth, { 'Content-Type': 'application/json' });
  while (Date.now() - started < TIMEOUT_MS) {
    const res = http.post(`${API_URL}/api/v1/search`, JSON.stringify({ query: token, mode: 'lexical', limit: 5 }), {
      headers, tags: { endpoint: 'search_poll' },
    });
    if (res.status === 200 && (res.json('items') || []).length > 0) return true;
    sleep(POLL_SECONDS * 2); // search is budgeted at 60/min; poll gently
  }
  return false;
}

/**
 * One ingest iteration.
 * @param {object} auth Session material from setup().
 */
export function ingestExec(auth) {
  ensureVuSession(auth);
  const token = `zqload${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const started = Date.now();
  const upload = http.post(
    `${API_URL}/api/v1/documents/upload`,
    { file: http.file(documentBody(token), `${token}.md`, 'text/markdown') },
    { headers: writeHeaders(auth), tags: { endpoint: 'ingest_upload' } },
  );
  if (!check(upload, { 'upload 202': (r) => r.status === 202 })) return;
  const succeeded = waitForRun(upload.json('run_id'), started);
  const found = succeeded && waitForSearch(auth, token, started);
  if (check(null, { 'document became searchable': () => found })) ingestLagMs.add(Date.now() - started);
  else ingestTimeouts.add(1);
}

export default ingestExec;
