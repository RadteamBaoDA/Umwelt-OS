import { describe, expect, it, vi } from 'vitest';
import { LIVE_FEED_ORIGINS, buildAppCsp } from '@/core/csp';
import { FIXTURES } from './fixtures';
import { fetchSource, nextDelayMs, refreshable, REFRESH_MIN_GAP_MS } from './runner';
import { BROWSER_SOURCES, BROWSER_SOURCE_BY_ID, MAX_ITEMS, toIso } from './sources';
import { filterEvents, pruneKeys, toStored, type StoredEvent } from './store';

describe('browser source parsers', () => {
  it.each(BROWSER_SOURCES.map((source) => source.id))('%s parses its fixture into safe events', (id) => {
    const events = BROWSER_SOURCE_BY_ID[id].parse(FIXTURES[id]);
    expect(events.length).toBeGreaterThan(0);
    for (const event of events) {
      expect(event.title.length).toBeLessThanOrEqual(500);
      expect(event.tags?.length).toBeGreaterThan(0);
      expect(event.url === null || /^https?:/.test(event.url)).toBe(true);
    }
  });
  it('keeps coordinates in lon/lat order and adds native types as tags', () => {
    const [quake] = BROWSER_SOURCE_BY_ID.usgs.parse(FIXTURES.usgs);
    expect(quake).toMatchObject({ id: 'us7000txqr', latitude: -59.03, longitude: -26.03 });
    expect(quake.tags).toContain('earthquake');
    expect(BROWSER_SOURCE_BY_ID.gdacs.parse(FIXTURES.gdacs)[0].tags).toEqual(['geo', 'disaster', 'eq', 'green']);
    expect(BROWSER_SOURCE_BY_ID.launches.parse(FIXTURES.launches)[0]).toMatchObject({
      latitude: 40.960482,
      longitude: 100.298059,
      url: 'https://spacelaunchnow.app/launch/long-march-2d-cas-pathfinder-1/',
    });
  });
  it('drops unsafe source links and uses the latest EONET point', () => {
    const [eonet] = BROWSER_SOURCE_BY_ID.eonet.parse(FIXTURES.eonet);
    expect(eonet.sources).toHaveLength(1);
    expect(eonet).toMatchObject({ longitude: 52.3, observed_at: '2026-11-08T00:00:00.000Z' });
    expect(eonet.tags).toContain('severestorms');
  });
  it('places NWS alerts at the polygon centroid and treats zone-less dates as UTC', () => {
    const [alert] = BROWSER_SOURCE_BY_ID.nws_alerts.parse(FIXTURES.nws_alerts);
    expect(alert.latitude).toBeGreaterThan(36.9);
    expect(toIso('2026-11-12T03:45:56')).toBe('2026-11-12T03:45:56.000Z');
    expect(toIso('2026-11-10 13:29:13')).toBe('2026-11-10T13:29:13.000Z');
    expect(toIso('nope')).toBeNull();
  });
  it('returns nothing for malformed bodies and caps item count and title length', () => {
    for (const source of BROWSER_SOURCES) expect(source.parse({ unexpected: true })).toEqual([]);
    const hits = Array.from({ length: 500 }, (_, i) => ({ objectID: String(i), title: 'x'.repeat(900), url: 'https://e.com' }));
    const parsed = BROWSER_SOURCE_BY_ID.hackernews.parse({ hits });
    expect(parsed).toHaveLength(MAX_ITEMS);
    expect(parsed[0].title).toHaveLength(500);
  });
  it('requires a safe GitHub scope', () => {
    const github = BROWSER_SOURCE_BY_ID.github_releases;
    expect(github.url({}, 0)).toBeNull();
    expect(github.url({ live_map_github_owner: '../x', live_map_github_repo: 'r' }, 0)).toBeNull();
    expect(github.url({ live_map_github_owner: 'vercel', live_map_github_repo: 'next.js' }, 0)).toContain('/repos/vercel/next.js/releases');
  });
});

describe('csp', () => {
  it('allows exactly the origins of the browser sources', () => {
    const origins = new Set(
      BROWSER_SOURCES.map((source) => new URL(source.url({ live_map_github_owner: 'a', live_map_github_repo: 'b' }, 0)!).origin),
    );
    expect([...origins].sort()).toEqual([...LIVE_FEED_ORIGINS].sort());
    expect(buildAppCsp('n', false)).toContain(`connect-src 'self' ${LIVE_FEED_ORIGINS.join(' ')}`);
  });
});

describe('store helpers', () => {
  const now = Date.parse('2026-11-12T00:00:00Z');
  const row = (key: string, observed_at: string | null, tags = ['geo'], provider = 'usgs'): StoredEvent => ({
    ...toStored(
      provider,
      tags,
      {
        id: key,
        category: 'geo',
        title: `Quake ${key}`,
        observed_at,
        latitude: null,
        longitude: null,
        url: null,
        source: 'USGS',
        summary: null,
      },
      new Date(now).toISOString(),
    ),
    key,
  });
  it('prunes rows past retention and beyond the cap, oldest first', () => {
    const rows = [row('a', '2026-11-11T00:00:00Z'), row('b', '2026-11-10T00:00:00Z'), row('old', '2026-09-01T00:00:00Z')];
    expect(pruneKeys(rows, now, 1).sort()).toEqual(['b', 'old']);
  });
  it('filters by provider, period, any-of tags and text, newest first', () => {
    const rows = [
      row('a', '2026-11-11T00:00:00Z', ['geo']),
      row('b', '2026-11-11T12:00:00Z', ['weather']),
      row('c', '2026-10-01T00:00:00Z', ['geo']),
      row('d', null, ['geo'], 'hackernews'),
    ];
    const base = { tags: [], days: 7, query: '', providers: ['usgs'] };
    expect(filterEvents(rows, base, now).map((r) => r.key)).toEqual(['b', 'a']);
    expect(filterEvents(rows, { ...base, tags: ['weather'] }, now).map((r) => r.key)).toEqual(['b']);
    expect(filterEvents(rows, { ...base, query: 'QUAKE b' }, now).map((r) => r.key)).toEqual(['b']);
    expect(filterEvents(rows, { ...base, query: 'nothing' }, now)).toEqual([]);
    expect(filterEvents(rows, { ...base, providers: ['hackernews'] }, now).map((r) => r.key)).toEqual(['d']);
  });
});

describe('runner', () => {
  it('backs off exponentially up to one hour', () => {
    expect(nextDelayMs(60_000, 0)).toBe(60_000);
    expect(nextDelayMs(60_000, 3)).toBe(480_000);
    expect(nextDelayMs(60_000, 20)).toBe(3_600_000);
  });
  it('fetches anonymously and reports failures as short codes', async () => {
    const as = (fn: unknown) => fn as typeof fetch;
    const ok = vi.fn(async () => new Response(JSON.stringify(FIXTURES.hackernews)));
    expect(await fetchSource('hackernews', {}, 0, as(ok))).toHaveLength(2);
    expect(ok).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ credentials: 'omit', referrerPolicy: 'no-referrer' }));
    await expect(
      fetchSource(
        'hackernews',
        {},
        0,
        as(async () => new Response('', { status: 429 })),
      ),
    ).rejects.toThrow('http_429');
    await expect(fetchSource('github_releases', {}, 0, as(ok))).rejects.toThrow('scope');
    await expect(
      fetchSource(
        'hackernews',
        {},
        0,
        as(async () => new Response('<html>')),
      ),
    ).rejects.toThrow('parse');
    await expect(
      fetchSource(
        'hackernews',
        {},
        0,
        as(async () => new Response('x', { headers: { 'content-length': String(6 * 1024 * 1024) } })),
      ),
    ).rejects.toThrow('too_large');
  });
});

describe('manual refresh', () => {
  it('skips providers in flight or fetched within the minimum gap', () => {
    const now = 1_000_000;
    const lastRun = new Map([
      ['usgs', now - 1_000],
      ['eonet', now - REFRESH_MIN_GAP_MS],
    ]);
    expect(refreshable(['usgs', 'eonet', 'gdacs', 'nvd'], new Set(['nvd']), lastRun, now)).toEqual(['eonet', 'gdacs']);
  });
});
