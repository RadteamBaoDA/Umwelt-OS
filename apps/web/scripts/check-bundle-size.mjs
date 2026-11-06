#!/usr/bin/env node
/**
 * Bundle-size gate, run after `next build` (Turbopack prints no per-route sizes, so we derive them).
 *
 * Per route, "first-load JS" is the gzip size of the union of: the root main/polyfill chunks from
 * `build-manifest.json`, plus every chunk the route's `page_client-reference-manifest.js` references
 * (`clientModules` + `entryJSFiles`). This is an upper bound: it also counts lazily loaded client
 * components the page references. "Total client JS" is the gzip sum of `.next/static/chunks/*.js`.
 *
 * Budgets live in `bundle-budget.json` (KB, gzip). Exit 1 when any route or the total exceeds its budget.
 * `--measure` prints numbers without failing. Raise a budget with a reviewed edit to the JSON (docs/development.md).
 */
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const nextDir = join(here, '..', '.next');
const budget = JSON.parse(readFileSync(join(here, '..', 'bundle-budget.json'), 'utf8'));
const measureOnly = process.argv.includes('--measure');

if (!existsSync(join(nextDir, 'build-manifest.json'))) {
  console.error('check-bundle-size: .next/build-manifest.json missing; run `npm run build` first');
  process.exit(2);
}

const gzipCache = new Map();
/** Gzip size in bytes of a build-relative JS file (cached); 0 when the file is absent. */
function gzipSize(rel) {
  if (!gzipCache.has(rel)) {
    const file = join(nextDir, decodeURIComponent(rel).replace(/^\/?(_next\/)?/, ''));
    gzipCache.set(rel, existsSync(file) ? gzipSync(readFileSync(file)).length : 0);
  }
  return gzipCache.get(rel);
}

/** Normalises manifest chunk references ("/_next/static/..." or "static/...") to one key. */
const norm = (p) => p.replace(/^\/?(_next\/)?/, '');

const build = JSON.parse(readFileSync(join(nextDir, 'build-manifest.json'), 'utf8'));
const shared = [...(build.polyfillFiles ?? []), ...(build.rootMainFiles ?? [])].map(norm);

/** Recursively lists `page_client-reference-manifest.js` files under `server/app`. */
function manifests(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return manifests(full);
    return name === 'page_client-reference-manifest.js' ? [full] : [];
  });
}

/** Evaluates a client-reference manifest and returns its `[route, manifest]` pairs. */
function loadManifest(file) {
  const sandbox = { globalThis: {} };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(readFileSync(file, 'utf8'), sandbox);
  return Object.entries(sandbox.__RSC_MANIFEST ?? {});
}

const routes = {};
for (const file of manifests(join(nextDir, 'server', 'app'))) {
  for (const [route, m] of loadManifest(file)) {
    const chunks = new Set(shared);
    for (const mod of Object.values(m.clientModules ?? {})) for (const c of mod.chunks ?? []) chunks.add(norm(c));
    for (const list of Object.values(m.entryJSFiles ?? {})) for (const c of list) chunks.add(norm(c));
    const js = [...chunks].filter((c) => c.endsWith('.js'));
    routes[route] = js.reduce((sum, c) => sum + gzipSize(c), 0);
  }
}

const chunkDir = join(nextDir, 'static', 'chunks');
const total = readdirSync(chunkDir)
  .filter((f) => f.endsWith('.js'))
  .reduce((sum, f) => sum + gzipSize(`static/chunks/${f}`), 0);

const kb = (n) => Math.round((n / 1024) * 10) / 10;
const ranked = Object.entries(routes).sort((a, b) => b[1] - a[1]);
console.log('Largest routes by first-load JS (gzip KB):');
for (const [route, size] of process.argv.includes('--all') ? ranked : ranked.slice(0, 8)) console.log(`  ${String(kb(size)).padStart(8)}  ${route}`);
const worst = ranked[0] ?? ['(none)', 0];
console.log(`Max route first-load: ${kb(worst[1])} KB (budget ${budget.maxRouteFirstLoadKb})`);
console.log(`Total client JS:      ${kb(total)} KB (budget ${budget.totalClientKb})`);

const failures = [];
for (const [route, size] of ranked) {
  const limit = budget.routes?.[route] ?? budget.maxRouteFirstLoadKb;
  if (kb(size) > limit) failures.push(`${route}: ${kb(size)} KB > ${limit} KB`);
}
if (kb(total) > budget.totalClientKb) failures.push(`total client JS: ${kb(total)} KB > ${budget.totalClientKb} KB`);

if (failures.length && !measureOnly) {
  console.error(`\nBundle budget exceeded:\n  ${failures.join('\n  ')}\nSee docs/development.md (Bundle-size budget) before raising.`);
  process.exit(1);
}
console.log(failures.length ? '(measure mode: over budget, not failing)' : 'Bundle budget OK');
