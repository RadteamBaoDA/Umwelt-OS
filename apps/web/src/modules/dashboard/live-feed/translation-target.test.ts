import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { isNewsEvent, liveEventTarget, SUMMARY_MAX, TITLE_MAX } from './translation-target';
import type { StoredEvent } from './store';

const event = (over: Partial<StoredEvent> = {}): StoredEvent =>
  ({
    id: '1',
    key: 'bbc_world:1',
    provider: 'bbc_world',
    tags: [],
    seen_at: '',
    title: 'Hello',
    summary: 'World',
    source: 'BBC',
    observed_at: null,
    ...over,
  }) as StoredEvent;
const sha = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');

describe('liveEventTarget', () => {
  it('formats a stable 8-4-4-4-12 id and the server revision vector', async () => {
    const a = await liveEventTarget(event());
    expect(a.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    expect((await liveEventTarget(event())).id).toBe(a.id);
    // Same vector the backend asserts: live_event_revision("Hello", "World") == sha256("Hello\nWorld").
    expect(a.revision).toBe(sha('Hello\nWorld'));
    expect(a.source).toEqual({ title: 'Hello', summary: 'World' });
  });

  it('uses a null summary and clips to the server bounds before hashing', async () => {
    const none = await liveEventTarget(event({ summary: null }));
    expect(none.source?.summary).toBeNull();
    expect(none.revision).toBe(sha('Hello\n'));
    const long = await liveEventTarget(event({ title: 'a'.repeat(TITLE_MAX + 5), summary: 'b'.repeat(SUMMARY_MAX + 5) }));
    expect(long.source?.title).toHaveLength(TITLE_MAX);
    expect(long.source?.summary).toHaveLength(SUMMARY_MAX);
    expect(long.revision).toBe(sha(`${'a'.repeat(TITLE_MAX)}\n${'b'.repeat(SUMMARY_MAX)}`));
  });

  it('flags only NEWS-kind events', () => {
    expect(isNewsEvent(event())).toBe(true);
    expect(isNewsEvent(event({ provider: 'usgs', source: 'USGS', tags: ['earthquake'] }))).toBe(false);
  });
});
