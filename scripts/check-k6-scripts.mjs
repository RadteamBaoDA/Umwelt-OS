// Offline validator for the k6 scripts under tests/load (no k6 binary or Docker needed).
//
// k6 scripts are ES modules that import the `k6/*` runtime modules, which Node cannot resolve. This script registers a
// loader hook that replaces every `k6` / `k6/*` import with a recording stub, then imports each entry script. That
// proves (1) the module graph parses and its init code runs, (2) every scenario's `exec` names an exported function,
// (3) every threshold refers to a built-in or declared custom metric (k6 rejects unknown ones at start-up) and
// (4) the `__ENV`/`__ITER` globals are the only undeclared runtime names used at init.
//
// Usage: node scripts/check-k6-scripts.mjs   (exit code 1 on any problem)
import { register } from 'node:module';
import { readdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const loadDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../tests/load');
const BUILTIN_METRICS = new Set([
  'http_req_failed', 'http_req_duration', 'http_req_waiting', 'http_req_blocked', 'http_reqs', 'checks',
  'iterations', 'iteration_duration', 'vus', 'vus_max', 'data_sent', 'data_received', 'dropped_iterations',
]);

// Stub source for each k6 module. Metric constructors record their names on globalThis for the threshold check.
const STUBS = {
  'k6': `export const check = () => true; export const sleep = () => {}; export const group = (n, f) => f();
    export const fail = (m) => { throw new Error(m); };`,
  'k6/http': `const noop = () => ({ status: 200, body: '', timings: { waiting: 0 }, json: () => ({}) });
    const http = { get: noop, post: noop, put: noop, del: noop, file: (d, n, t) => ({ data: d, filename: n, content_type: t }),
      cookieJar: () => ({ set() {}, cookiesForURL: () => ({}) }), setResponseCallback() {},
      expectedStatuses: (...a) => a };
    export default http;`,
  'k6/metrics': `const make = (kind) => class { constructor(name) { (globalThis.__k6Metrics ||= new Map()).set(name, kind); }
      add() {} };
    export const Trend = make('trend'); export const Counter = make('counter'); export const Rate = make('rate');
    export const Gauge = make('gauge');`,
  'k6/execution': 'export default {};',
};

const hooks = `
const STUBS = ${JSON.stringify(STUBS)};
export async function resolve(specifier, context, next) {
  if (specifier === 'k6' || specifier.startsWith('k6/')) return { url: 'k6-stub:' + specifier, shortCircuit: true };
  return next(specifier, context);
}
export async function load(url, context, next) {
  if (url.startsWith('k6-stub:')) return { format: 'module', source: STUBS[url.slice(8)] ?? 'export default {};', shortCircuit: true };
  return next(url, context);
}`;
register('data:text/javascript,' + encodeURIComponent(hooks), import.meta.url);

globalThis.__ENV = {};
globalThis.__ITER = 0;
globalThis.__VU = 1;

/** Returns the metric name a threshold key refers to (`name{tag:value}` -> `name`). */
const baseMetric = (key) => key.replace(/\{.*\}$/, '');

const problems = [];
const entries = readdirSync(loadDir).filter((f) => f.endsWith('.js'));
const libs = readdirSync(path.join(loadDir, 'lib')).filter((f) => f.endsWith('.js')).map((f) => `lib/${f}`);

for (const file of [...libs, ...entries]) {
  try {
    await import(pathToFileURL(path.join(loadDir, file)).href);
  } catch (error) {
    problems.push(`${file}: failed to load: ${error.message}`);
  }
}

const declared = globalThis.__k6Metrics ?? new Map();
for (const file of entries) {
  const mod = await import(pathToFileURL(path.join(loadDir, file)).href).catch(() => null);
  if (!mod || !mod.options) continue;
  for (const [name, scenario] of Object.entries(mod.options.scenarios ?? {})) {
    const exec = scenario.exec ?? 'default';
    if (typeof mod[exec] !== 'function') problems.push(`${file}: scenario ${name} exec "${exec}" is not an exported function`);
  }
  for (const key of Object.keys(mod.options.thresholds ?? {})) {
    const metric = baseMetric(key);
    if (!BUILTIN_METRICS.has(metric) && !declared.has(metric)) problems.push(`${file}: threshold on unknown metric "${metric}"`);
  }
  if (mod.options.noCookiesReset !== true) problems.push(`${file}: options.noCookiesReset must be true (the session cookie is installed once per VU)`);
  if (typeof mod.setup !== 'function') problems.push(`${file}: missing setup()`);
}

if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log(`k6 scripts OK: ${entries.length} entry scripts, ${libs.length} libs, ${declared.size} custom metrics`);
