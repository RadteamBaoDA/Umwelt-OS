import { describe, expect, it } from 'vitest';
import type { DashboardPlacement, GadgetInstance } from './api';
import { findFreeCoordinates, isValidLayoutSnapshot, projectMobilePlacements, sameLayout } from './layout-geometry';

/** Builds a minimal instance with the given renderer (all renderers fall back to a 4x3 minimum when unknown). */
function inst(id: string, renderer = 'unknown_renderer'): GadgetInstance {
  return { id, definition: { renderer, source_ids: [] } } as unknown as GadgetInstance;
}
/** Builds a placement. */
function rect(id: string, x: number, y: number, w: number, h: number): DashboardPlacement {
  return { instance_id: id, x, y, w, h };
}

describe('layout geometry', () => {
  it('finds the first free slot and rejects invalid sizes', () => {
    expect(findFreeCoordinates([rect('a', 0, 0, 10, 3)], 10, 3)).toEqual({ x: 10, y: 0 });
    expect(findFreeCoordinates([rect('a', 0, 0, 20, 3)], 4, 3)).toEqual({ x: 0, y: 3 });
    expect(findFreeCoordinates([], 21, 3)).toBeNull();
    expect(findFreeCoordinates([], 4, 0)).toBeNull();
    expect(findFreeCoordinates([], 4, 3, 21)).toBeNull();
  });

  it('compares layouts regardless of order', () => {
    const a = [rect('a', 0, 0, 4, 3), rect('b', 4, 0, 4, 3)];
    expect(sameLayout(a, [a[1], a[0]])).toBe(true);
    expect(sameLayout(a, [a[0], rect('b', 4, 1, 4, 3)])).toBe(false);
    expect(sameLayout(a, [a[0]])).toBe(false);
  });

  it('validates snapshots for ownership, bounds, minimums and overlap', () => {
    const instances = [inst('a'), inst('b')];
    const good = [rect('a', 0, 0, 4, 3), rect('b', 4, 0, 4, 3)];
    expect(isValidLayoutSnapshot(good, instances, 20)).toBe(true);
    expect(isValidLayoutSnapshot(good, instances, 0)).toBe(false);
    expect(isValidLayoutSnapshot([good[0]], instances, 20)).toBe(false);
    expect(isValidLayoutSnapshot([good[0], rect('a', 4, 0, 4, 3)], instances, 20)).toBe(false);
    expect(isValidLayoutSnapshot([good[0], rect('b', 2, 0, 4, 3)], instances, 20)).toBe(false);
    expect(isValidLayoutSnapshot([good[0], rect('b', 4, 0, 3, 3)], instances, 20)).toBe(false);
    expect(isValidLayoutSnapshot([good[0], rect('b', 18, 0, 4, 3)], instances, 20)).toBe(false);
    expect(isValidLayoutSnapshot([good[0], rect('b', 4.5, 0, 4, 3)], instances, 20)).toBe(false);
    expect(isValidLayoutSnapshot([good[0], rect('c', 4, 0, 4, 3)], instances, 20)).toBe(false);
  });

  it('passes valid mobile layouts through unchanged and repairs malformed ones downward', () => {
    const instances = [inst('a'), inst('b')];
    const valid = [rect('a', 0, 0, 8, 3), rect('b', 0, 3, 8, 3)];
    expect(projectMobilePlacements(valid, instances, 8)).toEqual({ placements: valid, unavailable: false });
    const overlapping = [rect('a', 0, 0, 8, 3), rect('b', 0, 0, 8, 3)];
    expect(projectMobilePlacements(overlapping, instances, 8)).toEqual({
      placements: [rect('a', 0, 0, 8, 3), rect('b', 0, 3, 8, 3)],
      unavailable: false,
    });
    const malformed = [rect('a', Number.NaN, 0, 2, 1), rect('b', 0, 0, 8, 3)];
    const repaired = projectMobilePlacements(malformed, instances, 8);
    expect(repaired.unavailable).toBe(false);
    expect(repaired.placements.map((p) => p.instance_id)).toEqual(['a', 'b']);
    expect(repaired.placements[0]).toMatchObject({ x: 0, w: 4, h: 3 });
  });

  it('marks the projection unavailable on ownership or column mismatch', () => {
    const instances = [inst('a')];
    expect(projectMobilePlacements([], instances, 8).unavailable).toBe(true);
    expect(projectMobilePlacements([rect('a', 0, 0, 4, 3)], instances, 3).unavailable).toBe(true);
    expect(projectMobilePlacements([rect('a', 0, 0, 4, 3)], instances, 0).unavailable).toBe(true);
  });
});
