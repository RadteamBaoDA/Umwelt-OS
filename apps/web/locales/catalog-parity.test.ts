import { describe, expect, it } from 'vitest';
import { flatCatalogs, jsonCatalogs, unflatten } from '@locales';

/**
 * Sorted ICU argument names (`{count, plural, ...}` -> `count`) so translations may reorder but not drop placeholders.
 * Plural/select branch bodies (`one {source}`) are removed first: their words are literal text that may legitimately
 * differ per language (Vietnamese has only the `other` branch).
 */
function placeholders(message: string): string[] {
  const withoutBranches = message.replace(/\b(?:zero|one|two|few|many|other|=\d+)\s*\{[^{}]*\}/g, '');
  return [...new Set([...withoutBranches.matchAll(/\{\s*(\w+)\s*[,}]/g)].map((m) => m[1]))].sort();
}

describe.each(Object.keys(flatCatalogs['en-us']) as (keyof (typeof flatCatalogs)['en-us'])[])('catalog parity: %s', (ns) => {
  const en = flatCatalogs['en-us'][ns] as Record<string, string>;
  const vi = flatCatalogs['vi-vi'][ns] as Record<string, string>;

  it('has the same flat key set in en-us and vi-vi', () => {
    expect(Object.keys(vi).sort()).toEqual(Object.keys(en).sort());
  });

  it('uses the same {placeholder} names in every message', () => {
    for (const [key, value] of Object.entries(en)) {
      expect(placeholders(vi[key] ?? ''), key).toEqual(placeholders(value));
    }
  });

  it('expands to a nested tree with no key collision', () => {
    expect(() => unflatten(en)).not.toThrow();
    expect(() => unflatten(vi)).not.toThrow();
    expect(jsonCatalogs['en-us'][ns]).toEqual(unflatten(en));
    expect(jsonCatalogs['vi-vi'][ns]).toEqual(unflatten(vi));
  });
});
