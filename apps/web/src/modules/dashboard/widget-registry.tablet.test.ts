import { describe, expect, it } from 'vitest';
import type { DashboardPlacement, GadgetInstance } from './api';
import { projectTabletPlacements } from './widget-registry';

const inst = (id: string): GadgetInstance => ({ id, definition: { renderer: 'text_panel' } } as unknown as GadgetInstance);
const pl = (id: string, x: number, y: number, w: number, h: number): DashboardPlacement =>
  ({ instance_id: id, x, y, w, h });

describe('projectTabletPlacements', () => {
  const instances = ['a', 'b', 'c', 'd'].map(inst);
  const out = projectTabletPlacements(
    [pl('d', 0, 30, 20, 10), pl('b', 10, 0, 6, 10), pl('a', 0, 0, 6, 10), pl('c', 0, 12, 14, 10)],
    instances, 30, 90, 56,
  )!;
  const get = (id: string) => out.find((p) => p.instance_id === id)!;

  it('keeps input order and flows in y,x order into two columns', () => {
    expect(out.map((p) => p.instance_id)).toEqual(['d', 'b', 'a', 'c']);
    expect(get('a')).toMatchObject({ x: 0, y: 0, w: 4 });
    expect(get('b')).toMatchObject({ x: 4, y: 0, w: 4 });
  });

  it('spans both columns when desktop width is at least 12', () => {
    expect(get('c')).toMatchObject({ x: 0, w: 8 });
    expect(get('d')).toMatchObject({ x: 0, w: 8 });
    expect(get('c').y).toBeGreaterThanOrEqual(get('a').y + get('a').h);
    expect(get('d').y).toBeGreaterThanOrEqual(get('c').y + get('c').h);
  });

  it('returns null when unmeasured', () => {
    expect(projectTabletPlacements([pl('a', 0, 0, 6, 4)], [inst('a')], 0, 90, 56)).toBeNull();
  });
});
