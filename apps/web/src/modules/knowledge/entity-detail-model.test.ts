import { describe, expect, it } from 'vitest';
import { ApiError } from '@/core/api';
import type { Entity } from './api';
import { conflictDetails } from './entity-detail-schemas';
import { matchingMergePreview, matchingSplitPreview, pagesOf } from './entity-detail-model';

const entity = { id: 'e1', type: 'person', name: 'A', revision: 3 } as unknown as Entity;
const target = { id: 'e2', type: 'person', name: 'B', revision: 7 } as unknown as Entity;

describe('entity detail model helpers', () => {
  it('flattens loaded pages and tolerates missing data', () => {
    expect(pagesOf(undefined, (page: { items: number[] }) => page.items)).toEqual([]);
    expect(pagesOf({ pages: [{ items: [1] }, { items: [2, 3] }] }, (page) => page.items)).toEqual([1, 2, 3]);
  });

  it('keeps a merge preview only while revisions, target and reason match', () => {
    const preview = { preview: {}, intoId: 'e2', intoRevision: 7, targetName: 'B', sourceRevision: 3, reason: 'why' } as never;
    expect(matchingMergePreview(preview, entity, target, ' why ')).toBe(preview);
    expect(matchingMergePreview(preview, { ...entity, revision: 4 }, target, 'why')).toBeNull();
    expect(matchingMergePreview(preview, entity, { ...target, revision: 8 }, 'why')).toBeNull();
    expect(matchingMergePreview(preview, entity, null, 'why')).toBeNull();
    expect(matchingMergePreview(undefined, entity, target, 'why')).toBeNull();
  });

  it('keeps a split preview only while revision, name, reason and selection match', () => {
    const preview = { preview: {}, payload: { evidence_ids: ['a', 'b'], expected_revision: 3, new_entity: { type: 'person', name: 'N', reason: 'r' }, reason: 'r' } } as never;
    expect(matchingSplitPreview(preview, 3, ['b', 'a'], ' N ', 'r')).toBe(preview);
    expect(matchingSplitPreview(preview, 4, ['a', 'b'], 'N', 'r')).toBeNull();
    expect(matchingSplitPreview(preview, 3, ['a'], 'N', 'r')).toBeNull();
    expect(matchingSplitPreview(preview, 3, ['a', 'b'], 'M', 'r')).toBeNull();
    expect(matchingSplitPreview(undefined, 3, [], 'N', 'r')).toBeNull();
  });

  it('formats only bounded 409 conflict details', () => {
    expect(conflictDetails(new Error('x'))).toEqual([]);
    const error = new ApiError(409, 'conflict', { details: { conflicts: [{ code: 'merge', message: 'blocked', entity_ids: ['x', 'y'] }] } });
    expect(conflictDetails(error)).toEqual(['merge: blocked', 'entity_ids: x, y']);
  });
});
