/**
 * Flat-to-nested catalog expansion, kept free of any catalog import so bundles that need only a few namespaces
 * (see `widget-catalogs.ts`) do not pull in the whole `index.ts` catalog set.
 */

type SetPath<P extends string, V> = P extends `${infer Head}.${infer Rest}` ? { [K in Head]: SetPath<Rest, V> } : { [K in P]: V };

type UnionToIntersection<U> = (U extends unknown ? (x: U) => void : never) extends (x: infer I) => void ? I : never;

/**
 * Nested message tree implied by a flat catalog's dotted keys, so `t('group.key')` type-checks against it.
 * @typeParam F Flat catalog type (the imported JSON), whose keys are dotted paths.
 */
export type NestedCatalog<F> = UnionToIntersection<{ [K in keyof F & string]: SetPath<K, F[K]> }[keyof F & string]>;

/** Own-property check; `in` would also match inherited names such as `constructor`. */
function hasOwn(node: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(node, key);
}

/**
 * Expands a flat catalog with dotted keys into the nested tree next-intl consumes.
 * `{ 'composer.send': 'Send' }` becomes `{ composer: { send: 'Send' } }`. Pure: returns a new object and
 * never mutates the input. Key order follows the input order.
 *
 * Throws instead of guessing when the flat catalog cannot be represented as a tree: an empty path segment
 * (`'a..b'`, `'.a'`, `'a.'`) or a collision where one key is a leaf and another key passes through it
 * (`'a'` together with `'a.b'`). Such a catalog would silently drop a message at runtime, so it fails at
 * module load instead.
 *
 * @param flat Flat catalog whose values are message strings.
 * @returns Nested catalog with the same leaves, typed by {@link NestedCatalog}.
 * @throws Error on a malformed key or a leaf/group collision; the message names the offending key.
 */
export function unflatten<F extends Record<string, string>>(flat: F): NestedCatalog<F> {
  const root: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(flat)) {
    const parts = key.split('.');
    if (parts.some((part) => part === '')) throw new Error(`malformed message key: "${key}"`);
    const leaf = parts.pop() as string;
    let node = root;
    for (const part of parts) {
      if (!hasOwn(node, part)) node[part] = {};
      const next = node[part];
      if (typeof next === 'string') throw new Error(`message key collision at "${key}": "${part}" is also a leaf`);
      node = next as Record<string, unknown>;
    }
    if (hasOwn(node, leaf)) throw new Error(`message key collision at "${key}"`);
    node[leaf] = value;
  }
  return root as NestedCatalog<F>;
}
