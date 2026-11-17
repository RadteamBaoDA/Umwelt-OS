/**
 * Bundles the libraries available to generative-UI sandbox widgets into single same-origin ESM files
 * (`public/vendor/genui/<lib>.mjs`, plus `leaflet.css`), so the iframe CSP never needs a CDN.
 * Run via `prebuild` / `predev`; output is gitignored.
 */
import { build } from 'esbuild';
import { cp, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const out = resolve(root, 'public/vendor/genui');
const libs = ['three', 'd3', 'chart.js', 'gsap', 'leaflet'];
// Leaflet is UMD (no ESM named exports), so expose it as default and `L`.
const entry = (lib) =>
  lib === 'leaflet' ? "import L from 'leaflet'; export default L; export { L };" : `export * from '${lib}';`;

await mkdir(out, { recursive: true });
await Promise.all(
  libs.map((lib) =>
    build({
      stdin: { contents: entry(lib), resolveDir: root },
      bundle: true,
      format: 'esm',
      minify: true,
      outfile: resolve(out, `${lib}.mjs`),
      logLevel: 'warning',
    }),
  ),
);
await cp(createRequire(import.meta.url).resolve('leaflet/dist/leaflet.css'), resolve(out, 'leaflet.css'));
console.log(`vendor-genui: wrote ${libs.length} bundles to ${out}`);
