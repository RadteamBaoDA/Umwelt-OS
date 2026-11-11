import { describe, expect, it } from 'vitest';
import { markUiPartRebuilding, upsertUiPart } from './generative-ui-parts';

describe('markUiPartRebuilding', () => {
  it('keeps a discarded widget as a rebuilding streaming frame and leaves siblings alone', () => {
    const parts = upsertUiPart(upsertUiPart([], { part_id: 'a', status: 'completed' }), { part_id: 'b', status: 'streaming' });
    expect(markUiPartRebuilding(parts, 'a')).toEqual([
      { part_id: 'a', status: 'streaming', rebuilding: true },
      { part_id: 'b', status: 'streaming' },
    ]);
  });

  it('returns the same array for an unknown id', () => {
    const parts = [{ part_id: 'a' }];
    expect(markUiPartRebuilding(parts, 'zzz')).toBe(parts);
  });

  it('drops the rebuilding frame once a replacement snapshot arrives', () => {
    const rebuilding = markUiPartRebuilding([{ part_id: 'r1-a', status: 'completed' }], 'r1-a');
    expect(upsertUiPart(rebuilding, { part_id: 'r2-a', status: 'streaming' })).toEqual([{ part_id: 'r2-a', status: 'streaming' }]);
  });
});
