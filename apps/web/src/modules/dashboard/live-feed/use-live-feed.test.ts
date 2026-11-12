import { describe, expect, it } from 'vitest';
import { liveErrorKey, mergeEvents, snapshotProviders } from './use-live-feed';
import type { StoredEvent } from './store';

const row = (key: string, title: string) => ({ key, title }) as StoredEvent;

describe('snapshotProviders', () => {
  it('excludes providers that failed to load or returned a truncated response', () => {
    const results = [{ data: { truncated: false } }, { data: { truncated: true } }, {}, { data: {} }];
    expect(snapshotProviders(['a', 'b', 'c', 'd'], results)).toEqual(['a', 'd']);
  });
});

describe('mergeEvents', () => {
  it('lets later lists win per key (fresh proxy rows over memory over stored)', () => {
    const merged = mergeEvents([row('x', 'stored'), row('y', 'stored')], [row('x', 'memory')], [row('x', 'fresh')]);
    expect(merged.find((item) => item.key === 'x')?.title).toBe('fresh');
    expect(merged).toHaveLength(2);
  });
});

describe('liveErrorKey', () => {
  it('maps the worker storage failure to its own key, not a source error', () => {
    expect(liveErrorKey('storage', 'usgs')).toBe('liveMapStorageError');
    expect(liveErrorKey('network', 'usgs')).toBe('liveMapSourceError');
  });
});
