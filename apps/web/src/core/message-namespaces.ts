/**
 * Namespaces rendered outside the authenticated workspace (public pages, root error/not-found pages, the route loader).
 * The root layout sends only these; app/(workspace)/layout.tsx sends the full catalog. Derived from the import graph of
 * app/(public), app/(redirects) and app/*.tsx; add a namespace here when a public page starts using it.
 */
export const PUBLIC_NAMESPACES = [
  'account',
  'accountDeletion',
  'common',
  'landing',
  'login',
  'notifications',
  'pick',
  'preferences',
  'routeErrors',
  'setup',
  'shell',
  'workspaceMembers',
  'workspaces',
] as const;

/**
 * Return a catalog holding only `names` (missing names are skipped).
 * @param catalog Full message catalog of one locale.
 * @param names Top-level namespaces to keep.
 * @returns A shallow copy with the selected namespaces.
 */
export function pickNamespaces<T extends Record<string, unknown>>(catalog: T, names: readonly string[]): T {
  return Object.fromEntries(names.filter((name) => name in catalog).map((name) => [name, catalog[name]])) as T;
}
