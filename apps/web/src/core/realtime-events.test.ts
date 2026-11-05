import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import {
  applyDashboardChange,
  applyKnowledgeChange,
  isCurrent,
  parseEnvelope,
  validCursor,
  validDashboardEvent,
  validFloorSequence,
  type KnowledgeEvent,
} from '@/core/realtime-events';

const ID = '11111111-1111-4111-8111-111111111111';
const ID2 = '22222222-2222-4222-8222-222222222222';

/** Builds a QueryClient whose invalidate/reset calls are recorded as query keys. */
function recordingClient() {
  const client = new QueryClient();
  const invalidated: unknown[] = [];
  const reset: unknown[] = [];
  vi.spyOn(client, 'invalidateQueries').mockImplementation(async (filters) => { invalidated.push(filters?.queryKey); });
  vi.spyOn(client, 'resetQueries').mockImplementation(async (filters) => { reset.push(filters?.queryKey); });
  return { client, invalidated, reset };
}

const base = (extra: Partial<KnowledgeEvent>): KnowledgeEvent => ({ schema_version: 1, deleted: false, ...extra });

describe('cursor helpers', () => {
  it('accepts only bounded uuid:sequence cursors and floor sequences', () => {
    expect(validCursor(`${ID}:5`)).toBe(true);
    expect(validCursor(`${ID}:05`)).toBe(false);
    expect(validCursor('nope')).toBe(false);
    expect(validFloorSequence('0')).toBe(true);
    expect(validFloorSequence('01')).toBe(false);
  });

  it('advances only within the same stream and compares numerically', () => {
    expect(isCurrent(`${ID}:10`, `${ID}:9`)).toBe(true);
    expect(isCurrent(`${ID}:9`, `${ID}:10`)).toBe(false);
    expect(isCurrent(`${ID2}:10`, `${ID}:9`)).toBe(false);
  });

  it('parses only schema_version 1 envelopes', () => {
    expect(parseEnvelope('{"schema_version":1,"a":1}')).toEqual({ schema_version: 1, a: 1 });
    expect(parseEnvelope('{"schema_version":2}')).toBeNull();
    expect(parseEnvelope('not json')).toBeNull();
  });
});

describe('applyKnowledgeChange', () => {
  const queue = vi.fn();

  it('rejects index events with stray scope fields and accepts a valid one', () => {
    const { client, invalidated } = recordingClient();
    const valid = base({
      scope: 'index', index_generation_id: ID, index_status: 'active', indexed_items: 3, failed_items: 0,
    });
    expect(applyKnowledgeChange(client, { ...valid, source_id: ID }, queue)).toBe(false);
    expect(applyKnowledgeChange(client, { ...valid, deleted: true }, queue)).toBe(false);
    expect(applyKnowledgeChange(client, { ...valid, indexed_items: -1 }, queue)).toBe(false);
    expect(invalidated).toEqual([]);
    expect(applyKnowledgeChange(client, valid, queue)).toBe(true);
    expect(invalidated).toEqual([['search-index']]);
  });

  it('requires exactly one of entity/relationship for graph events', () => {
    const { client, invalidated } = recordingClient();
    expect(applyKnowledgeChange(client, base({ scope: 'graph' }), queue)).toBe(false);
    expect(applyKnowledgeChange(client, base({ scope: 'graph', entity_id: ID, relationship_id: ID2 }), queue)).toBe(false);
    expect(applyKnowledgeChange(client, base({ scope: 'graph', entity_id: 'bad' }), queue)).toBe(false);
    expect(applyKnowledgeChange(client, base({ scope: 'graph', entity_id: ID }), queue)).toBe(true);
    expect(invalidated).toEqual([['entities'], ['relationships'], ['search']]);
  });

  it('validates timeline and timeline_collection scopes', () => {
    const { client, reset } = recordingClient();
    expect(applyKnowledgeChange(client, base({ scope: 'timeline', event_id: ID, event_revision: 0 }), queue)).toBe(false);
    expect(applyKnowledgeChange(client, base({ scope: 'timeline', event_id: ID, event_revision: 1 }), queue)).toBe(true);
    expect(applyKnowledgeChange(client, base({ scope: 'timeline_collection', source_id: ID, entity_id: ID2 }), queue)).toBe(false);
    expect(applyKnowledgeChange(client, base({ scope: 'timeline_collection', source_id: ID, deleted: true }), queue)).toBe(false);
    expect(applyKnowledgeChange(client, base({ scope: 'timeline_collection', source_id: ID }), queue)).toBe(true);
    expect(reset).toEqual([['intelligence-correlations'], ['intelligence-correlations']]);
  });

  it('queues the document for source-scoped events and resets observations on delete', () => {
    const { client, reset } = recordingClient();
    const queueDocument = vi.fn();
    expect(applyKnowledgeChange(client, base({ source_id: ID, document_id: ID2 }), queueDocument)).toBe(true);
    expect(queueDocument).toHaveBeenLastCalledWith(ID2);
    expect(reset).toEqual([['intelligence-correlations']]);
    expect(applyKnowledgeChange(client, base({ source_id: ID, deleted: true }), queueDocument)).toBe(true);
    expect(queueDocument).toHaveBeenLastCalledWith(null);
    expect(reset).toContainEqual(['world-observations']);
    expect(applyKnowledgeChange(client, base({ scope: 'source', source_id: 'bad' }), queueDocument)).toBe(false);
    expect(applyKnowledgeChange(client, base({ source_id: ID, entity_id: ID2 }), queueDocument)).toBe(false);
  });
});

describe('dashboard events', () => {
  it('rejects unknown keys, bad scope, ids and revisions', () => {
    const ok = { schema_version: 1, type: 'dashboard.changed', scope: 'dashboard', id: ID, revision: 1, deleted: false } as const;
    expect(validDashboardEvent(ok)).toBe(true);
    expect(validDashboardEvent({ ...ok, extra: 1 } as never)).toBe(false);
    expect(validDashboardEvent({ ...ok, scope: 'x' } as never)).toBe(false);
    expect(validDashboardEvent({ ...ok, id: 'bad' })).toBe(false);
    expect(validDashboardEvent({ ...ok, revision: 0 })).toBe(false);
    expect(validDashboardEvent(null)).toBe(false);
  });

  it('invalidates the family implied by each scope', () => {
    const { client, invalidated } = recordingClient();
    const event = { schema_version: 1, type: 'dashboard.changed', id: ID, revision: 1, deleted: false } as const;
    applyDashboardChange(client, { ...event, scope: 'brief' });
    applyDashboardChange(client, { ...event, scope: 'dashboard' });
    applyDashboardChange(client, { ...event, scope: 'definition' });
    expect(invalidated).toEqual([
      ['daily-context'], ['notifications'],
      ['dashboards'], ['dashboard', ID],
      ['gadget-definitions'], ['gadget-definition', ID], ['dashboard'],
    ]);
  });
});
