#!/usr/bin/env node
/**
 * Prettier gate for files touched since a base ref.
 *
 * The tree predates the prettier config and formatting it wholesale pushes ~55-65 components past the
 * enforced `max-lines-per-function` (100) budget, so a repo-wide check is not yet possible. This gate keeps
 * every new or modified source file formatted until those components are split; then `format:check:all`
 * can replace it.
 *
 * Base ref: `FORMAT_BASE` env (CI passes the PR base SHA); defaults to `HEAD`, i.e. uncommitted changes.
 * Exit code 1 lists the unformatted files; 0 otherwise (including when nothing changed).
 */
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';

const git = (args) => execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).split('\n').filter(Boolean);

/** True when `ref` resolves to a commit in this checkout. */
function commitExists(ref) {
  try {
    execFileSync('git', ['cat-file', '-e', `${ref}^{commit}`], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

/**
 * Resolve the diff base. An all-zero SHA (first push of a new branch) or an unreachable commit (force push)
 * is treated as missing and falls back to HEAD~1, then HEAD, so the gate never fails on git plumbing.
 */
function resolveBase() {
  const requested = process.env.FORMAT_BASE;
  if (requested && !/^0+$/.test(requested) && commitExists(requested)) return requested;
  if (requested) console.warn(`format-check: base "${requested}" unusable; falling back`);
  return commitExists('HEAD~1') ? 'HEAD~1' : 'HEAD';
}

const base = resolveBase();

const changed = new Set([
  ...git(['diff', '--name-only', '--diff-filter=ACMR', base, '--', 'src']),
  ...git(['ls-files', '--others', '--exclude-standard', '--', 'src']),
]);
const root = git(['rev-parse', '--show-toplevel'])[0];
const files = [...changed]
  .filter((f) => /\.(ts|tsx)$/.test(f) && !f.endsWith('api-types.gen.ts'))
  .map((f) => (f.startsWith('apps/web/') ? `${root}/${f}` : f));

if (files.length === 0) {
  console.log('format-check: no changed ts/tsx files');
  process.exit(0);
}
// Run prettier's CLI with this Node binary: spawning `npx` fails on Windows (it is npx.cmd, so execFileSync throws
// ENOENT without `shell: true`), and that failure used to exit 1 silently.
const prettierBin = createRequire(import.meta.url).resolve('prettier/bin/prettier.cjs');
try {
  execFileSync(process.execPath, [prettierBin, '--check', ...files], { stdio: 'inherit' });
} catch (error) {
  // Prettier already printed the unformatted files; a spawn failure (no status) has no output, so say why.
  if (error.status == null) console.error(`format-check: could not run prettier: ${error.message}`);
  process.exit(1);
}
