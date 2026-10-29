import { describe, expect, it } from 'vitest';
import { removeUiPart, upsertUiPart } from './generative-ui-parts';

describe('removeUiPart', () => {
  it('drops a streamed partial widget by part_id and keeps siblings', () => {
    const parts = upsertUiPart(upsertUiPart([], { part_id: 'a', status: 'streaming' }), { part_id: 'b', status: 'streaming' });
    expect(removeUiPart(parts, 'a')).toEqual([{ part_id: 'b', status: 'streaming' }]);
  });

  it('returns the same array for an unknown id', () => {
    const parts = [{ part_id: 'a' }];
    expect(removeUiPart(parts, 'zzz')).toBe(parts);
  });
});
