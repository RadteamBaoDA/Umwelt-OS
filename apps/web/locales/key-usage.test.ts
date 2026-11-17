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

/**
 * Translators whose type erases the namespace, so the scanner cannot bind them from the source. Keyed by receiver name
 * (identifier `translate(...)` or property `obj.tAgents(...)`); the value is the namespace the source binds them to.
 * - `translate`: `indexSummary` in modules/search/search-page.tsx casts the `useTranslations('entities')` translator.
 * - `tAgents`: `ChatSendDeps.tAgents` in modules/chat/use-chat-send.ts, a `useTranslations('aiSettings') as unknown as Translator`.
 * A new untyped translator must be added here, otherwise its keys go unchecked.
 */
const UNTYPED_TRANSLATOR_NS: Record<string, string> = { translate: 'entities', tAgents: 'aiSettings' };

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

/** Strip `await`, parentheses, `as` casts (incl. `as unknown as X`) and non-null assertions to reach the core expression. */
function unwrapExpr(expr: ts.Expression): ts.Expression {
  let cur = expr;
  while (ts.isAwaitExpression(cur) || ts.isParenthesizedExpression(cur) || ts.isAsExpression(cur) || ts.isNonNullExpression(cur))
    cur = cur.expression;
  return cur;
}

/**
 * The namespace string of `useTranslations('ns')` / `getTranslations('ns')` / `getTranslations({ namespace: 'ns' })`.
 * Casts are unwrapped so `useTranslations('aiSettings') as unknown as Translator` still binds the namespace.
 * Returns `undefined` when the initializer is not a translator call, `null` when the namespace is not a literal.
 */
function bindingNamespace(init: ts.Expression): string | null | undefined {
  const expr = unwrapExpr(init);
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

/**
 * The namespace named by a translator type: `ReturnType<typeof useTranslations<'ns'>>`, or a reference to one of the
 * `aliases` (file-local or imported `type X = ReturnType<typeof useTranslations<'ns'>>`). Returns `undefined` for any
 * other type, such as a plain function signature, whose namespace the gate cannot know from the type alone.
 */
function nsFromTypeNode(type: ts.TypeNode, aliases: Map<string, string>): string | undefined {
  if (!ts.isTypeReferenceNode(type)) return undefined;
  const name = type.typeName.getText();
  if (aliases.has(name)) return aliases.get(name);
  if (name !== 'ReturnType') return undefined;
  const arg = type.typeArguments?.[0];
  if (!arg || !ts.isTypeQueryNode(arg) || arg.exprName.getText() !== 'useTranslations') return undefined;
  const ns = arg.typeArguments?.[0];
  return ns && ts.isLiteralTypeNode(ns) && ts.isStringLiteral(ns.literal) ? ns.literal.text : undefined;
}

/**
 * Translator type aliases visible in one file: its own `type X = ReturnType<typeof useTranslations<'ns'>>` declarations
 * plus named imports of such aliases from another src file (followed one level, which is how the codebase re-exports them).
 */
function translatorAliases(path: string, sf: ts.SourceFile): Map<string, string> {
  const out = new Map<string, string>();
  for (const st of sf.statements) {
    if (ts.isTypeAliasDeclaration(st)) {
      const ns = nsFromTypeNode(st.type, new Map());
      if (ns !== undefined) out.set(st.name.text, ns);
    } else if (ts.isImportDeclaration(st) && ts.isStringLiteral(st.moduleSpecifier)) {
      const target = resolveSrcImport(path, st.moduleSpecifier.text);
      const bindings = st.importClause?.namedBindings;
      if (!target || !bindings || !ts.isNamedImports(bindings)) continue;
      const exported = localAliasesOf(target);
      for (const el of bindings.elements) {
        const ns = exported.get((el.propertyName ?? el.name).text);
        if (ns !== undefined) out.set(el.name.text, ns);
      }
    }
  }
  return out;
}

const localAliasCache = new Map<string, Map<string, string>>();
/** Cached `translatorAliases`-style map of the type aliases a file *declares* (no import following). */
function localAliasesOf(path: string): Map<string, string> {
  const cached = localAliasCache.get(path);
  if (cached) return cached;
  const sf = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true);
  const out = new Map<string, string>();
  for (const st of sf.statements) {
    if (!ts.isTypeAliasDeclaration(st)) continue;
    const ns = nsFromTypeNode(st.type, new Map());
    if (ns !== undefined) out.set(st.name.text, ns);
  }
  localAliasCache.set(path, out);
  return out;
}

/** Resolve a relative or `@/` specifier to a src file path, or `null` for packages and unresolvable paths. */
function resolveSrcImport(fromPath: string, spec: string): string | null {
  const base = spec.startsWith('@/') ? join(SRC, spec.slice(2)) : spec.startsWith('.') ? join(join(fromPath, '..'), spec) : null;
  if (!base) return null;
  for (const cand of [`${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')]) {
    try {
      if (statSync(cand).isFile()) return cand;
    } catch {
      /* not this candidate */
    }
  }
  return null;
}

/**
 * All string literals reachable inside an object/array literal (the values of a key table).
 * The forEachChild callback must return `undefined`: a truthy return (the accumulator) stops the iteration early and
 * silently drops every table entry after the first child, which made table-keyed lookups go unchecked. Type nodes are
 * skipped: a `satisfies Record<Row['status'], string>` clause holds the property name `'status'` as a type literal, which
 * is not a message key.
 */
function literalValues(node: ts.Node, out: string[] = []): string[] {
  if (ts.isTypeNode(node)) return out;
  if (ts.isStringLiteralLike(node)) out.push(node.text);
  else
    ts.forEachChild(node, (c) => {
      literalValues(c, out);
    });
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

/**
 * The innermost binding named `name` whose scope encloses the source range `[pos, end)`, or `undefined`.
 * Innermost is decided by the largest scope start, so a shadowing `t` in a nested function wins over the outer one.
 */
function innermostBinding(bindings: Binding[], name: string, pos: number, end: number): Binding | undefined {
  return bindings.filter((b) => b.name === name && pos >= b.scope.pos && end <= b.scope.end).sort((a, b) => b.scope.pos - a.scope.pos)[0];
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
  const aliases = translatorAliases(path, sf);
  const aliasDecls: { name: string; source: string; scope: ts.Node }[] = [];

  /**
   * Record one `const/let x[: T] = init` declaration as a translator binding, a deferred alias of another binding
   * (`const translate = t as unknown as ...`), or a file-level key table. Bindings are typed by namespace literal,
   * by the initializer call, or by a translator type; anything else is ignored.
   */
  const recordDeclaration = (n: ts.VariableDeclaration & { name: ts.Identifier }): void => {
    const scope = scopeOf(n);
    const name = n.name.text;
    if (n.initializer) {
      const ns = bindingNamespace(n.initializer);
      if (ns !== undefined) return void bindings.push({ name, ns: ns ?? '', scope });
    }
    const typed = n.type ? nsFromTypeNode(n.type, aliases) : undefined;
    if (typed !== undefined) return void bindings.push({ name, ns: typed, scope });
    if (!n.initializer) return;
    const core = unwrapExpr(n.initializer);
    if (ts.isIdentifier(core)) aliasDecls.push({ name, source: core.text, scope });
    if (ts.isSourceFile(scope)) tables.set(name, n.initializer);
  };
  const visit = (n: ts.Node): void => {
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name)) recordDeclaration(n as ts.VariableDeclaration & { name: ts.Identifier });
    // Parameters typed with a translator (`t: ReturnType<typeof useTranslations<'x'>>`) bind that namespace in their function.
    if (ts.isParameter(n) && ts.isIdentifier(n.name) && n.type) {
      const ns = nsFromTypeNode(n.type, aliases);
      if (ns !== undefined) bindings.push({ name: n.name.text, ns, scope: scopeOf(n) });
    }
    if (ts.isCallExpression(n)) calls.push(n);
    ts.forEachChild(n, visit);
  };
  visit(sf);
  // Aliases resolve after the walk so an alias may name a binding declared later in source order only if it was seen first.
  for (const a of aliasDecls) {
    const src = innermostBinding(bindings, a.source, a.scope.pos, a.scope.end);
    if (src?.ns) bindings.push({ name: a.name, ns: src.ns, scope: a.scope });
  }

  for (const call of calls) {
    const callee = call.expression;
    let name: string | null = null;
    let pinnedNs: string | undefined;
    if (ts.isIdentifier(callee)) {
      name = callee.text;
      pinnedNs = UNTYPED_TRANSLATOR_NS[name];
    } else if (ts.isPropertyAccessExpression(callee) && ts.isIdentifier(callee.expression) && KEY_METHODS.has(callee.name.text)) {
      name = callee.expression.text;
    } else if (ts.isPropertyAccessExpression(callee)) {
      // Untyped translator stored on an object (`d.tAgents('k')`): only the pinned table applies, never a local binding.
      name = callee.name.text;
      pinnedNs = UNTYPED_TRANSLATOR_NS[name];
      if (pinnedNs === undefined) continue;
    }
    if (!name || !call.arguments[0]) continue;
    const bound =
      ts.isPropertyAccessExpression(callee) && pinnedNs !== undefined ? undefined : innermostBinding(bindings, name, call.pos, call.end);
    const owner = bound ?? (pinnedNs !== undefined ? { name, ns: pinnedNs, scope: sf } : undefined);
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

  it('binds receivers the namespace cannot be read from the call (typed params, casts, pinned translators)', () => {
    // Typed parameter: `t: ReturnType<typeof useTranslations<'gadgetSettings'>>` in core/renderer-labels.ts.
    expect(scan.resolved.some((f) => f.file === 'core/renderer-labels.ts' && f.ns === 'gadgetSettings')).toBe(true);
    // Pinned untyped translator on an object: `d.tAgents('agent.unavailable')` in modules/chat/use-chat-send.ts.
    expect(
      scan.resolved.some((f) => f.file === 'modules/chat/use-chat-send.ts' && f.ns === 'aiSettings' && f.key === 'agent.unavailable'),
    ).toBe(true);
    // Pinned cast alias: `translate('indexPreparing')` in modules/search/search-page.tsx.
    expect(
      scan.resolved.some((f) => f.file === 'modules/search/search-page.tsx' && f.ns === 'entities' && f.key === 'indexPreparing'),
    ).toBe(true);
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
