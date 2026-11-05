import { describe, expect, it, vi } from 'vitest';
import { buildPaletteOptions } from './command-palette';

const t = ((key: string) => key) as never;
const tc = ((key: string, values?: { query?: string }) => (values?.query ? `${key}:${values.query}` : key)) as never;

/** Builds a minimal option context; individual tests override the parts they exercise. */
function context(overrides: Record<string, unknown> = {}) {
  return {
    query: '', scope: 'all', role: 'owner', availability: undefined, t, tc,
    close: vi.fn(), go: vi.fn(), setDraft: vi.fn(), openDrawer: vi.fn(), selectConversation: vi.fn(),
    wants: { docs: true, entities: true, events: true, conversations: true },
    docs: undefined, entities: undefined, events: undefined, conversations: undefined,
    ...overrides,
  } as Parameters<typeof buildPaletteOptions>[0];
}

describe('buildPaletteOptions', () => {
  it('offers quick actions when the query is empty in the All scope', () => {
    const ids = buildPaletteOptions(context()).map((option) => option.id);
    expect(ids).toEqual(expect.arrayContaining(['quick-chat', 'manage-sources', 'create-rule', 'collect-now']));
    expect(ids).not.toContain('ask-ai');
  });

  it('puts the query actions first and keeps collect-now inert', () => {
    const options = buildPaletteOptions(context({ query: 'zzzz-no-destination' }));
    expect(options.map((option) => option.id)).toEqual(['ask-ai', 'create-rule', 'collect-now']);
    expect(options[2].disabledReason).toBe('paletteCollectUnavailable');
    expect(options[0].label).toBe('paletteAskAi:zzzz-no-destination');
  });

  it('lists only search hits outside the All scope, in documents, entities, events, conversations order', () => {
    const go = vi.fn();
    const options = buildPaletteOptions(context({
      scope: 'entities', query: 'ab', go,
      entities: { items: [{ id: 'e/1', name: null, type: 'person' }] },
      docs: { items: [{ chunk_id: 'c', title: 'D', source: { name: 'S' }, version_number: 2, document_id: 'd', document_version_id: 'v' }] },
      wants: { docs: false, entities: true, events: false, conversations: false },
    }));
    expect(options).toHaveLength(1);
    expect(options[0]).toMatchObject({ id: 'ent-e/1', group: 'entities', label: 'e/1', badge: 'person' });
    options[0].run();
    expect(go).toHaveBeenCalledWith('/knowledge/entities/e%2F1');
  });

  it('selects the conversation before navigating to Chat', () => {
    const go = vi.fn();
    const selectConversation = vi.fn();
    const [option] = buildPaletteOptions(context({ scope: 'conversations', query: 'ab', go, selectConversation, wants: { docs: false, entities: false, events: false, conversations: true }, conversations: [{ id: 'k', title: '' }] }));
    expect(option.label).toBe('untitledConversation');
    option.run();
    expect(selectConversation).toHaveBeenCalledWith('k');
    expect(go).toHaveBeenCalledWith('/chat');
  });
});
