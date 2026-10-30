import { describe, expect, it } from 'vitest';
import { packPlacements } from './dashboard-page';

const p = (id: string, x: number, y: number, w: number, h: number) => ({ instance_id: id, x, y, w, h });

describe('packPlacements', () => {
  const input = [p('b', 10, 8, 5, 3), p('a', 2, 4, 6, 2), p('c', 0, 20, 20, 2)];
  const out = packPlacements(input, 20);
  it('removes holes', () => {
    expect(out.find((q) => q.instance_id === 'a')).toMatchObject({ x: 0, y: 0 });
    expect(out.find((q) => q.instance_id === 'b')).toMatchObject({ x: 6, y: 0 });
    expect(out.find((q) => q.instance_id === 'c')).toMatchObject({ x: 0, y: 3 });
  });
  it('preserves input order and sizes, stays in columns', () => {
    expect(out.map((q) => q.instance_id)).toEqual(['b', 'a', 'c']);
    out.forEach((q, i) => { expect([q.w, q.h]).toEqual([input[i].w, input[i].h]); expect(q.x + q.w).toBeLessThanOrEqual(20); });
  });
  it('is idempotent', () => expect(packPlacements(out, 20)).toEqual(out));
});
