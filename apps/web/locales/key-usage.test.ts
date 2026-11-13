import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import { flatCatalogs } from '@locales';

/**
 * Static i18n completeness gate. Walks every production source file with the TypeScript compiler API, binds each
 * `const t = useTranslations('ns')` / `await getTranslations('ns')` to its enclosing scope, and asserts that every
 * statically resolvable key (`t('k')`, `t.rich/raw/has('k')`, literal ternaries, and `t(TABLE[x])` against a file-level
 * `as const` key table) exists in BOTH the en-us and vi-vi flat catalogs. Keys it cannot resolve must be listed in
 * {@link ALLOWLIST} with a reason. Unused keys are only reported (console summary), never failed.
 */

const SRC = join(__dirname, '..', 'src');

/**
 * Reviewed unresolvable call sites, keyed `<posix path under src>::<call text>`. Each entry needs a why-comment.
 * Calls on a `t` that is only a function parameter are checked at the binding site of the caller, so they are listed
 * by file when the parameter is namespace-less.
 */
const ALLOWLIST: Record<string, string> = {};

const KEY_METHODS = new Set(['rich', 'raw', 'has', 'markup']);

interface Binding {
  name: string;
  ns: string;
  scope: ts.Node;
}

/** Recursively list `.ts`/`.tsx` production files (tests, stories and declaration files excluded). */
function listSources(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return entry === 'node_modules' ? [] : listSources(full);
    return /\.(ts|tsx)$/.test(entry) && !/\.(test|spec)\.|\.d\.ts$|\.gen\./.test(entry) ? [full] : [];
  });
}

/** Innermost function-like (or source file) node that owns `node`, used as the binding scope. */
function scopeOf(node: ts.Node): ts.Node {
  let cur: ts.Node = node.parent;
  while (cur && !ts.isFunctionLike(cur) && !ts.isSourceFile(cur)) cur = cur.parent;
  return cur;
}

/** The namespace string of `useTranslations('ns')` / `getTranslations('ns')` / `getTranslations({ namespace: 'ns' })`. */
function bindingNamespace(init: ts.Expression): string | null | undefined {
  let expr = init;
  while (ts.isAwaitExpression(expr) || ts.isParenthesizedExpression(expr)) expr = expr.expression;
  if (!ts.isCallExpression(expr) || !ts.isIdentifier(expr.expression)) return undefined;
  if (!['useTranslations', 'getTranslations'].includes(expr.expression.text)) return undefined;
  const arg = expr.arguments[0];
  if (!arg) return null;
  if (ts.isStringLiteralLike(arg)) return arg.text;
  if (ts.isObjectLiteralExpression(arg)) {
    for (const p of arg.properties) {
      if (ts.isPropertyAssignment(p) && ts.isIdentifier(p.name) && p.name.text === 'namespace' && ts.isStringLiteralLike(p.initializer)) {
        return p.initializer.text;
      }
    }
  }
  return null;
}

/** All string literals reachable inside an object/array literal (the values of a key table). */
function literalValues(node: ts.Node, out: string[] = []): string[] {
  if (ts.isStringLiteralLike(node)) out.push(node.text);
  else ts.forEachChild(node, (c) => literalValues(c, out));
  return out;
}

/** Resolve a key argument to the set of literal keys it can take, or `null` when it is dynamic. */
function resolveKeys(arg: ts.Expression, tables: Map<string, ts.Expression>): string[] | null {
  if (ts.isStringLiteralLike(arg)) return [arg.text];
  if (ts.isParenthesizedExpression(arg) || ts.isAsExpression(arg) || ts.isNonNullExpression(arg))
    return resolveKeys(arg.expression, tables);
  if (ts.isConditionalExpression(arg)) {
    const a = resolveKeys(arg.whenTrue, tables);
    const b = resolveKeys(arg.whenFalse, tables);
    return a && b ? [...a, ...b] : null;
  }
  if (ts.isBinaryExpression(arg) && arg.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken) {
    const a = resolveKeys(arg.left, tables);
    const b = resolveKeys(arg.right, tables);
    return a && b ? [...a, ...b] : null;
  }
  // TABLE[x] / TABLE.prop: every literal in the file-level `as const` table must be a key.
  const base = ts.isElementAccessExpression(arg) || ts.isPropertyAccessExpression(arg) ? arg.expression : arg;
  if (ts.isIdentifier(base) && tables.has(base.text)) return literalValues(tables.get(base.text)!);
  return null;
}

interface Finding {
  file: string;
  ns: string;
  key: string;
  text: string;
}

interface Scan {
  resolved: Finding[];
  unresolved: { file: string; text: string }[];
  /** Non-literal key arguments left to the TypeScript key typing. */
  typeChecked: number;
  /** Per catalog namespace: flat keys referenced somewhere, including dynamic-prefix references. */
  usedPrefixes: Map<string, Set<string>>;
}

/** Scan one source file; adds resolved `ns.key` findings and unresolved call sites to `scan`. */
function scanFile(path: string, scan: Scan): void {
  const text = readFileSync(path, 'utf8');
  if (!text.includes('useTranslations') && !text.includes('getTranslations')) return;
  const file = relative(SRC, path).split(sep).join('/');
  const sf = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, path.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const bindings: Binding[] = [];
  const tables = new Map<string, ts.Expression>();
  const calls: ts.CallExpression[] = [];
  const visit = (n: ts.Node): void => {
    if (ts.isVariableDeclaration(n) && n.initializer && ts.isIdentifier(n.name)) {
      const ns = bindingNamespace(n.initializer);
      if (ns !== undefined) bindings.push({ name: n.name.text, ns: ns ?? '', scope: scopeOf(n) });
      else if (ts.isSourceFile(scopeOf(n))) tables.set(n.name.text, n.initializer);
    }
    if (ts.isCallExpression(n)) calls.push(n);
    ts.forEachChild(n, visit);
  };
  visit(sf);

  for (const call of calls) {
    const callee = call.expression;
    let name: string | null = null;
    if (ts.isIdentifier(callee)) name = callee.text;
    else if (ts.isPropertyAccessExpression(callee) && ts.isIdentifier(callee.expression) && KEY_METHODS.has(callee.name.text))
      name = callee.expression.text;
    if (!name || !call.arguments[0]) continue;
    // Innermost binding of `name` whose scope contains the call.
    const owner = bindings
      .filter((b) => b.name === name && call.pos >= b.scope.pos && call.end <= b.scope.end)
      .sort((a, b) => b.scope.pos - a.scope.pos)[0];
    if (!owner) continue;
    const snippet = call.getText(sf).replace(/\s+/g, ' ').slice(0, 120);
    const keys = resolveKeys(call.arguments[0], tables);
    if (!keys) {
      // Template literal: record its static prefix so the unused-key report does not flag the whole family.
      const arg = call.arguments[0];
      if (ts.isTemplateExpression(arg) && owner.ns) noteDynamicPrefix(scan, owner.ns, arg.head.text);
      // Identifier/call arguments are constrained to existing en-us keys by `tsc` (global.d.ts AppConfig.Messages) and
      // vi parity is asserted by catalog-parity.test.ts. A runtime-built template string defeats both, so only those fail.
      if (ts.isTemplateExpression(arg)) scan.unresolved.push({ file, text: snippet });
      else scan.typeChecked += 1;
      continue;
    }
    for (const key of keys) scan.resolved.push({ file, ns: owner.ns, key, text: snippet });
  }
}

/** Record `ns` + static template head as a used prefix (best effort for the unused-key report). */
function noteDynamicPrefix(scan: Scan, ns: string, head: string): void {
  const [catalog, ...rest] = ns.split('.');
  const set = scan.usedPrefixes.get(catalog) ?? new Set<string>();
  set.add([...rest, head].join('.'));
  scan.usedPrefixes.set(catalog, set);
}

const scan: Scan = { resolved: [], unresolved: [], typeChecked: 0, usedPrefixes: new Map() };
for (const f of listSources(SRC)) scanFile(f, scan);

type Catalogs = Record<string, Record<string, string>>;

/** Does `key` (relative to namespace `ns`) exist in the flat catalog of `locale`? */
function exists(locale: 'en-us' | 'vi-vi', ns: string, key: string): boolean {
  const [catalog, ...rest] = ns.split('.');
  const flat = (flatCatalogs[locale] as Catalogs)[catalog];
  return flat !== undefined && Object.hasOwn(flat, [...rest, key].join('.'));
}

describe('i18n static key usage', () => {
  it('finds a plausible number of translation call sites (guards against a silently broken scanner)', () => {
    expect(scan.resolved.length).toBeGreaterThan(1500);
  });

  it.each(['en-us', 'vi-vi'] as const)('every statically referenced key exists in %s', (locale) => {
    const missing = scan.resolved.filter((f) => !exists(locale, f.ns, f.key)).map((f) => `${f.file}: ${f.ns}.${f.key}`);
    expect([...new Set(missing)]).toEqual([]);
  });

  it('has no unreviewed runtime-built (template literal) keys', () => {
    const unreviewed = scan.unresolved.filter((u) => !(`${u.file}::${u.text}` in ALLOWLIST)).map((u) => `${u.file}::${u.text}`);
    expect(unreviewed).toEqual([]);
  });

  it('reports unused keys per namespace (informational)', () => {
    const used = new Map<string, Set<string>>();
    for (const f of scan.resolved) {
      const [catalog, ...rest] = f.ns.split('.');
      const set = used.get(catalog) ?? new Set<string>();
      set.add([...rest, f.key].join('.'));
      used.set(catalog, set);
    }
    const lines: string[] = [];
    for (const [ns, flat] of Object.entries(flatCatalogs['en-us'] as Catalogs)) {
      const usedKeys = used.get(ns) ?? new Set<string>();
      const prefixes = [...(scan.usedPrefixes.get(ns) ?? [])];
      const unused = Object.keys(flat).filter((k) => !usedKeys.has(k) && !prefixes.some((p) => k.startsWith(p)));
      if (unused.length > 0) lines.push(`  ${ns}: ${unused.length}/${Object.keys(flat).length} unused`);
    }
    // Some catalogs are consumed through other paths (typed `getCatalog`, server-only readers), so this never fails.
    console.info(`[i18n-unused-keys]\n${lines.join('\n') || '  none'}`);
    expect(true).toBe(true);
  });
});
