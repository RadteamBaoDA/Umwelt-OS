import { describe, expect, it } from 'vitest';
import { jsonCatalogs } from '@messages';

type Tree = { [key: string]: string | Tree };

/** Flattens a nested catalog to `{ 'group.key': value }`. */
function flatten(tree: Tree, prefix = ''): Record<string, string> {
  return Object.entries(tree).reduce<Record<string, string>>((acc, [key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === 'string' ? { ...acc, [path]: value } : { ...acc, ...flatten(value, path) };
  }, {});
}

/** Sorted ICU argument names (`{count, plural, ...}` -> `count`) so translations may reorder but not drop placeholders. */
function placeholders(message: string): string[] {
  return [...new Set([...message.matchAll(/\{\s*(\w+)\s*[,}]/g)].map((m) => m[1]))].sort();
}

describe.each(Object.keys(jsonCatalogs['en-us']) as (keyof (typeof jsonCatalogs)['en-us'])[])('catalog parity: %s', (ns) => {
  const en = flatten(jsonCatalogs['en-us'][ns] as Tree);
  const vi = flatten(jsonCatalogs['vi-vi'][ns] as Tree);

  it('has the same key tree in en-us and vi-vi', () => {
    expect(Object.keys(vi).sort()).toEqual(Object.keys(en).sort());
  });

  it('uses the same {placeholder} names in every message', () => {
    for (const [key, value] of Object.entries(en)) expect(placeholders(vi[key] ?? ''), key).toEqual(placeholders(value));
  });
});
