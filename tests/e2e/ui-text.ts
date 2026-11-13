import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { E2E_LOCALE } from './fixtures';

/** Parsed flat catalogs by namespace for the owner locale, so each JSON file is read once per worker. */
const catalogs = new Map<string, Record<string, string>>();

/**
 * Loads `apps/web/locales/<E2E_LOCALE>/<ns>.json` (the same files the app bundles, flat dotted keys).
 * Reading the catalog keeps specs in step with the real UI copy instead of hard-coding one locale's strings.
 */
function catalog(ns: string): Record<string, string> {
  let entries = catalogs.get(ns);
  if (!entries) {
    const file = join(__dirname, '..', '..', 'apps', 'web', 'locales', E2E_LOCALE, `${ns}.json`);
    entries = JSON.parse(readFileSync(file, 'utf8')) as Record<string, string>;
    catalogs.set(ns, entries);
  }
  return entries;
}

/** Escapes a literal string for use inside a RegExp source. */
export function escapeRe(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Owner-locale copy for `ns.key` (flat dotted key), with `{name}` placeholders replaced from `vars`.
 * Throws when the key is missing so a stale spec fails at the lookup, not as an unmatched locator.
 */
export function uiText(ns: string, key: string, vars: Record<string, string | number> = {}): string {
  const raw = catalog(ns)[key];
  if (raw === undefined) throw new Error(`missing ${ns}.${key} in the ${E2E_LOCALE} catalog`);
  return raw.replace(/\{(\w+)\}/g, (_, name: string) => String(vars[name] ?? `{${name}}`));
}

/**
 * Unanchored RegExp for a catalog string (a substring match, like Playwright's default name matching). Add `^`
 * yourself when a prefix match is wanted.
 */
export function uiRe(ns: string, key: string, flags?: string): RegExp {
  return new RegExp(escapeRe(uiText(ns, key)), flags);
}

/**
 * RegExp for a catalog string whose placeholders are variable: each `{name}` matches `fragments[name]` (a regex source,
 * default `.+?`). The result is anchored at both ends, so it matches the whole rendered label.
 */
export function uiPattern(ns: string, key: string, fragments: Record<string, string> = {}): RegExp {
  const raw = catalog(ns)[key];
  if (raw === undefined) throw new Error(`missing ${ns}.${key} in the ${E2E_LOCALE} catalog`);
  const parts = raw.split(/\{(\w+)\}/);
  // split() with a capture group alternates literal text (even index) and placeholder names (odd index).
  const source = parts.map((part, i) => (i % 2 === 0 ? escapeRe(part) : (fragments[part] ?? '.+?'))).join('');
  return new RegExp(`^${source}$`);
}
