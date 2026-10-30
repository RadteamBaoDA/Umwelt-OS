import type { DataShape } from '@/core/data-shapes';

const SHAPES: readonly DataShape[] = ['news', 'measurement', 'event', 'located', 'messages', 'video', 'project_activity', 'weather', 'series'];

/**
 * Reads the `?shapes=a,b&for=<renderer id>` filter that dashboard tiles link to (see `sourcesLinkFor`).
 *
 * @param params Current URL search params; null (no router) is treated as empty.
 * @returns Known shapes only (unknown strings are dropped, so an empty list means no filter) and the renderer id or null.
 */
export function parseShapeFilter(params: URLSearchParams | null): { shapes: DataShape[]; renderer: string | null } {
  const shapes = (params?.get('shapes') ?? '').split(',').filter((s): s is DataShape => (SHAPES as readonly string[]).includes(s));
  return { shapes, renderer: params?.get('for') || null };
}

/**
 * Whether an entry's declared shapes intersect the active filter; always true when no filter is active.
 *
 * @param entryShapes `data_shapes` of a built-in or connector catalog entry (may be absent on older payloads).
 * @param filter Active shape filter, or undefined/empty for none.
 */
export function matchesShapes(entryShapes: readonly string[] | undefined, filter: readonly DataShape[] | undefined): boolean {
  return !filter?.length || (entryShapes ?? []).some((s) => (filter as readonly string[]).includes(s));
}
