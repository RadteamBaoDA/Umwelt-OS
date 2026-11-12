import { z } from 'zod';
import { safeLiveLink, type LiveEvent } from '../live-map-data';

/** Tag vocabulary offered as filter chips; provider-native types (e.g. GDACS `eq`) are searchable but not chips. */
export const LIVE_TAGS = [
  'geo',
  'disaster',
  'weather',
  'technology',
  'security',
  'finance',
  'crypto',
  'economy',
  'space',
  'gaming',
  'politics',
  'world',
] as const;

/** Provider-scoped values a browser source may use to build its URL. */
export type LiveFeedScope = { live_map_github_owner?: string | null; live_map_github_repo?: string | null };

/** One CORS-capable public source fetched directly by the worker. */
export type BrowserSource = {
  id: string;
  name: string;
  home: string;
  tags: string[];
  intervalMs: number;
  /**
   * True when each fetch is the complete current set for the provider, so stored rows it no longer returns are
   * expired and may be deleted. False for windows, top-N lists and catalog slices, whose absence means nothing.
   */
  snapshot: boolean;
  /** Request URL, or null when the scope is incomplete (the source is then skipped with a scope error). */
  url: (scope: LiveFeedScope, now: number) => string | null;
  /**
   * Turn a parsed JSON body into bounded events. Malformed items are dropped. Snapshot sources also throw when the
   * top-level list is missing or not an array, or when a non-empty list has no valid item, so schema drift is never
   * mistaken for an empty current set.
   */
  parse: (json: unknown) => LiveEvent[];
};

/** Maximum events kept from one fetch. */
export const MAX_ITEMS = 200;
const TITLE_MAX = 500;
const SUMMARY_MAX = 500;

const num = z.coerce.number().refine(Number.isFinite);
const str = z.string();
const optStr = z.string().nullish();

/**
 * Parse a provider timestamp into canonical ISO, treating zone-less strings (GDACS, GamerPower) as UTC.
 * @param value Epoch milliseconds or a date string.
 * @returns ISO string, or null for missing/invalid input.
 */
export function toIso(value: unknown): string | null {
  if (typeof value === 'number') return Number.isFinite(value) ? new Date(value).toISOString() : null;
  if (typeof value !== 'string' || !value) return null;
  // A date-only string (`2026-11-10`) has no time part to append Z to; anchor it at UTC midnight.
  const dateOnly = /^\d{4}-\d\d-\d\d$/.test(value);
  const normalized = dateOnly ? `${value}T00:00:00Z` : /(?:Z|[+-]\d\d:?\d\d)$/.test(value) ? value : `${value.replace(' ', 'T')}Z`;
  const time = Date.parse(normalized);
  return Number.isFinite(time) ? new Date(time).toISOString() : null;
}

/** Collapse whitespace and truncate provider text to a bounded length; empty text becomes null. */
function clip(value: string | null | undefined, max: number): string | null {
  const text = (value ?? '').replace(/\s+/g, ' ').trim();
  return text ? text.slice(0, max) : null;
}

/** Normalize a tag list: lower-cased, de-duplicated, empty values dropped, bounded. */
function tagsOf(tags: (string | null | undefined)[]): string[] {
  return [...new Set(tags.map((tag) => (tag ?? '').trim().toLowerCase()).filter(Boolean))].slice(0, 12);
}

/** Input of `event`; id, title, tags and source are required, the rest is optional provider fact. */
type EventInput = {
  id: string | number;
  title: string;
  tags: (string | null | undefined)[];
  source: string;
  observed_at?: unknown;
  lat?: number | null;
  lon?: number | null;
  url?: string | null;
  summary?: string | null;
  sources?: { id: string | null; url: string }[];
};

/**
 * Build one bounded LiveEvent. Only http(s) links survive (`safeLiveLink`); coordinates are kept only when finite.
 * @returns The event, or null when the title is empty.
 */
function event(input: EventInput): LiveEvent | null {
  const title = clip(input.title, TITLE_MAX);
  if (!title) return null;
  const tags = tagsOf(input.tags);
  const sources = input.sources?.filter((item) => safeLiveLink(item.url));
  return {
    id: String(input.id),
    category: tags[0] ?? 'geo',
    title,
    observed_at: toIso(input.observed_at),
    latitude: typeof input.lat === 'number' && Number.isFinite(input.lat) ? input.lat : null,
    longitude: typeof input.lon === 'number' && Number.isFinite(input.lon) ? input.lon : null,
    url: safeLiveLink(input.url ?? null),
    source: input.source,
    summary: clip(input.summary, SUMMARY_MAX),
    ...(sources?.length ? { sources } : {}),
    tags,
  };
}

/** Validate each array item with `schema`, drop failures, map the survivors, cap at MAX_ITEMS. */
function mapItems<S extends z.ZodType>(list: unknown, schema: S, build: (item: z.infer<S>) => LiveEvent | null): LiveEvent[] {
  if (!Array.isArray(list)) return [];
  const out: LiveEvent[] = [];
  for (const raw of list) {
    const parsed = schema.safeParse(raw);
    const built = parsed.success ? build(parsed.data) : null;
    if (built) out.push(built);
    if (out.length >= MAX_ITEMS) break;
  }
  return out;
}

/** Read `json[key]` when `json` is an object. */
function field(json: unknown, key: string): unknown {
  return json && typeof json === 'object' ? (json as Record<string, unknown>)[key] : undefined;
}

/**
 * Require the top-level item list of a snapshot payload. A missing or non-array list means schema drift or a
 * non-data response, not an empty set, so it throws; `fetchSource` turns it into a `parse` failure and the worker
 * then stores nothing. Only snapshot sources use this: an empty array returned here is a real empty current set.
 * @param list The value found at the payload's list key (or the payload itself for top-level arrays).
 * @returns The list, unchanged, when it is an array.
 * @throws Error `payload_shape` when `list` is not an array.
 */
function requireList(list: unknown): unknown[] {
  if (!Array.isArray(list)) throw new Error('payload_shape');
  return list;
}

/**
 * Map the items of a snapshot payload, failing when a non-empty list yields no valid item. `mapItems` silently drops
 * schema failures, so an upstream field rename would turn every item into a drop and produce an empty set; the worker
 * treats a verified empty set as authoritative and would prune every stored row. Throwing keeps those rows instead.
 * An empty input list stays a real empty set. A partially valid list is accepted, dropping its bad items.
 * @param list The already-required item array (see `requireList`).
 * @returns Mapped events, at least one when `list` was non-empty.
 * @throws Error `payload_shape` when `list` is non-empty and no item passes validation and building.
 */
function snapshotItems<S extends z.ZodType>(list: unknown[], schema: S, build: (item: z.infer<S>) => LiveEvent | null): LiveEvent[] {
  const out = mapItems(list, schema, build);
  if (list.length && !out.length) throw new Error('payload_shape');
  return out;
}

const point = z.object({ coordinates: z.tuple([num, num]).rest(z.unknown()) });

const usgs: BrowserSource = {
  id: 'usgs',
  snapshot: false, // 2.5-day rolling window
  name: 'USGS',
  home: 'https://earthquake.usgs.gov/',
  tags: ['geo', 'disaster'],
  intervalMs: 60_000,
  url: () => 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson',
  parse: (json) =>
    mapItems(
      field(json, 'features'),
      z.object({ id: str, properties: z.object({ title: str, time: num.nullish(), url: optStr }), geometry: point.nullish() }),
      (f) =>
        event({
          id: f.id,
          title: f.properties.title,
          tags: ['geo', 'disaster', 'earthquake'],
          source: 'USGS',
          observed_at: f.properties.time,
          lon: f.geometry?.coordinates[0],
          lat: f.geometry?.coordinates[1],
          url: f.properties.url,
        }),
    ),
};

const eonet: BrowserSource = {
  id: 'eonet',
  snapshot: false, // open events, capped at 100
  name: 'NASA EONET',
  home: 'https://eonet.gsfc.nasa.gov/',
  tags: ['geo', 'disaster'],
  intervalMs: 300_000,
  url: () => 'https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=100',
  parse: (json) =>
    mapItems(
      field(json, 'events'),
      z.object({
        id: str,
        title: str,
        link: optStr,
        categories: z.array(z.object({ id: str })).default([]),
        sources: z.array(z.object({ id: optStr, url: str })).default([]),
        geometry: z.array(z.object({ date: optStr, type: optStr, coordinates: z.unknown() })).default([]),
      }),
      (e) => {
        // The newest geometry is the event's latest position; polygons carry no single point.
        const last = e.geometry.at(-1);
        const coords = last?.type === 'Point' && Array.isArray(last.coordinates) ? last.coordinates : [];
        return event({
          id: e.id,
          title: e.title,
          tags: ['geo', 'disaster', ...e.categories.map((c) => c.id)],
          source: 'NASA EONET',
          observed_at: last?.date,
          lon: typeof coords[0] === 'number' ? coords[0] : null,
          lat: typeof coords[1] === 'number' ? coords[1] : null,
          url: e.link,
          sources: e.sources.map((s) => ({ id: s.id ?? null, url: s.url })),
        });
      },
    ),
};

const gdacs: BrowserSource = {
  id: 'gdacs',
  snapshot: false, // 7-day window
  name: 'GDACS',
  home: 'https://www.gdacs.org/',
  tags: ['geo', 'disaster'],
  intervalMs: 300_000,
  url: (_scope, now) => {
    const day = (offset: number) => new Date(now - offset * 86_400_000).toISOString().slice(0, 10);
    return `https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=EQ,TC,FL,VO,DR,WF&alertlevel=Green;Orange;Red&fromDate=${day(7)}&toDate=${day(0)}`;
  },
  parse: (json) =>
    mapItems(
      field(json, 'features'),
      z.object({
        geometry: point.nullish(),
        properties: z.object({
          eventtype: str,
          eventid: num,
          name: str,
          fromdate: optStr,
          alertlevel: optStr,
          url: z.object({ report: optStr }).nullish(),
          description: optStr,
        }),
      }),
      (f) =>
        event({
          id: `${f.properties.eventtype}${f.properties.eventid}`,
          title: f.properties.name,
          tags: ['geo', 'disaster', f.properties.eventtype, f.properties.alertlevel],
          source: 'GDACS',
          observed_at: f.properties.fromdate,
          lon: f.geometry?.coordinates[0],
          lat: f.geometry?.coordinates[1],
          url: f.properties.url?.report,
          summary: f.properties.description,
        }),
    ),
};

/** Mean of the first polygon ring's vertices: good enough to place an alert pin without a geometry library. */
function centroid(geometry: unknown): { lat: number; lon: number } | null {
  const rings = field(geometry, 'coordinates');
  const ring = Array.isArray(rings) && Array.isArray(rings[0]) ? (rings[0] as unknown[]) : [];
  const pairs = ring.filter((p): p is [number, number] => Array.isArray(p) && typeof p[0] === 'number' && typeof p[1] === 'number');
  if (!pairs.length) return null;
  return { lon: pairs.reduce((sum, p) => sum + p[0], 0) / pairs.length, lat: pairs.reduce((sum, p) => sum + p[1], 0) / pairs.length };
}

const nwsAlerts: BrowserSource = {
  id: 'nws_alerts',
  snapshot: true, // active alerts: the full current set (Severe/Extreme)
  name: 'NWS alerts',
  home: 'https://www.weather.gov/',
  tags: ['weather'],
  intervalMs: 120_000,
  // Severe and extreme only: the unfiltered list is hundreds of routine statements.
  url: () => 'https://api.weather.gov/alerts/active?status=actual&message_type=alert&severity=Severe,Extreme',
  parse: (json) =>
    snapshotItems(
      requireList(field(json, 'features')),
      z.object({
        geometry: z.unknown().optional(),
        properties: z.object({ id: str, event: str, headline: optStr, sent: optStr, severity: optStr, areaDesc: optStr, '@id': optStr }),
      }),
      (f) => {
        const at = centroid(f.geometry);
        return event({
          id: f.properties.id,
          title: f.properties.headline ?? f.properties.event,
          tags: ['weather', f.properties.event, f.properties.severity],
          source: 'NWS',
          observed_at: f.properties.sent,
          lat: at?.lat,
          lon: at?.lon,
          url: f.properties['@id'],
          summary: f.properties.areaDesc,
        });
      },
    ),
};

const hackernews: BrowserSource = {
  id: 'hackernews',
  snapshot: false, // front page, top 50
  name: 'Hacker News',
  home: 'https://news.ycombinator.com/',
  tags: ['technology'],
  intervalMs: 300_000,
  url: () => 'https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50',
  parse: (json) =>
    mapItems(field(json, 'hits'), z.object({ objectID: str, title: optStr, url: optStr, created_at: optStr }), (h) =>
      event({
        id: h.objectID,
        title: h.title ?? '',
        tags: ['technology'],
        source: 'Hacker News',
        observed_at: h.created_at,
        url: h.url ?? `https://news.ycombinator.com/item?id=${h.objectID}`,
      }),
    ),
};

const nvd: BrowserSource = {
  id: 'nvd',
  snapshot: false, // 2-day publication window
  name: 'NVD',
  home: 'https://nvd.nist.gov/',
  tags: ['security'],
  intervalMs: 900_000,
  // NVD wants zone-less ISO timestamps with milliseconds; ask only for CVEs published in the last two days.
  url: (_scope, now) =>
    `https://services.nvd.nist.gov/rest/json/cves/2.0?resultsPerPage=100&pubStartDate=${new Date(now - 2 * 86_400_000).toISOString().slice(0, 23)}&pubEndDate=${new Date(now).toISOString().slice(0, 23)}`,
  parse: (json) =>
    mapItems(
      field(json, 'vulnerabilities'),
      z.object({
        cve: z.object({
          id: str,
          published: optStr,
          descriptions: z.array(z.object({ lang: str, value: str })).default([]),
          metrics: z
            .record(z.string(), z.array(z.object({ cvssData: z.object({ baseSeverity: optStr }).optional(), baseSeverity: optStr })))
            .optional(),
        }),
      }),
      ({ cve }) => {
        const metric = Object.values(cve.metrics ?? {})[0]?.[0];
        const severity = metric?.cvssData?.baseSeverity ?? metric?.baseSeverity ?? null;
        const description = cve.descriptions.find((d) => d.lang === 'en')?.value;
        return event({
          id: cve.id,
          title: severity ? `${cve.id} (${severity})` : cve.id,
          tags: ['security', 'cve', severity],
          source: 'NVD',
          observed_at: cve.published,
          url: `https://nvd.nist.gov/vuln/detail/${encodeURIComponent(cve.id)}`,
          summary: description,
        });
      },
    ),
};

const devto: BrowserSource = {
  id: 'devto',
  snapshot: false, // top 30 articles
  name: 'DEV',
  home: 'https://dev.to/',
  tags: ['technology'],
  intervalMs: 600_000,
  url: () => 'https://dev.to/api/articles?per_page=30&top=1',
  parse: (json) =>
    mapItems(
      json,
      z.object({
        id: num,
        title: str,
        url: optStr,
        published_timestamp: optStr,
        description: optStr,
        tag_list: z.array(z.string()).default([]),
      }),
      (a) =>
        event({
          id: a.id,
          title: a.title,
          tags: ['technology', ...a.tag_list.slice(0, 3)],
          source: 'DEV',
          observed_at: a.published_timestamp,
          url: a.url,
          summary: a.description,
        }),
    ),
};

/** GitHub owner and repo names: letters, digits and `._-` only, so the scope cannot rewrite the request path. */
const GITHUB_NAME = /^[A-Za-z0-9._-]{1,100}$/;

const githubReleases: BrowserSource = {
  id: 'github_releases',
  snapshot: false, // latest 20 releases
  name: 'GitHub',
  home: 'https://github.com/',
  tags: ['technology'],
  intervalMs: 600_000,
  url: (scope) => {
    const owner = scope.live_map_github_owner ?? '';
    const repo = scope.live_map_github_repo ?? '';
    return GITHUB_NAME.test(owner) && GITHUB_NAME.test(repo) ? `https://api.github.com/repos/${owner}/${repo}/releases?per_page=20` : null;
  },
  parse: (json) =>
    mapItems(json, z.object({ id: num, name: optStr, tag_name: str, html_url: optStr, published_at: optStr, body: optStr }), (r) =>
      event({
        id: r.id,
        title: r.name || r.tag_name,
        tags: ['technology', 'release'],
        source: 'GitHub',
        observed_at: r.published_at,
        url: r.html_url,
        summary: r.body,
      }),
    ),
};

const coingecko: BrowserSource = {
  id: 'coingecko',
  snapshot: false, // top 50 by market cap
  name: 'CoinGecko',
  home: 'https://www.coingecko.com/',
  tags: ['finance', 'crypto'],
  intervalMs: 120_000,
  url: () => 'https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&per_page=50',
  parse: (json) =>
    mapItems(
      json,
      z.object({
        id: str,
        name: str,
        symbol: str,
        current_price: num.nullish(),
        price_change_percentage_24h: num.nullish(),
        last_updated: optStr,
      }),
      (c) => {
        const change = c.price_change_percentage_24h;
        const price = c.current_price == null ? '' : ` $${c.current_price}`;
        const delta = change == null ? '' : ` (${change >= 0 ? '+' : ''}${change.toFixed(2)}%)`;
        return event({
          id: c.id,
          title: `${c.name} ${c.symbol.toUpperCase()}${price}${delta}`,
          tags: ['finance', 'crypto'],
          source: 'CoinGecko',
          observed_at: c.last_updated,
          url: `https://www.coingecko.com/en/coins/${encodeURIComponent(c.id)}`,
        });
      },
    ),
};

const fearGreed: BrowserSource = {
  id: 'fear_greed',
  snapshot: false, // last 7 daily values
  name: 'Fear & Greed',
  home: 'https://alternative.me/crypto/fear-and-greed-index/',
  tags: ['finance', 'crypto'],
  intervalMs: 3_600_000,
  url: () => 'https://api.alternative.me/fng/?limit=7',
  parse: (json) =>
    mapItems(field(json, 'data'), z.object({ value: num, value_classification: str, timestamp: num }), (d) =>
      event({
        id: d.timestamp,
        title: `Fear & Greed Index ${d.value} (${d.value_classification})`,
        tags: ['finance', 'crypto', d.value_classification],
        source: 'alternative.me',
        observed_at: d.timestamp * 1000,
        url: 'https://alternative.me/crypto/fear-and-greed-index/',
      }),
    ),
};

const launches: BrowserSource = {
  id: 'launches',
  snapshot: true, // upcoming launches; the soonest 30 are the current set
  name: 'Launch Library 2',
  home: 'https://thespacedevs.com/',
  tags: ['space'],
  // The unauthenticated LL2 tier allows 15 requests per hour.
  intervalMs: 600_000,
  url: () => 'https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=30',
  parse: (json) =>
    snapshotItems(
      requireList(field(json, 'results')),
      z.object({
        id: str,
        name: str,
        net: optStr,
        slug: optStr,
        status: z.object({ name: optStr }).nullish(),
        pad: z.object({ name: optStr, latitude: num.nullish(), longitude: num.nullish() }).nullish(),
      }),
      (l) =>
        event({
          id: l.id,
          title: l.name,
          tags: ['space', 'launch'],
          source: 'Launch Library 2',
          observed_at: l.net,
          lat: l.pad?.latitude,
          lon: l.pad?.longitude,
          // LL2 `url` is its JSON API; Space Launch Now (same publisher) is the public page for the slug.
          url: l.slug && /^[a-z0-9-]{1,200}$/.test(l.slug) ? `https://spacelaunchnow.app/launch/${l.slug}/` : null,
          summary: [l.status?.name, l.pad?.name].filter(Boolean).join(' · '),
        }),
    ),
};

const freetogame: BrowserSource = {
  id: 'freetogame',
  snapshot: false, // newest 30 of the catalog
  name: 'FreeToGame',
  home: 'https://www.freetogame.com/',
  tags: ['gaming'],
  intervalMs: 3_600_000,
  url: () => 'https://www.freetogame.com/api/games?sort-by=release-date',
  // The API returns the whole catalog newest-first; keep the freshest 30.
  parse: (json) =>
    mapItems(
      Array.isArray(json) ? json.slice(0, 30) : json,
      z.object({ id: num, title: str, game_url: optStr, short_description: optStr, genre: optStr, release_date: optStr }),
      (g) =>
        event({
          id: g.id,
          title: g.title,
          tags: ['gaming', g.genre],
          source: 'FreeToGame',
          observed_at: g.release_date,
          url: g.game_url,
          summary: g.short_description,
        }),
    ),
};

const gamerpower: BrowserSource = {
  id: 'gamerpower',
  snapshot: true, // active giveaways; newest 50 are the current set
  name: 'GamerPower',
  home: 'https://www.gamerpower.com/',
  tags: ['gaming'],
  intervalMs: 1_800_000,
  url: () => 'https://www.gamerpower.com/api/giveaways',
  parse: (json) =>
    snapshotItems(
      requireList(json).slice(0, 50),
      z.object({ id: num, title: str, open_giveaway_url: optStr, description: optStr, published_date: optStr, type: optStr }),
      (g) =>
        event({
          id: g.id,
          title: g.title,
          tags: ['gaming', 'giveaway', g.type],
          source: 'GamerPower',
          observed_at: g.published_date,
          url: g.open_giveaway_url,
          summary: g.description,
        }),
    ),
};

/** Providers the browser worker fetches directly (fixed by the live-feed plan). */
export const BROWSER_SOURCES: BrowserSource[] = [
  usgs,
  eonet,
  gdacs,
  nwsAlerts,
  hackernews,
  nvd,
  devto,
  githubReleases,
  coingecko,
  fearGreed,
  launches,
  freetogame,
  gamerpower,
];

/**
 * Providers whose stored rows the worker may replace on a successful fetch (their complete current set), as the
 * `snapshotProviders` argument of `upsertEvents`. Empty for windowed or capped sources.
 * @param provider Browser source id.
 * @returns `[provider]` for a snapshot source, otherwise `[]`.
 */
export function snapshotScope(provider: string): string[] {
  return BROWSER_SOURCE_BY_ID[provider]?.snapshot ? [provider] : [];
}

/** Browser source lookup by provider id. */
export const BROWSER_SOURCE_BY_ID: Record<string, BrowserSource> = Object.fromEntries(BROWSER_SOURCES.map((source) => [source.id, source]));

/** Display name, home link and tags of providers served by the backend proxy (their items carry no tags). */
export const PROXY_PROVIDERS: Record<string, { name: string; home: string; tags: string[] }> = {
  lobsters: { name: 'Lobsters', home: 'https://lobste.rs/', tags: ['technology'] },
  cisa_kev: { name: 'CISA KEV', home: 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog', tags: ['security', 'technology'] },
  business: { name: 'VnExpress', home: 'https://vnexpress.net/kinh-doanh', tags: ['finance', 'economy'] },
  fed: { name: 'Federal Reserve', home: 'https://www.federalreserve.gov/', tags: ['economy', 'finance'] },
  sec: { name: 'SEC', home: 'https://www.sec.gov/', tags: ['finance'] },
  firms: { name: 'NASA FIRMS', home: 'https://firms.modaps.eosdis.nasa.gov/', tags: ['geo', 'disaster'] },
  bbc_world: { name: 'BBC World', home: 'https://www.bbc.com/news/world', tags: ['world', 'politics'] },
  aljazeera: { name: 'Al Jazeera', home: 'https://www.aljazeera.com/', tags: ['world', 'politics'] },
  vnexpress_world: { name: 'VnExpress World', home: 'https://vnexpress.net/the-gioi', tags: ['world'] },
  vnexpress_news: { name: 'VnExpress News', home: 'https://vnexpress.net/thoi-su', tags: ['world', 'politics'] },
  gaming_news: { name: 'Eurogamer', home: 'https://www.eurogamer.net/', tags: ['gaming'] },
};

/** True when the provider is fetched by the browser worker rather than the backend proxy. */
export function isBrowserProvider(id: string): boolean {
  return id in BROWSER_SOURCE_BY_ID;
}

/** Provider display name and home link for the status line, covering both fetch paths. */
export function providerMeta(id: string): { name: string; home: string | null } {
  const meta = BROWSER_SOURCE_BY_ID[id] ?? PROXY_PROVIDERS[id];
  return { name: meta?.name ?? id, home: meta?.home ?? null };
}

/** Tags a provider contributes to every event it delivers (browser source tags or proxy tags). */
export function providerTags(id: string): string[] {
  return BROWSER_SOURCE_BY_ID[id]?.tags ?? PROXY_PROVIDERS[id]?.tags ?? [];
}
