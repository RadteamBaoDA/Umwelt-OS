// Shared configuration for the Umwelt-OS k6 load scenarios (k6 >= 0.50, plain ES modules).
// Everything is read from environment variables (`k6 run -e NAME=value`) so scripts/load-test.sh can point
// the same scenarios at the disposable Compose stack.

/** Base URL of the API service on the Compose network (no trailing slash). */
export const API_URL = (__ENV.API_URL || 'http://localhost:8000').replace(/\/+$/, '');
/** Public origin the API was configured with (PUBLIC_ORIGIN); owner writes require a matching Origin header. */
export const ORIGIN = __ENV.PUBLIC_ORIGIN || 'http://localhost:3300';
/** Setup token of the disposable stack (SETUP_TOKEN); used only when no owner exists yet. */
export const SETUP_TOKEN = __ENV.E2E_SETUP_TOKEN || 'bbd-os-disposable-test-token';
/** Owner password created/used by the load run (disposable stacks only). */
export const OWNER_PASSWORD = __ENV.OWNER_PASSWORD || 'test-owner-password-42';
/** OpenAI-compatible base URL of tests/integration/fake_model.py as seen from the API containers. */
export const FAKE_MODEL_URL = __ENV.FAKE_MODEL_URL || 'http://fake-model:8000/v1';
/** Cookie names issued by core/auth (SESSION_COOKIE / CSRF_COOKIE). */
export const SESSION_COOKIE = 'bbd_session';
export const CSRF_COOKIE = 'bbd_csrf';

/**
 * Parses a positive number from the environment.
 * @param {string} name Environment variable name.
 * @param {number} fallback Value used when unset or invalid.
 * @returns {number}
 */
export function numEnv(name, fallback) {
  const value = Number(__ENV[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/** Multiplier applied to every latency threshold (1 = documented SLO; raise it on slow laptops). */
export const LATENCY_SCALE = numEnv('SLO_LATENCY_SCALE', 1);
/** Scenario length multiplier: 1 = full run, 0.1 = smoke. */
export const DURATION_SCALE = numEnv('LOAD_DURATION_SCALE', 1);

/**
 * Scales a duration in seconds by DURATION_SCALE and renders it as a k6 duration string.
 * @param {number} seconds Nominal duration.
 * @returns {string} For example `"30s"`.
 */
export function dur(seconds) {
  return `${Math.max(1, Math.round(seconds * DURATION_SCALE))}s`;
}
