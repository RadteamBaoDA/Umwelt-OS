// Run-level setup shared by every entry script.
import http from 'k6/http';
import { API_URL, numEnv } from './config.js';
import { ensureOwner, login, installSession, writeHeaders } from './auth.js';
import { configureFakeModel } from './fake_model.js';

// Only 2xx/3xx count as successes for http_req_failed. 429 from a documented rate budget is a failure on purpose:
// the scenarios are sized to stay inside those budgets, so a 429 means the load model (or the budget) is wrong.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 399 }));

/**
 * Creates the owner (when missing), logs in once and optionally configures the fake chat model.
 * Intended to be returned from the script's `setup()`; the result is passed to every VU.
 * @param {{fakeModel?: boolean, seed?: boolean}} [opts] `fakeModel` for chat scenarios; `seed` uploads SEED_DOCS (default 8, clamped to 10 to stay inside the 20/min upload budget shared with the ingest scenario) documents.
 * @returns {{session: string, csrfCookie: string, csrfToken: string}}
 */
export function setupRun(opts = {}) {
  ensureOwner();
  const auth = login();
  if (opts.fakeModel) configureFakeModel(auth);
  if (opts.seed) seedDocuments(auth, Math.min(10, Math.floor(numEnv('SEED_DOCS', 8))));
  return auth;
}

/**
 * Uploads a few fictional Markdown documents so dashboard/documents/search have data to read. Does not wait for
 * indexing (the read mix tolerates a partly indexed corpus). Stays far below the 20/min upload budget.
 * @param {{csrfToken: string}} auth Session material from login().
 * @param {number} count Number of documents.
 */
function seedDocuments(auth, count) {
  for (let i = 0; i < count; i += 1) {
    const body = `# Seed note ${i}\n\nFictional load-test seed document ${i} about umwelt dashboards, chat, ingestion and finance.\n`;
    http.post(`${API_URL}/api/v1/documents/upload`, { file: http.file(body, `seed-${i}.md`, 'text/markdown') }, {
      headers: writeHeaders(auth), tags: { endpoint: 'seed_upload' },
    });
  }
}

let installed = false;

/**
 * Installs the shared session into this VU's jar the first time it runs. Only valid because every entry script sets
 * `noCookiesReset: true`; without it k6 replaces the jar each iteration and later iterations would be unauthenticated
 * (scripts/check-k6-scripts.mjs enforces the option).
 * @param {{session: string, csrfCookie: string}} auth Data returned by setupRun().
 */
export function ensureVuSession(auth) {
  if (installed) return;
  installSession(auth);
  installed = true;
}

/**
 * Issues an authenticated GET tagged for the per-endpoint SLO thresholds.
 * @param {string} path API path beginning with /api.
 * @param {string} endpoint Value of the `endpoint` tag (dashboard, search, documents, ...).
 * @returns {import('k6/http').RefinedResponse<'text'>}
 */
export function apiGet(path, endpoint) {
  return http.get(`${API_URL}${path}`, { tags: { endpoint } });
}

export { writeHeaders };
