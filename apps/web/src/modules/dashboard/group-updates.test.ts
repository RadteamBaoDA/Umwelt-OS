import { describe, expect, it } from 'vitest';
import { computeUpdatedGroupIds } from './group-updates';

const inst = (group_id: string, ids: string[]) => ({ group_id, definition: { source_ids: ids } }) as never;
const src = (id: string, at: string | null) => ({ id, last_success_at: at }) as never;

describe('computeUpdatedGroupIds', () => {
  it('flags groups with a source newer than the last visit', () => {
    const out = computeUpdatedGroupIds(
      [inst('g1', ['s1']), inst('g2', ['s2']), inst('g3', ['s1'])],
      [src('s1', '2026-01-02T00:00:00Z'), src('s2', '2026-01-01T00:00:00Z')],
      { g1: '2026-01-01T12:00:00Z', g2: '2026-01-01T12:00:00Z', g3: null },
      'none',
    );
    expect([...out]).toEqual(['g1']);
  });

  it('never flags the active group or a group without a stored visit', () => {
    const out = computeUpdatedGroupIds([inst('g1', ['s1'])], [src('s1', '2026-01-02T00:00:00Z')], { g1: '2026-01-01T00:00:00Z' }, 'g1');
    expect(out.size).toBe(0);
  });
});
