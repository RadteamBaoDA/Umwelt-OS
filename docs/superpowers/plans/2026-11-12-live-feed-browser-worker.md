# Live feed: browser worker + local store (hybrid)

Owner decision 2026-11-12: hybrid. Browser Web Worker fetches CORS-capable public sources directly; CORS-blocked RSS and keyed providers stay on the existing stateless BE proxy (`/api/v1/live-map/events`, in-memory cache, no event rows in Postgres). All events (direct + proxied) land in an IndexedDB store in the browser, tagged and filterable. BE persistence is a proposal only (section 5), not built.

## 1. Facts (probed 2026-11-12 with `Origin:` header)

Direct OK (`Access-Control-Allow-Origin` present): USGS, NASA EONET, HN Algolia, NVD, GitHub API, GDACS, CoinGecko, alternative.me FNG, Launch Library 2, FreeToGame, GamerPower, dev.to, api.weather.gov, Wikipedia (`origin=*`), Open-Meteo.
Blocked (no ACAO / 403): VnExpress RSS, Federal Reserve RSS, Lobsters, CISA KEV, BBC RSS, Reddit, tsunami.gov. GDELT 429, Polymarket timeout: skip.
Secret/header bound (BE only): FIRMS (key), OpenWeather tiles (key), SEC (User-Agent).

## 2. Provider split

| Path | Providers | Tags |
|---|---|---|
| Browser worker (new) | `usgs` (all_day / 2.5_day feed, 60 s), `eonet`, `gdacs`, `nws_alerts`, `hackernews` (Algolia front_page, one request), `nvd`, `devto`, `github_releases` (scope owner/repo), `coingecko` (top markets), `fear_greed`, `launches` (LL2 upcoming, pad lat/lon), `freetogame`, `gamerpower` | geo, disaster, weather, technology, security, finance, crypto, economy, space, gaming |
| BE proxy (existing + new RSS) | existing: `lobsters`, `cisa_kev`, `business` (VnExpress kinh doanh), `fed`, `sec`, `firms`; new RSS: `bbc_world`, `aljazeera`, `vnexpress_world`, `vnexpress_news`, `polygon_gaming` | politics, world, economy, finance, gaming, technology, security |

`usgs`, `eonet`, `hackernews`, `nvd`, `github_releases` are removed from the BE proxy (no dead paths). Terms gating still applies to every provider: the worker only fetches providers the BE `terms_catalog` marks `accepted`; new direct providers get catalog entries (attribution, terms URL) in `modules/connectors/provider_specs.py` exactly like existing ones, with an empty/none egress (browser-side) if the spec model requires it.

## 3. Frontend (`apps/web/src/modules/dashboard/live-feed/`)

- `sources.ts`: catalog `{ id, url(scope), intervalMs, tags, parse(json|text) => LiveEvent[] }`. Parsers validate with `zod` (installed), cap 200 items/source, title 500 chars, only `http(s)` links (reuse `safeLiveLink`). `LiveEvent` gains `tags: string[]`; event tags = source tags + provider-native type (e.g. GDACS `eventtype`, EONET category) lower-cased.
- `live-feed.worker.ts` (module worker, `new Worker(new URL(..., import.meta.url), { type: 'module' })`): receives `{type:'config', providers, scope, workspaceId}` and `{type:'visibility', visible}`; per-source timer (pauses while hidden, no background polling), `fetch` with `AbortSignal.timeout(15s)`, `credentials:'omit'`, `referrerPolicy:'no-referrer'`, response size cap 5 MB, per-source exponential backoff on error/429; writes to IndexedDB; posts `{type:'status', provider, ok, error?, fetchedAt}` and `{type:'changed'}`. Never touches DOM or app cookies.
- `store.ts`: native IndexedDB (no new dependency). DB name `umwelt-live-feed:<workspaceId>`, store `events` keyPath `key` (`provider:id`), indexes `observed_at`, `tags` (multiEntry), `provider`. Upsert by key; prune `observed_at` older than 30 days and cap 5 000 rows after each write batch. `clearLiveFeed()` deletes all `umwelt-live-feed:*` DBs; call it from the same logout / account-deletion path that clears dashboard drafts.
- `use-live-feed.ts`: owns worker lifecycle (start when gadget visible, terminate on unmount), keeps existing react-query calls for BE-proxied providers and upserts their items into the store, reads store with filters `{ tags[], days, query }` and returns `{ events, points, statuses }`. Keeps the existing stable-points memo.
- `live-map-gadget.tsx`: replace category `Select` with tag filter (multi-toggle chips using `components/ui` primitives, "Tất cả" reset), keep period select, add text search. Status/attribution line covers both paths. i18n keys in en-us and vi-vi.
- `core/csp.ts`: append exact origins of the browser providers to `connect-src` (no wildcards beyond what a provider needs); update `csp.test.ts`.

## 4. Backend

- `modules/connectors/live_map.py` / `provider_specs.py`: add the five RSS providers (fixed-origin endpoints, existing XML parser, egress allowlist, terms catalog entry, tags/category), remove the five migrated providers and their parsers/tests, catalog entries for the new browser providers. Ruff + mypy + targeted pytest.

## 5. Proposal: saving to BE (not built)

1. **Pin to keep (recommended first step)**: a "Lưu" action on one event posts `{title, url, source, observed_at, tags}` to the existing document/bookmark ingestion as a URL source item with provenance (provider id, fetched_at). Owner-initiated, small, reuses permission + deletion flows; chat/search can cite it.
2. **Watch rules**: owner-defined tag/keyword rules; matching events are sent in a batched POST (≤50/req) to a `live_feed_saved` table owned by `modules/dashboard`, with TTL retention (e.g. 90 days) and erasure hook in `modules/dashboard/erasure.py`.
3. **Daily digest**: the existing daily brief job reads saved events for summaries instead of re-fetching.
Avoid bulk-mirroring the whole local store: it re-creates the DB load the owner wants to avoid and copies third-party content without a retention reason.

## 6. Validation

Vitest: parsers (fixture per source), store upsert/prune/filter (fake-indexeddb only if already installed; else test the pure filter/prune functions), worker message handling as pure functions, CSP test. Pytest: live_map providers. `pnpm lint`, `typecheck`, ruff, mypy. Browser check of the overview after both stages.
