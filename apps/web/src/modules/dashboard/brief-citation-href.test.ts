import { describe, expect, it } from 'vitest';
import { briefCitationHref } from './brief-citation-href';

describe('briefCitationHref', () => {
  it('links stories, events and source health', () => {
    expect(briefCitationHref('stories', 's1')).toBe('/news?story=s1');
    expect(briefCitationHref('events', 'e1')).toBe('/timeline?event_id=e1');
    expect(briefCitationHref('source_health', 'x')).toBe('/settings/sources');
  });

  it('leaves kinds without a page as text', () => {
    for (const kind of ['markets', 'weather', 'hazards', 'tasks', 'goals']) expect(briefCitationHref(kind, 'x')).toBeNull();
  });

  it('cuts long ids and encodes unsafe characters', () => {
    expect(briefCitationHref('stories', 'a'.repeat(80))).toBe(`/news?story=${'a'.repeat(64)}`);
    expect(briefCitationHref('stories', 'a&b')).toBe('/news?story=a%26b');
  });
});
