// Login + CSRF flow helpers.
//
// The API rate-limits login/setup to 5 attempts per minute per address, and a session read with a stale CSRF cookie
// rotates the token for every client of that session. So the run logs in ONCE in setup() and each VU replays the
// same session/CSRF cookie pair; no VU calls /auth/session afterwards (that would rotate the shared token).
import http from 'k6/http';
import { check, fail } from 'k6';
import { API_URL, ORIGIN, SETUP_TOKEN, OWNER_PASSWORD, SESSION_COOKIE, CSRF_COOKIE } from './config.js';

const JSON_HEADERS = { 'Content-Type': 'application/json' };

/**
 * Reads a cookie from the current cookie jar for the API origin.
 * @param {string} name Cookie name.
 * @returns {string | undefined} Cookie value (k6's cookiesForURL maps name to an array of value strings).
 */
function jarCookie(name) {
  const cookies = http.cookieJar().cookiesForURL(`${API_URL}/`);
  return cookies[name] && cookies[name][0];
}

/**
 * Fetches a fresh anonymous CSRF token (GET /api/v1/auth/csrf); the signed cookie lands in the jar.
 * @returns {string} Token to send as X-CSRF-Token.
 */
export function anonymousCsrf() {
  const res = http.get(`${API_URL}/api/v1/auth/csrf`, { tags: { endpoint: 'auth_csrf' } });
  check(res, { 'csrf 200': (r) => r.status === 200 }) || fail(`csrf failed: ${res.status} ${res.body}`);
  return res.json('csrfToken');
}

/**
 * Creates the owner when none exists (idempotent: skips when setup is already done).
 * Requires the disposable stack's setup token. Aborts the run (fail) on any unexpected status.
 */
export function ensureOwner() {
  const status = http.get(`${API_URL}/api/v1/auth/setup-status`, { tags: { endpoint: 'auth_setup_status' } });
  if (status.status === 200 && status.json('setupRequired') === false) return;
  const token = anonymousCsrf();
  const res = http.post(`${API_URL}/api/v1/auth/setup`, JSON.stringify({ password: OWNER_PASSWORD }), {
    headers: { ...JSON_HEADERS, Origin: ORIGIN, 'X-CSRF-Token': token, 'X-Setup-Token': SETUP_TOKEN },
    tags: { endpoint: 'auth_setup' },
  });
  // 409 = a concurrent setup won the race; the owner exists either way.
  check(res, { 'setup 201/409': (r) => r.status === 201 || r.status === 409 }) || fail(`setup failed: ${res.status} ${res.body}`);
}

/**
 * Logs in as the owner and returns the cookie/CSRF material every VU needs to issue authenticated requests.
 * @returns {{session: string, csrfCookie: string, csrfToken: string}}
 */
export function login() {
  const token = anonymousCsrf();
  const res = http.post(`${API_URL}/api/v1/auth/login`, JSON.stringify({ password: OWNER_PASSWORD }), {
    headers: { ...JSON_HEADERS, Origin: ORIGIN, 'X-CSRF-Token': token },
    tags: { endpoint: 'auth_login' },
  });
  check(res, { 'login 200': (r) => r.status === 200 }) || fail(`login failed: ${res.status} ${res.body}`);
  // Align the CSRF cookie with the session (login may already have rotated it); one read here, never in VUs.
  const state = http.get(`${API_URL}/api/v1/auth/session`, { tags: { endpoint: 'auth_session' } });
  check(state, { 'session 200': (r) => r.status === 200 }) || fail(`session failed: ${state.status} ${state.body}`);
  return {
    session: jarCookie(SESSION_COOKIE),
    csrfCookie: jarCookie(CSRF_COOKIE),
    csrfToken: state.json('csrfToken'),
  };
}

/**
 * Installs the shared owner session into the current VU's cookie jar. Call once per VU (via ensureVuSession) and only with `noCookiesReset: true`, since k6 otherwise resets the jar every iteration.
 * @param {{session: string, csrfCookie: string}} auth The object returned by login() in setup().
 */
export function installSession(auth) {
  const jar = http.cookieJar();
  jar.set(`${API_URL}/`, SESSION_COOKIE, auth.session, { path: '/' });
  jar.set(`${API_URL}/`, CSRF_COOKIE, auth.csrfCookie, { path: '/' });
}

/**
 * Builds headers for an owner write (CSRF token plus the Origin the API enforces).
 * @param {{csrfToken: string}} auth The object returned by login().
 * @param {Record<string, string>} [extra] Additional headers (for example Content-Type).
 * @returns {Record<string, string>}
 */
export function writeHeaders(auth, extra = {}) {
  return { Origin: ORIGIN, 'X-CSRF-Token': auth.csrfToken, ...extra };
}
