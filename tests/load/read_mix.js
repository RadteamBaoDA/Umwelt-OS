// Read-mix scenario: dashboard list + documents list with ramping VUs, plus a rate-limited lexical search stream.
// Run alone: k6 run -e API_URL=... -e PUBLIC_ORIGIN=... tests/load/read_mix.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { API_URL, dur } from './lib/config.js';
import { sloThresholds } from './lib/thresholds.js';
import { setupRun, ensureVuSession, apiGet, writeHeaders } from './lib/run.js';
import './lib/metrics.js';

const QUERIES = ['umwelt', 'load test document', 'dashboard', 'chat', 'ingestion', 'finance'];

/** Scenario definitions, exported so all.js can compose them. */
export const readMixScenarios = {
  // Ramps a single owner's browser-like traffic; think time keeps it realistic for one person on a 2 vCPU host.
  read_mix: {
    executor: 'ramping-vus',
    exec: 'readMixExec',
    startVUs: 1,
    stages: [
      { duration: dur(30), target: 5 },
      { duration: dur(120), target: 10 },
      { duration: dur(30), target: 0 },
    ],
    gracefulRampDown: '10s',
  },
  // Search is limited to 60 requests/minute by the API budget (require_budget("search", 60, 60)); stay at half of it.
  search: {
    executor: 'constant-arrival-rate',
    exec: 'searchExec',
    rate: 30,
    timeUnit: '1m',
    duration: dur(180),
    preAllocatedVUs: 3,
    maxVUs: 6,
  },
};

// noCookiesReset: k6 otherwise swaps in an empty jar every iteration, dropping the session installed once per VU.
export const options = { scenarios: readMixScenarios, thresholds: sloThresholds, noCookiesReset: true };

/** k6 setup: logs in once; the result is handed to every VU. */
export function setup() {
  return setupRun({ seed: true });
}

/**
 * One browsing iteration: dashboards then documents, with think time.
 * @param {object} auth Session material from setup().
 */
export function readMixExec(auth) {
  ensureVuSession(auth);
  const dashboards = apiGet('/api/v1/dashboards', 'dashboard');
  check(dashboards, { 'dashboards 200': (r) => r.status === 200 });
  sleep(0.5 + Math.random());
  const documents = apiGet('/api/v1/documents?limit=50', 'documents');
  check(documents, { 'documents 200': (r) => r.status === 200 });
  sleep(1 + Math.random() * 2);
}

/**
 * One lexical search (hybrid would call the embedding provider, which is out of scope for the fake-model stack).
 * @param {object} auth Session material from setup().
 */
export function searchExec(auth) {
  ensureVuSession(auth);
  const query = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  const res = http.post(`${API_URL}/api/v1/search`, JSON.stringify({ query, mode: 'lexical', limit: 20 }), {
    headers: writeHeaders(auth, { 'Content-Type': 'application/json' }),
    tags: { endpoint: 'search' },
  });
  check(res, { 'search 200': (r) => r.status === 200 });
}

export default readMixExec;
