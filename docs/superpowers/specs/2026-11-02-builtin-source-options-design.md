# Design: Built-in datasource options (common filters, RSS sections, per-type options)

Delivery waves (from risk analysis 2026-11-02), each shippable alone with its own gate:

| Wave | Scope | Gate |
|---|---|---|
| A | Resume fix (goal 4); new config fields default `None`; per-provider registry allowlist; `select_incremental` + common filters on preset feeds/HN/JSON feeds; hash-stable coverage (remove `_with_coverage` record rewrite); `cursor_before` CAS on no-change path | defaults reproduce today's URL+caps byte-for-byte; cap < run of equal timestamps drains over N syncs, no stall; pause→resume gives 0 new observations; edited Atom item with `updated` accepted; undated items don't move cursor; crash before commit replays same cursor |
| B | RSS sections, wizard + Configure UI, receipt `collection_stats` migration, sync-history copy | alembic up/down; same article in 2 sections → 1 observation; vitest; tsc |
| C | Generic RSS/REST point fixes L3/L4/L5; MCP truncation + failed-call reporting | existing `test_collection*.py` green + one new test per fix |
| D | Hazards/security per-type options, region, native time-param resume + `config_fingerprint` | per-provider URL golden tests; native param change resets cursor |
| E | Social catch-up walk (cursor v2 with v1 fallback) | v1 cursor decodes and restarts safely; new posts appear first sync after backlog |
| F | Economy/tech/space per-type options (snapshot: no cursor) | URL golden tests |


## Intent / why
Owner is testing built-ins (e.g. Tuổi Trẻ) and found they cannot tune anything: no schedule, no section, no
limit, no name. Built-ins were designed as a zero-config toggle (`docs/connectors/free-sources.md:14`):
`enable_builtin` writes a fixed `ConnectorConfig` (`modules/connectors/builtins.py:231-234`), the registry rejects
every scope key for fixed-scope providers (`registry.py:55-60`, `backends.py:98`), and feed URLs are hard-coded
(`modules/connectors/providers/news.py:46-60`). Owner wants to shape what is collected to their need, per source type.

## Goals
1. **Common options for every built-in** (all ~40):
   - `name` — owner-chosen source name at add time (default = preset label), renamable later.
   - `schedule_interval_minutes` — 15/30/60/360/1440 (existing Literal, `public.py:311`).
   - `max_items` — **max NEW records accepted per sync**, 1..100 (default 100 = today's `MAX_FEED_ITEMS`).
     Counted after cursor + filters (see goal 7).
   - `include_keywords` / `exclude_keywords` — match title+summary, case- and diacritic-insensitive
     (NFKD strip combining marks + `đ→d`). Include = any-of; exclude wins. Bounded: ≤20 terms, each 1..64 chars.
   - `max_age_days` — drop records whose published time is older than N days (1..365); records with no
     published time are kept.
2. **RSS section picker (phase 1 per-type option)** for: `tuoitre_news`, `vnexpress_business`,
   `thanhnien_news`, `dantri_news`, `bbc_world`, `npr_news`, `fed_press`, `lobsters`.
   - One source, multi-select sections (owner decision: no per-section sources).
   - Each fetch GETs every selected section feed, merges, dedups by stable item id (an article can sit in
     several sections), then `select_incremental` (goal 8): oldest-first, `max_items` cap, backlog next sync.
     Section slug never enters record metadata (would change `record_hash` → one copy per section).
3. Same options shown in **add wizard** (`BuiltinWizard`/`BuiltinTermsFields`) and **Configure**
   (`connector-editor.tsx`, already has built-in edit mode and schedule panel).
4. **Resume keeps owner choices.** `enable_builtin` re-run must not reset config to defaults.

5. **Per-type options for every other built-in** (owner: "làm luôn cho các phần khác"). All values are
   server-side enums/bounded numbers; "native" = pushed into the upstream request, "client" = filtered after
   mapping. Defaults reproduce today's request exactly.

   | Provider | Options (native/client) | Default = today |
   |---|---|---|
   | `hn_top` | `hn_list` top/new/best/ask/show (native); item count = `max_items`, fan-out hard cap 30 | top, 10 |
   | `gdelt_economy` | `gdelt_query` bounded text (sanitized like `news_query`), `gdelt_timespan` 1h/6h/24h/3d/7d, `gdelt_source_lang` enum, `gdelt_source_country` ISO enum (native, in-query operators); `maxrecords` = `max_items` ≤50 | economy, 24h, 5 |
   | `usgs` | `usgs_min_magnitude` significant/4.5/2.5/1.0/all + `usgs_period` hour/day/week → summary feed filename (native) | 4.5_day |
   | `emsc_quakes` | `min_magnitude` 2.0–8.0 → `minmag` (native); `limit` = `max_items` | 4, 50 |
   | `nasa_eonet` | `eonet_categories` multi (from `/api/v3/categories` curated list), `eonet_status` open/all, `eonet_days` 1–30 (native) | open, 50 |
   | `gdacs_events` | `gdacs_event_types` multi EQ/TC/FL/VO/WF/DR, `gdacs_alert_levels` multi Green/Orange/Red (native) | all types, Orange+Red |
   | `nws_alerts` | `nws_severity` multi Extreme/Severe/Moderate/Minor, `nws_area` US state enum optional (native) | Extreme+Severe, all US |
   | `nvd` | `nvd_severity` multi LOW/MEDIUM/HIGH/CRITICAL, `nvd_window_days` 1–7, `nvd_keyword` bounded text (native); page = `max_items` ≤100 | all, 1 day, 20 |
   | `cisa_kev` | `kev_ransomware_only` bool (client) | off |
   | `ioda` | `ioda_country` ISO optional (native `entityCode`) | all countries |
   | `launch_library` | count = `max_items` ≤25 (tight 15 req/h upstream) | 10 |
   | `wikipedia_mostread`, `wikipedia_top_views` | `wiki_language` enum en/vi/ja/zh/fr/de/es/ko (native project) | en |
   | `satnogs_observations` | `satnogs_status` good/bad/unknown (native) | good |
   | `disease_sh` | `country` ISO optional (native `/countries/{x}`) | global |
   | `world_bank` | `wb_country` ISO enum, `wb_indicator` curated enum (GDP, GDP growth, inflation CPI, unemployment, population, FDI) (native) | VN, GDP |
   | `frankfurter` | `fx_base`, `fx_quotes` multi ≤10 from provider-supported currency list (native) | USD→VND |
   | `ecb` | `fx_quotes` subset (client) | all |
   | `open_er_api` | `fx_base` enum (native), `fx_quotes` subset (client) | USD, all |
   | `oecd_cli` | `oecd_countries` multi ≤10 ISO enum (native SDMX key) | USA+CHN+JPN+DEU+GBR |
   | `binance` | `crypto_symbols` multi ≤10 from curated pair list (native; one batched `ticker/price?symbols=` call) | BTCUSDT |
   | `coinpaprika` | `coin_ids` multi ≤5 curated (native; one call each → counts against quota) | btc-bitcoin |
   | `noaa_kp` | `kp_min` 0–9 threshold (client) | none |
   | `us_travel_advisories` | `advisory_min_level` 1–4 (client, parsed from title) | 1 |
   | `aljazeera_world`, `gdacs_rss`, `noaa_tsunami`, `jma_eqvol`, `bmkg_quakes`, `alternative_me`, `iss_position` | common options only (no useful native filter) | — |

6. **Region preset (client, shared)** for geo records (`usgs`, `emsc_quakes`, `nasa_eonet`, `gdacs_events`,
   `bmkg_quakes`): `region` enum (world, southeast_asia, vietnam, east_asia, south_asia, europe,
   north_america, ...) mapped to server-side bounding boxes; drops records whose coordinates fall outside.

7. **Resume from last record (incremental cursor)** for `news`/`event` built-ins.
   - Reuse existing per-source collection state `cursor` (already used by generic RSS:
     `read_rss(url, state.cursor)`, `collection.py:647`; cursor commit in same transaction as batch,
     `collection.py:511-533`). Built-in preset path `_run_feed` (`collection.py:1165`) does not use it today.
   - Cursor = plain ISO time string (existing column format, no envelope) = newest `max(published, updated)`
     among **dated** accepted records. Used as an inclusive lower bound minus a small skew margin; dedup
     handles the overlap. No `(ts,id)` tuple / boundary ids needed.
   - Each sync: see `select_incremental` in goal 8 (dedup **before** cap, oldest first). Leftover backlog is
     taken next sync.
   - Known limit (documented): edits to items older than the window with no `updated` field are not picked up.
   - Native resume where upstream has a time param: `nvd` pubStartDate, `usgs` (switch to FDSN `starttime` when
     cursor set, builder may keep feed when no cursor), `nasa_eonet` start, `gdelt_economy` startdatetime,
     `launch_library` last_updated__gte. Others filter client-side.
   - Gap detection: if cursor is older than the oldest item the feed still returns ⇒ gap window in run stats.
   - Records without published time: never move the cursor; accepted via dedup only, counted in cap, placed last.
   - Measurement snapshots: no cursor (each sync is a fresh snapshot).
   - Changing sections/filters keeps cursor (applies to future fetches); changing a native query param
     (e.g. `gdelt_query`, `nvd_keyword`, `usgs_min_magnitude`) resets cursor to "now − default window"
     (tracked by a small `config_fingerprint` stored only for native-param providers, wave D). UI copy warns
     older items matching only the new query are not backfilled.
   - Sync history (`sync-history.tsx`) shows: "continued from <time> · N new · M waiting for next sync" or
     "gap: K records missed (<from>–<to>)", from receipt `collection_stats` (goal 8).

8. **Common resume/no-loss/no-dup mechanism for ALL datasources** (owner: "làm common ... toàn bộ").
   Revised after risk analysis (2026-11-02): **no new contract/envelope**. The checkpoint = two things that
   already exist working together: **dedup** (`seen_record_keys`, `ingestion/public.py:1907`) + a **time cursor**
   (`source_ingestion_state.cursor`, plain ISO). The cursor survives generation bumps (only
   `reset_native_collection_cursor` clears it, `ingestion/public.py:1150`), so it also stops re-ingest after
   pause/resume and config saves — which each bump generation and reset dedup scope
   (`sources/public.py:1347`, `:953`; `enable_builtin` bumps twice).

   **Shared, new, small** — `modules/connectors/selection.py`:
   - `select_incremental(records, *, cursor, seen, filters, max_items, skew)` →
     `(taken, backlog_count, cursor_after, gap)`:
     1. drop records with time < `cursor − skew`;
     2. drop already-seen (runner calls read-only `seen_record_keys` **before** the cap — else a cap smaller
        than a run of seen items stalls forever; safe because one active request per source,
        `connectors/models.py:585`; in-transaction dedup stays as guard);
     3. owner filters (age, keywords, region, client-side per-type);
     4. sort oldest first by `(time, provider_id)`, undated last;
     5. cap `max_items`;
     6. `cursor_after` = newest dated `max(published, updated)` among taken, else old cursor;
     7. `gap` when cursor exists and oldest dated item returned is newer than it.
   - `fold()` diacritic-insensitive whole-word matcher.

   **Run stats**: additive migration — nullable `collection_stats JSONB` (bounded ~1 KB) on
   `ingestion_collection_receipts`, written by `_settle_request` (`ingestion/public.py:1435`) from a new
   optional `stats` field on `CollectionStateUpdate`. Holds new / backlog / gap / duplicates skipped /
   failed sections. Rollback = drop column; old code ignores it. **Never** put cap/backlog/section/coverage
   into `IngestionRecord.metadata` (it is in `record_hash` ⇒ duplicates).

   **Per path**:
   | Path | Change |
   |---|---|
   | Preset feeds `_run_feed`, HN, JSON/hazard feeds (`collection.py:1165-1222`) | read `state.cursor` + `seen_record_keys`, call `select_incremental`, pass existing `cursor_after=` to `_accept_native` (`:1037`). Remove `_with_coverage` record rewrite (`news.py:326-329`) — causes duplicate observations today. |
   | Generic REST/RSS | keep `rest.Checkpoint`; point fixes: L5 undated items don't move cursor (`n8n.py:429,446`); L3 >500-item page degrades to cap+backlog instead of failing forever (`collection.py:651`); L4 fixed 1-day overlap → skew margin + dedup. |
   | Native time-param APIs (NVD, USGS, EONET, GDELT, Launch Library) | wave D: push cursor as native start param; `config_fingerprint` beside it. |
   | Social (Bluesky/Reddit/Mastodon/X) | wave E: catch-up walk; cursor v2 `{v:2, high_water, page?}`, v1 decodes as "walk in progress, no high-water" (safe restart, dedup guards). |
   | MCP | tail beyond cap counted + reported `truncated`; failed calls counted in stats (`collection.py:788`). |
   | Google, GitHub, Telegram, measurements, crawl worker | **no change** (GitHub/Telegram proof validators compare cursor byte-for-byte, `ingestion/public.py:2243,2263`). |

   **Hardening**: add `cursor_before` CAS to `accept_collection_no_changes` (`ingestion/public.py:2697-2699`,
   today unguarded).

   **Do NOT**: change cursor format for time sources; wrap Google/GitHub/Telegram cursors; touch
   `_apply_state_update` (GitNexus HIGH: 23 impacted, 3 processes); change generation scoping of
   `seen_record_keys`; unify coverage enums (receipt `coverage` is `String(16)`); add `boundary_ids`.

   **Blast radius** (GitNexus; undercounts because runners dispatch via `ADAPTERS` dict): `_apply_state_update`
   HIGH (avoided); `_accept_generic`, `accept_native_collection`, `rest.Checkpoint`, `_accept_walk`,
   `accept_collection_no_changes` LOW. Grep: 15 `_accept_native(` + 8 `_accept_generic`/`_accept_walk` call
   sites. Pinning tests: `test_collection.py`, `test_collection_github_telegram.py`,
   `test_free_provider_dispatch.py`, `test_keyed_providers.py`, `test_sec_edgar.py`, `test_coingecko_key_slot.py`.

## Non-goals
- Multiple sources of the same built-in (owner: one source + multi-select is enough). Keep one-per-built-in.
- Free-form URLs/bboxes/coordinates from the owner (enums only this round).
- Non-built-in (scoped/keyed) providers — already have scope fields.
- Cross-source dedup, keyword relevance ranking, LLM filtering.

## Anti-goals (failure even if it works)
- Built-in with no changes behaving differently from today (defaults must reproduce current URL + caps).
- Any owner input reaching a URL unvalidated (SSRF/host escape). Sections are slugs from a server allowlist,
  never free URLs; hosts stay the spec's `hosts`.
- Silently blowing provider politeness budgets.

## Decisions made (by owner)
- One source per built-in, multi-select sections, owner-named.
- Filter/section changes apply to future fetches only; already-collected records stay. UI copy says
  "applies to new fetches". No purge job.
- Scope = common options for all + sections for RSS news + per-type options for every other built-in, in one design.

## Decisions made (my call — owner may override)
- **Section catalog lives server-side** in `provider_specs.py` (or a sibling module): per provider
  `sections: tuple[(slug, label_en, label_vi)]` + `default_section` (= today's feed, e.g. tuoitre `home`,
  vnexpress `kinh-doanh`, npr `1001`, fed `press_all`, lobsters front page). Exposed via `BuiltinSourceRead`
  and catalog so UI renders it; no duplicated slug lists in the frontend.
- Curated slug lists from the outlets' RSS index pages (tuoitre.vn/rss.htm, vnexpress.net/rss,
  thanhnien.vn/rss.html top-level + key subsections, dantri.com.vn/rss.htm). BBC/NPR/Fed/Lobsters lists are
  from memory → builder must verify each URL returns a valid feed before shipping it; drop failures.
- **Max 6 sections per source.** Quota is per HTTP call (`_day(96)` for RSS presets, `provider_specs.py:103`);
  raise those presets' daily pilot cap to `96 * 6` (self-imposed politeness cap, not an upstream limit) so a
  30-min schedule with 6 sections fits. UI shows "N sections × every M min ≈ X requests/day".
- **Conditional GET**: keep ETag/Last-Modified replay only when exactly one section is selected (current
  validators are single-URL, `collection.py:1166-1186`); multi-section fetches go unconditional. Dedup by
  `(provider_id, record_hash)` already prevents re-ingest.
- **Where common filters run**: inside `select_incremental` (goal 8), called by each runner after mapping.
  Order: cursor floor → dedup → filters → oldest-first → cap.
  `max_items` replaces each adapter's cap only downward (never raises hard caps like usgs 500 fail-closed).
- Config storage: new optional fields on `ConnectorConfig` (`public.py:291`, `extra="forbid"`), all default
  `None` = current behaviour. Registry: common filter fields allowed for every built-in; `feed_sections`
  allowed only for providers with a section catalog, values must be in that catalog.
- **Enable request** (`BuiltinEnableRequest`, `builtins.py:73`) gains optional `name`, `schedule_interval_minutes`,
  `feed_sections`, `max_items`, `include_keywords`, `exclude_keywords`, `max_age_days`. Precedence per field:
  request value → currently saved config → spec default. Same rule applied to existing `realtime_mode`.
  `name` only applied on create; rename after uses existing `SourcePatch.name` (`modules/sources/schemas.py:41`).
- **Partial section failure**: if ≥1 selected section succeeds, keep its records, mark run coverage
  `truncated` with failed slugs in run metadata, and show a warning in sync history; all sections fail →
  run fails as today. Every GET attempt counts against quota.
- **Option applicability by data shape**: keywords apply to `news`/`event` records only (title+summary);
  `max_age_days` to `news`/`event` with a published time; `max_items` to list feeds. Measurement snapshots
  (prices, FX, macro series, Kp, ISS) get no keyword/age/max_items — they are tuned by their per-type options
  (symbols, currencies, countries). Catalog exposes `applicable_options` per built-in; registry allowlists per
  provider (not blanket); wizard/Configure render only applicable fields.
- **Keyword matching**: whole-word / phrase match on folded tokens (lowercase, NFKD diacritics stripped, `đ→d`,
  split on non-alphanumerics). Multi-word term = consecutive token phrase. Avoids "ga"→"garage", "AI"→"mail".
- i18n en/vi for all new labels/hints (`apps/web/src/core/messages/sources.ts`, `source-scope-hints.ts` pattern).

## Left to builder
- Exact module placement of section catalog and shared filter step; field names if clearer ones fit.
- UI layout within design system (multi-select = existing shadcn primitives; `ScopeHint` pattern).
- Whether Configure exposes rename inline or via existing source rename UI.
- Test split. Required at minimum:
  - pytest: defaults reproduce today's request URL/caps; multi-section merge+dedup; keyword include/exclude
    with Vietnamese diacritics; max_age; max_items; invalid section slug → 422; too many sections → 422;
    resume keeps saved interval/sections; registry rejects `feed_sections` on non-RSS built-in;
    cursor: oldest-first cap + backlog drained next sync, cap < run of seen/equal-timestamp items does not
    stall, pause→resume 0 new observations, undated items don't move cursor, gap detection, crash before
    commit replays from previous cursor, native-param change resets cursor, same record hash across
    truncated/non-truncated runs.
  - vitest: wizard sends options in enable body; Configure renders sections for RSS built-in only.
- GitNexus `impact` on `enable_builtin`, `ConnectorConfig`, registry validate, `feed_request`,
  collection RSS path, `BuiltinTermsFields`, `enableBuiltinSource` before edits; JSDoc/docstrings per AGENTS.md.

## Verify
- `pytest tests/unit/modules/connectors` (builtins, news, registry, collection).
- `cd apps/web && npx vitest run src/modules/sources && npx tsc --noEmit`.
- Manual: Add datasource → Built-in feed → Tuổi Trẻ → name "TT kinh tế & công nghệ", sections
  `kinh-doanh` + `nhip-song-so`, keyword exclude "quảng cáo", every 60 min → test fetch shows only those
  sections; Pause → Resume → settings unchanged; Configure shows same values.

## Release notes

- The first sync after deploy may re-accept up to 100 items that were previously stored with coverage "truncated".
- The first sync of a feed with more than 100 items takes the oldest 100 and drains the rest on later syncs.
- `hn_top` and `google_news` are dedup-only: no cursor floor, the stored cursor is left unchanged.
- Measurement providers (including cisa_kev, ioda, nasa_eonet, nvd, usgs) are snapshots with no cursor and accept no filter options.
