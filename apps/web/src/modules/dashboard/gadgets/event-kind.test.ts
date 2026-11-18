import { describe, expect, it } from 'vitest';
import { eventKind } from './event-kind';

describe('eventKind', () => {
  it('maps earthquakes from the tag or the usgs provider to QUAKE', () => {
    expect(eventKind({ tags: ['geo', 'disaster', 'earthquake'], provider: 'usgs' })).toEqual({ label: 'QUAKE', tone: 'var(--danger)' });
    expect(eventKind({ tags: ['geo', 'disaster'], provider: 'usgs' }).label).toBe('QUAKE');
    expect(eventKind({ tags: ['geo', 'disaster', 'eq'], provider: 'gdacs' }).label).toBe('QUAKE');
  });

  it('maps EONET categories and fire / launch providers', () => {
    expect(eventKind({ tags: ['geo', 'disaster', 'wildfires'], provider: 'eonet' }).label).toBe('FIRE');
    expect(eventKind({ tags: ['geo', 'disaster', 'severestorms'], provider: 'eonet' }).label).toBe('STORM');
    expect(eventKind({ tags: ['geo', 'disaster', 'volcanoes'], provider: 'eonet' }).label).toBe('VOLC');
    expect(eventKind({ tags: ['geo', 'disaster'], provider: 'firms' })).toEqual({ label: 'FIRE', tone: 'var(--status-warning)' });
    expect(eventKind({ tags: ['space', 'launch'], provider: 'launches' })).toEqual({ label: 'LAUNCH', tone: 'var(--status-success)' });
  });

  it('labels news providers NEWS and falls back to a short tag', () => {
    expect(eventKind({ tags: ['world', 'politics'], provider: 'bbc_world' }).label).toBe('NEWS');
    expect(eventKind({ tags: ['mystery-category'], provider: 'custom' })).toEqual({ label: 'MYSTER', tone: 'var(--muted)' });
    expect(eventKind({ tags: [], provider: 'ab' }).label).toBe('AB');
  });
});
