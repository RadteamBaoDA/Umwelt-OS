import { describe, expect, it } from 'vitest';
import { flatCatalogs, jsonCatalogs, unflatten } from '@locales';

/** Sorted ICU argument names (`{count, plural, ...}` -> `count`) so translations may reorder but not drop placeholders. */
function placeholders(message: string): string[] {
  return [...new Set([...message.matchAll(/\{\s*(\w+)\s*[,}]/g)].map((m) => m[1]))].sort();
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
