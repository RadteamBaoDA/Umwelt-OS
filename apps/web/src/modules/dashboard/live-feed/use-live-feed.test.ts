import { describe, expect, it } from 'vitest';
import { ApiError } from '@/core/api';
import { liveErrorKey, mergeEvents, PROXIED_SNAPSHOT, proxiedStatus, proxyIncluded, snapshotProviders } from './use-live-feed';
import type { StoredEvent } from './store';

const row = (key: string, title: string) => ({ key, title }) as StoredEvent;

describe('snapshotProviders', () => {
  it('excludes providers that failed, were not written, or returned a truncated response', () => {
    const results = [
      { data: { truncated: false } },
      { data: { truncated: true } },
      {},
      { data: {} },
      { isError: true, data: {} },
      { data: {} },
    ];
    const included = [true, true, true, true, true, false];
    const allSnapshot = { a: true, b: true, c: true, d: true, e: true, f: true };
    expect(snapshotProviders(['a', 'b', 'c', 'd', 'e', 'f'], results, included, allSnapshot)).toEqual(['a', 'd']);
  });
  it('prunes nothing for proxied providers unless they are flagged as current-set snapshots', () => {
    const complete = [{ data: { truncated: false } }, { data: { truncated: false } }];
    // Default map: every backend provider is a rolling window or slice, so a complete response never prunes.
    expect(snapshotProviders(['lobsters', 'cisa_kev'], complete, [true, true])).toEqual([]);
    expect(Object.values(PROXIED_SNAPSHOT).every((flag) => flag === false)).toBe(true);
    // Only an explicitly true provider that is included, complete and not errored is pruned.
    expect(snapshotProviders(['x', 'y'], complete, [true, true], { x: true })).toEqual(['x']);
    // Unknown ids (not in the map) default to false.
    expect(snapshotProviders(['unknown'], complete, [true], {})).toEqual([]);
  });
});

describe('proxyIncluded', () => {
  it('drops a 409 from the store write even when stale data is retained', () => {
    expect(proxyIncluded({ error: new ApiError(409, 'conflict') }, true, true)).toBe(false);
    expect(proxyIncluded({ error: new ApiError(500, 'server') }, true, true)).toBe(true);
  });
  it('requires owner access and accepted terms', () => {
    expect(proxyIncluded({ error: null }, false, true)).toBe(false);
    expect(proxyIncluded({ error: null }, true, false)).toBe(false);
    expect(proxyIncluded({ error: null }, true, true)).toBe(true);
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

describe('proxiedStatus', () => {
  const data = { items: [], fetched_at: '2026-11-10T00:00:00Z', truncated: false };
  it('marks old rows stale when a refetch failed but data is retained', () => {
    expect(proxiedStatus({ isError: true, error: new Error('x'), data }, 'usgs', true)).toEqual({
      error: 'liveMapSourceError',
      staleSince: data.fetched_at,
    });
  });
  it('has no stale marker without retained data, and reports fresh data plainly', () => {
    expect(proxiedStatus({ isError: true, error: new Error('x') }, 'usgs', true)?.staleSince).toBeUndefined();
    expect(proxiedStatus({ isError: false, error: null, data }, 'usgs', true)).toEqual({ fetchedAt: data.fetched_at, truncated: false });
  });
});
