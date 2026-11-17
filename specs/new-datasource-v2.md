# Technical Implementation Plan: Media Collectors & Dashboard Gadget Upgrades for Umwelt-OS

**Status:** Implementation in progress; see `docs/superpowers/plans/EXECUTION.md`.
**Target Repository:** `d:\Project\Umwelt-OS`
**Prepared:** 2026-11-17

---

## Executive Summary

This plan defines the architecture, verified official data sources, data contracts, exact codebase file changes, dashboard gadget upgrades, and automated verification gates required to add **Movie**, **Anime**, **Manga / Manhwa**, **Book**, **YouTube New Releases** (upgraded), and **Recommended Related Media** (**Podcasts**, **TV Series / Episodes**, **Music Releases**, and optional **Video Games**) collectors to Umwelt-OS.

### Core Architectural Decisions
1. **Keyless-First, Optional-Key Official Sources:** Every primary category works out-of-the-box with zero API keys via official public APIs, GraphQL endpoints, or Atom/RSS feeds. Optional API-key presets (`source.type = "api"`) unlock commercial/high-detail catalogs (TMDB, Trakt, OMDb, MAL v2, Hardcover, Google Books, YouTube Data API v3, Podcast Index, IGDB/Steam) via `ConnectorRestCredential` or native multi-secret/OAuth adapters.
2. **Gated Shared Executor Integration (`SendGate` + `rest.fetch_bounded`):** All new media collectors (and the upgraded `youtube` collector) execute through `modules/connectors/collection.py` + `SendGate` + `rest.fetch_bounded`, ensuring PostgreSQL quota debiting (`quota.py`), terms gating (`provider_terms.py`), SSRF/DNS pinning (`core/net_policy.py`), decompression-bomb byte caps, `ETag`/`Last-Modified` conditional GET, and `429 Retry-After` deferral.
3. **Dedicated `"media"` `DataShape` & Typed `MediaProjectionRead` Contract:** Rather than overloading `news_feed` (which routes through `NewsFeed` story clustering) or parsing semicolon-delimited strings out of `excerpt` (the `job_posting.ts` pattern), we introduce `"media"` to `DataShape` (`modules/connectors/data_shapes.py`), register bounded media `source_fields` in `modules/knowledge/documents/schemas.py`, expose a typed optional `media` block on `GadgetDocumentProjectionRead`, and preserve `/api/v1/media/proxy?token=...` signing via `core/media_proxy.py` (`make_image_signer`).
4. **Two-Tier Multi-Dimensional Filtering Across the Stack (Country, Genre, Language, Format, Year/Season, Content Rating, Min Rating):** Combine **upstream query pushdown** (via `provider_options.py` `OPTION_FIELDS` + `build_url()`/`KeyedProvider.build()`, falling back to post-fetch `filter_records()` for filterless RSS/schedule feeds) with **normalized `_MEDIA_FIELDS` metadata** (`country`, `language`, `genres`, `release_year`, `release_season`, `format`, `content_rating`, `rating` normalized `0.0–100.0`) and **server-side JSONB projection filtering** (`_news.py` / `routes.py`) wired into `GadgetScope`, `GadgetFilters`, the Gadget Settings Editor, and compact in-gadget filter popovers/chips.
5. **Media-Capable Dashboard Gadgets:** We add **`media_feed`** (Media Shelf & Releases — adaptive `2:3` poster / `1:1` cover / `16:9` video aspect ratios, multi-dimensional filter bar + popover + active chips, metadata badges, cursor pagination, bookmark/read/dismiss, 1-click "Ask AI") and **`media_calendar`** (Release Calendar — multi-source upcoming/airing/recent timeline buckets with country/genre/language/format/rating filters), while upgrading **`video_panel`** (removing YouTube-only locks, adding pagination/timestamps/Ask AI) and **`YoutubeScope`** (`@handle` resolution, multi-channel/playlist & long-form/Shorts filtering).

---

## Part 1: R1 — Official Data Sources & Reference GitHub Implementations

### 1.1 Category-by-Category Source Matrix (Keyless Primary vs. Optional API Key)

| Category | Keyless Primary Official Source(s) | Optional API-Key Official Source(s) (What the Key Unlocks) | Primary Dedupe Key & Delta-Sync | Rate Limits & Licensing / ToS Notes |
|---|---|---|---|---|
| **1. Movie** | **Wikidata SPARQL** (`https://query.wikidata.org/sparql`, `wd:Q11424`, `P577` release date, `P345` IMDb / `P4947` TMDB IDs, `P18` Wikimedia Commons image)<br>**iTunes Search API** (`https://itunes.apple.com/search?media=movie&entity=movie`)<br>**Letterboxd Public RSS** (`https://letterboxd.com/{username}/rss/`)<br>*(Optional bulk local index: TMDB Daily ID Exports `https://files.tmdb.org/p/exports/movie_ids_MM_DD_YYYY.json.gz` [NDJSON]; IMDb Datasets `https://datasets.imdbws.com/` — non-commercial personal use only)* | **TMDB API v3** (`https://api.themoviedb.org/3` — unlocks `/movie/now_playing`, `/movie/upcoming`, `/movie/changes`, localized posters/backdrops & synopses)<br>**Trakt API v2** (`https://api.trakt.tv` — trending/anticipated & user sync)<br>**OMDb API** (`https://www.omdbapi.com/` — IMDb/RT/Metacritic ratings)<br>**Fanart.tv v3** (`https://webservice.fanart.tv/v3/movies/{id}`) | `tmdb:{id}` / `imdb:{tconst}` / `wikidata:{qid}` / `letterboxd:{guid}`<br>Delta: TMDB `/3/movie/changes` (max 14-day window); Wikidata `schema:dateModified`; Letterboxd `ETag`/`If-Modified-Since` (50-item window) | Wikidata: **CC0 1.0**, mandatory descriptive `User-Agent`, 60s query timeout.<br>iTunes: ~20 req/min (`rss.marketingtools.apple.com` does **not** support `movies`).<br>TMDB v3: ~40–50 req/s, **mandatory visible TMDB attribution + logo**.<br>OMDb: 1,000 req/day free.<br>IMDb `.tsv.gz`: strictly personal/non-commercial (`\N` = null). |
| **2. Anime** | **AniList GraphQL API v2** (`POST https://graphql.anilist.co`, `MediaType: ANIME` + `airingSchedules` + `updatedAt`)<br>**LiveChart.me Official RSS** (`https://www.livechart.me/feeds/episodes`, `/headlines`)<br>**Kitsu Edge API** (`https://kitsu.io/api/edge/anime`)<br>**ANN Encyclopedia XML & Atom** (`https://cdn.animenewsnetwork.com/encyclopedia/api.xml`, `https://www.animenewsnetwork.com/news/atom.xml`)<br>**Cross-ID Index:** `manami-project/anime-offline-database` & `Fribb/anime-lists` | **MyAnimeList Official API v2** (`https://api.myanimelist.net/v2`, `X-MAL-CLIENT-ID` — official MAL seasonal/rankings & user list sync without scraping)<br>**AniDB HTTP API** (`http://api.anidb.net:9001/httpapi`)<br>**Simkl API** (`https://api.simkl.com`) | `anilist:{id}` / `livechart:{guid}` / `kitsu:{id}` (cross-mapped via `idMal` / `Fribb/anime-lists`)<br>Delta: AniList `sort: UPDATED_AT_DESC` + `updatedAt` & `airingSchedules(airingAt_greater, airingAt_lesser)`; LiveChart `If-Modified-Since` | AniList: **90 req/min** (drops ~30 under load, inspect `X-RateLimit-Remaining`, `429` = 1-min cooldown), non-commercial free (<$150/mo revenue).<br>LiveChart: "as-is", requests `If-Modified-Since`.<br>ANN: **1 req/sec**, batch <=50 IDs, non-bot `User-Agent`.<br>MAL v2: <=1 req/s recommended.<br>*Do NOT use Jikan v4 as primary (unofficial scraper).* |
| **3. Manga / Manhwa** | **AniList GraphQL API v2** (`type: MANGA`, `countryOfOrigin: JP` [Manga] / `KR` [Manhwa] / `CN`/`TW` [Manhua])<br>**MangaUpdates Official API v1** (`https://api.mangaupdates.com/v1` — explicit `Manhwa` type, `latest_chapter`, `licensed`, official publishers, `last_updated`)<br>**MangaDex Official API v5** (`https://api.mangadex.org`, `originalLanguage[]=ko/ja/zh`, `updatedAtSince`)<br>**Kitsu Edge** (`/manga`) & **Shikimori** (`/api/mangas`, `/api/ranobe`) | **MyAnimeList Official API v2** (`https://api.myanimelist.net/v2/manga`, `X-MAL-CLIENT-ID`) | `anilist_manga:{id}` / `mangaupdates:{series_id}` / `mangadex:{uuid}`<br>Delta: AniList `updatedAt`; MangaUpdates `/v1/releases` + `last_updated.timestamp`; MangaDex `updatedAtSince` / `publishAtSince` | MangaUpdates: 1–2s spacing, **attribution required**, `POST` for search, integer `series_id` (not base36 web slug).<br>MangaDex: **5 req/sec**, **strict non-commercial AUP + mandatory attribution + no hotlinking** (proxy covers via `/api/v1/media/proxy`), missing `User-Agent` or ignoring `429` triggers `403` IP ban.<br>*Note: MANGA Plus & WEBTOON have NO official public API/RSS; track official releases via MangaUpdates `licensed`/`publishers` & AniList `externalLinks`.* |
| **4. Book** | **Open Library APIs** (`https://openlibrary.org/search.json`, `/works/{id}.json`, `/subjects/{subject}.json`, `/recentchanges.json`, `https://covers.openlibrary.org/b/...`)<br>**Crossref Polite Pool** (`https://api.crossref.org/works?filter=type:book&mailto=...`)<br>**Gutendex** (`https://gutendex.com/books` — public domain)<br>**Apple Marketing Tools RSS v2** (`https://rss.marketingtools.apple.com/api/v2/{cc}/books/{chart}/{limit}/books.json` & `audio-books`) | **Hardcover GraphQL API** (`POST https://api.hardcover.app/v1/graphql`, Bearer token — modern releases, series, upcoming books)<br>**Google Books API v1** (`https://www.googleapis.com/books/v1/volumes?q=...&key=...` — rich ISBN-10/13, page count, blurbs) | `openlibrary:{work_key}` / `isbn13:{isbn}` / `doi:{doi}` / `hardcover:{id}`<br>Delta: Open Library `/recentchanges.json` & `search.json?sort=new` | Open Library: **CC0 1.0**, identified `User-Agent` (~3 req/s vs ~1 anonymous; ISBN cover lookups ~100/5m so prefer `cover_i`).<br>Crossref: **CC0**, ~3–10 req/s with `mailto`.<br>Hardcover: **60 req/min** (burst 10), 5,000/day.<br>Google Books: ~1,000/day; normalize `http://` `imageLinks` to `https://`. |
| **5. YouTube New Releases** | **YouTube Native Atom RSS** (`https://www.youtube.com/feeds/videos.xml?channel_id=UC...`, `?playlist_id=PL...`, long-form `?playlist_id=UULF...`, Shorts `?playlist_id=UUSH...`)<br>**Keyless `@handle -> UC...` Resolution** (cached HTML `<link rel="canonical">` / `<link rel="alternate">` discovery) | **YouTube Data API v3** (`https://www.googleapis.com/youtube/v3/channels?part=id,contentDetails&forHandle=...` [1 unit] + `playlistItems.list` on `UU...` [1 unit] + `If-None-Match` — unlocks >15-item history, durations, reliable datacenter polling) | `youtube:{channel_or_playlist}:{videoId}`<br>Delta: `yt:videoId` + `If-None-Match` / `If-Modified-Since` (and optional future WebSub `pubsubhubbub.appspot.com`) | Atom feed: **15-item rolling cap**, strictly needs 24-char `UC...` ID (`@handle` -> `400`), prone to transient datacenter `404`/`500` (needs graceful backoff).<br>Data API v3: **10,000 units/day** — strictly use `channels.list` (1u) + `playlistItems.list` (1u); **never** use `search.list` (100u). |

### 1.2 Category 6 — Recommended Related Media Categories (Ranked)

| Rank & Category | Keyless Primary Official Source(s) | Optional API-Key Upgrade | Value & Cleanliness Rationale |
|---|---|---|---|
| **6a. Podcasts** *(Wave 1 Recommended)* | **iTunes Search / Lookup API** (`https://itunes.apple.com/search?entity=podcast` -> `feedUrl`, ~20 calls/min) + **Standard RSS 2.0 / iTunes / Podcasting 2.0 XML Feeds** (`ETag`/`Last-Modified` 304, `<enclosure>`, `<itunes:duration>`, `<podcast:chapters>`) + **Apple Marketing Tools RSS v2** | **Podcast Index API v1.0** (`https://api.podcastindex.org/api/1.0/...`, `X-Auth-Key` + `X-Auth-Date` + SHA-1 `Authorization`, `newestSince` delta endpoint) | **Highest keyless cleanliness:** 100% open RSS ecosystem; `304 Not Modified` efficiency; dedupe by `<guid>` with `(enclosure.url, title, pubDate)` fallback; parse `<itunes:duration>` (`HH:MM:SS` / `MM:SS` / int seconds). |
| **6b. TV Series / Episodes** *(Wave 1 Recommended)* | **TVMaze Public REST API** (`https://api.tvmaze.com/schedule`, `/schedule/web`, `/updates/shows?since=day\|week\|month`, `/shows/{id}?embed=episodes`) | **TMDB API v3 (`/3/tv/airing_today`)**; **Trakt API v2 (`/calendars/all/shows/{start_date}/{days}`)** | **Highest keyless JSON quality:** >=20 req/10s per IP, **CC BY-SA 4.0** (visible TVMaze attribution link required), clean ISO-8601 UTC `airstamp` for release calendar, built-in `/updates/shows` delta map. |
| **6c. Music Releases** *(Wave 2 Recommended)* | **ListenBrainz API** (`https://api.listenbrainz.org/1/explore/fresh-releases/?days=14`) + **MusicBrainz WS/2** (`https://musicbrainz.org/ws/2/release`, `/release-group`, Lucene date search) + **Cover Art Archive** (`https://coverartarchive.org/release-group/{mbid}/front-500.jpg`) + **Apple Marketing Tools RSS v2** (`music/most-played`) | Spotify Web API / Discogs API (keyed/OAuth) | **100% keyless & CC0 1.0**, but requires strict **1 req/sec** rate limiter, mandatory descriptive `User-Agent` (else `403`/`503`), fuzzy partial-date parsing (`YYYY`, `YYYY-MM`), and handling CAA 307/302 redirects to Internet Archive. |
| **6d. Video Games** *(Wave 3 Optional)* | **Steam Store** (`https://store.steampowered.com/api/featuredcategories?l=english&cc=US`, `/api/appdetails?appids={id}`, `/feeds/news.xml`) + **CheapShark v1.0** (`https://www.cheapshark.com/api/1.0/deals` — ToS mandates CheapShark redirect URLs) | **Steam Web API** (`IStoreService/GetAppList/v1/`, replacing deprecated `ISteamApps/GetAppList/v2`), **IGDB v4** (Twitch OAuth, 4 req/s, **non-commercial only**), **RAWG** (key + mandatory attribution backlink) | **Lowest keyless cleanliness:** Steam storefront API is undocumented (~200 req/5 min on `appdetails`, `coming_soon` items have `final_price: null`); IGDB is non-commercial OAuth; RAWG mandates backlinks. Worth including as Wave 3 after Tier 1–2 categories. |

### 1.3 Verified Reference GitHub Repositories
- **Multi-Category Trackers / Provider Abstraction:**
  - `https://github.com/FuzzyGrim/Yamtrack` — Python/Django unified tracker (Movies, TV, Anime, Manga, Books, Games) + `.ics` release calendars.
  - `https://github.com/IgnisDa/ryot` — Unified provider-adapter pattern isolating TMDB, AniList, MAL, MangaUpdates, IGDB, OpenLibrary behind a shared media schema.
  - `https://github.com/bonukai/MediaTracker` — Optional user-supplied metadata provider credentials alongside keyless fallbacks.
  - `https://github.com/Kometa-Team/Kometa` — Python cross-ID caching & metadata resolution (TMDB, IMDb, Trakt, AniDB, MAL).
- **Movie:** `https://github.com/celiao/tmdbsimple`, `https://github.com/AnthonyBloomer/tmdbv3api`, `https://github.com/fuzeman/trakt.py`, `https://github.com/cinemagoer/cinemagoer`, `https://github.com/Radarr/Radarr`.
- **Anime & Cross-ID Mapping:** `https://github.com/manami-project/anime-offline-database` (ODbL/DbCL), `https://github.com/Fribb/anime-lists` (MAL/AniList/Kitsu/AniDB/TMDB/IMDb/TVDB/LiveChart mapping), `https://github.com/1x6/xenylist`, `https://github.com/abhinavk99/jikanpy` (reference only).
- **Manga / Manhwa:** `https://github.com/AbstractUmbra/Hondana` (async Python MangaDex v5 client), `https://github.com/FourierMeow/manga-updates-api-client` (Python MangaUpdates v1 client).
- **Book:** `https://github.com/internetarchive/openlibrary-client`, `https://github.com/garethbjohnson/gutendex`, `https://github.com/bookwyrm-social/bookwyrm`, `https://github.com/darkestthewhite/calibre-web-hardcover`.
- **YouTube / RSS Polling:** `https://github.com/tubearchivist/tubearchivist`, `https://github.com/yt-dlp/yt-dlp`, `https://github.com/FreshRSS/FreshRSS`, `https://github.com/miniflux/v2`, `https://github.com/DIYgod/RSSHub`, `https://github.com/RSS-Bridge/rss-bridge`.
- **Podcasts / TV / Music / Games:** `https://github.com/Podcastindex-org`, `https://github.com/advplyr/audiobookshelf`, `https://github.com/AntennaPod/AntennaPod`, `https://github.com/Sonarr/Sonarr`, `https://github.com/sbondCo/Watcharr`, `https://github.com/Taxel/PlexTraktSync`, `https://github.com/metabrainz/listenbrainz-server`, `https://github.com/alastair/python-musicbrainzngs`, `https://github.com/Lidarr/Lidarr`, `https://github.com/beetbox/beets`, `https://github.com/rommapp/romm`, `https://github.com/JosefNemec/Playnite`, `https://github.com/ValvePython/steam`, `https://github.com/twitchtv/igdb-api-python`.

---

## Part 2: R2 — Codebase Constraints & Dashboard Gadget Gap Analysis

All findings below are verified against `d:\Project\Umwelt-OS`:

### 2.1 Backend Architecture & Constraints
1. **Modular-Monolith Enforcement (`core/modules.py`, `tests/unit/architecture/test_module_boundaries.py`):**
   - Cross-module imports must use `<module>.public` or `<module>.descriptor` only (enforced by `importlinter` via `.importlinter`). `*ReadProjection` aliases from `*.public` must never be written through.
2. **Closed Source & Provider Registration (`modules/sources/schemas.py`, `modules/connectors/`):**
   - `SourceCreate` (`modules/sources/schemas.py:15-37`, `extra="forbid"`) validates `type` against `modules.connectors.public.PROVIDER_SOURCE_TYPES`.
   - `CatalogEntry.category` (`modules/connectors/catalog.py:62`) and `FreeProviderSpec.category` (`modules/connectors/provider_specs.py:116`) only allow `("news", "economy", "hazards", "space", "security", "tech")` — no `"media"` category exists yet.
   - `DataShape` (`modules/connectors/data_shapes.py:14-26`) only allows `("news", "measurement", "event", "located", "messages", "video", "project_activity", "weather", "series", "job")`.
   - `PUT /api/v1/connectors/{source_id}/credentials/native-rest` (`modules/connectors/provisioning/_routes/activation.py:1085-1126`) enforces `source.type == "api"`. Therefore, **all keyed optional-API media providers MUST register with `source_type = "api"`** (never `"rss"`).
3. **Mandatory Gated Execution & `keyed.py` Single-GET Constraint (`modules/connectors/collection.py`, `_collection/base.py`, `providers/rest.py`, `providers/keyed.py`):**
   - `GATED_EXECUTOR_PROVIDERS` runs inside `SendGate` (commits `quota.debit_send()` before HTTP I/O, renews `ConnectorAdmissionSlot`, checks `AccessFence`/`SourceFence`/`terms_revision`).
   - `rest.fetch_bounded()` enforces `core.net_policy.is_public_address` DNS pinning, `MAX_REDIRECTS = 3` with sensitive-header stripping, `MAX_PAGE_BYTES = 10 MiB` / `MAX_TRANSPORT_BYTES = 25 MiB` decompression-bomb caps, `ETag`/`Last-Modified` conditional GET, and `429 Retry-After` deferral.
   - **`keyed.py` constraint (`modules/connectors/providers/keyed.py:1-14, 75-125`, `tests/unit/modules/connectors/test_keyed_providers.py:360-388`):** `keyed.py` supports ONLY a single static secret (`SecretRequest`) and single-request execution (`len(run.gate.calls) == 1`; OAuth token exchange is explicitly deferred). Therefore, OAuth2 token-exchange (`igdb_games`), two-secret HMAC-SHA1 (`podcast_index`), GraphQL `POST` (if needing multi-call pagination), or two-call `channels.list` + `playlistItems.list` (`youtube_api` with `@handle`) MUST be implemented as `SendGate`-backed native adapters (`_collection/native.py` or `_collection/providers.py`), NOT inside `keyed.KEYED`.
   - Existing `youtube` (`modules/connectors/providers/feed_catalog.py:96-184, 338-452`) currently fetches via `httpx.AsyncClient` in `_read_feed()` rather than `rest.fetch_bounded` inside `SendGate`, and validates a single `youtube_channel_id` (`^UC[A-Za-z0-9_-]{22}$`).
   - **XML parsing (`modules/connectors/providers/news.py:7-8, 299-314`):** `defusedxml` is **not** in `pyproject.toml`; existing XML/RSS parsing uses `safe_xml_root()` in `providers/news.py`.
4. **Strict Ingestion & Snapshot Metadata Gates (`modules/ingestion/_normalize_metadata.py`, `modules/knowledge/documents/schemas.py`):**
   - `ENVELOPE_REQUIRED_PROVIDERS` requires `record.metadata["provider_record"]` validated by `ProviderRecordMetadata` (`documents/schemas.py:593-652`).
   - `PROVIDER_IDS` (`documents/schemas.py:85-106`) and `_PROVIDER_SOURCE_FIELD_ALLOWLIST` (`documents/schemas.py:310-395`) strictly reject any unknown provider or unallowlisted `source_fields` key; `_validate_source_field` (`schemas.py:570-591`) currently validates scalar strings, ints, HTTPS links, and string lists (no `float` or `dict` branch yet).
   - No `observations` (`ck_observations_provider`) DB migration is required as long as media collectors emit document records with `world_data=None`.
5. **Scope-Hash vs. Option-Field Invariants (`modules/connectors/service/scope.py`, `service/config.py`, `provider_options.py`, `builtins.py`):**
   - `get_current_provider_scope()` (`scope.py:70-90`) reads `CatalogEntry.scope_fields` and returns `None` if any field is missing from both `configuration` and `OPTIONAL_SCOPE_FIELDS` (`scope.py:60-67`), causing `_current_document_components` (`_news.py:258-272`) to drop documents from projections! Furthermore, fields in `CatalogEntry.scope_fields` participate in `provider_scope_discriminator`, so editing them hides prior-scope documents. Filter dimensions (`countries`, `genres`, `languages`, `formats`, `release_year`, `anime_season`, `content_rating`, `min_rating`, `exclude_adult`) therefore belong in **`OPTION_FIELDS`** (`provider_options.py`) + `native_fingerprint()` rather than `CatalogEntry.scope_fields` (and any genuinely optional `scope_fields` entry MUST be registered in `OPTIONAL_SCOPE_FIELDS`).
   - All new `ConnectorConfig` (`service/schemas.py`) filter fields MUST default to `None` so `_NATIVE_ONLY_DEFAULTS` (`service/config.py:364-371`) is unaffected and generic RSS/Web/REST sources are not rejected by `registry._validate_generic_source` (`registry.py:184-255`). Fixed-scope `BUILTIN_PROVIDERS` options must be mirrored on `BuiltinEnableRequest` (`builtins.py:127-197`).
6. **`Document.language` (`FEED_LANGUAGES`) & Missing `provenance_json` GIN Index (`modules/knowledge/documents/_language.py`, `models.py`, `_news.py`):**
   - `Document.language` (`_language.py:4-22`, `_normalize.py:455`) accepts ONLY the 13 primary subtags in `FEED_LANGUAGES` (`en, vi, fr, de, es, pt, it, ru, ja, ko, zh, id, th`), storing `NULL` for anything else, and is populated only when `record.metadata["language"]` is set. Keeping existing `language: ^[a-z]{2}$` (`routes.py:257`) untouched while adding `languages: list[str] | None` querying `provenance_json["provider_record"]["source_fields"]["language"].astext` preserves backwards compatibility with `test_feed_filters.py`.
   - Checked `models.py` and migrations `0001`–`0023`: `normalized_version_provenance.provenance` (`JSONB`) has **no GIN index** (only `document_chunks.content_tsv` has a GIN index). Projection filters on `source_fields` are already bounded to <=32 active sources via `Document.source_id.in_(source_ids)` (`ix_documents_source_id`), and an optional `CONCURRENTLY` GIN migration (`core.migration_support.create_index_concurrently`) is noted if library scale warrants it.
7. **CI Doc-Inventory & Guard Tests:**
   - `tests/unit/test_provider_docs_inventory.py` requires 1:1 sync between `FREE_PROVIDER_SPECS` / `list_catalog()` / `DISPATCHABLE` and `docs/connectors/provider-catalog.md` + `docs/connectors/free-sources.md`.
   - `tests/unit/modules/connectors/test_free_provider_dispatch.py`, `test_data_shapes.py`, `test_keyed_providers.py`, `test_wizard_payloads.py` (`SCOPED & OFFERED_NATIVE` vs `MINIMAL_SCOPE`), `test_wave_f_options.py`, and `tests/unit/modules/knowledge/documents/test_provider_snapshot_fields.py` enforce dispatchability, non-empty data shapes, keyed registry parity, wizard payload validation, and snapshot field validation.

### 2.2 Dashboard Gadget & Frontend Capability Gaps
1. **Closed Projection, Scope & Filter Schemas (`modules/dashboard/`, `modules/knowledge/documents/`, `apps/web/`):**
   - `GadgetScope` and `GadgetFilters` (`modules/dashboard/schemas.py:42-126`) inherit `StrictConfiguration` (`extra="forbid"`), preventing `media_types`, `countries`, `genres`, `languages`, `formats`, `content_ratings`, `release_year`, `release_season`, `min_rating`, `release_status`, `release_window_days`, or `group_by`. Note `GadgetScope.ensure_unique_selectors` (`schemas.py:99-105`) automatically enforces uniqueness on every `list` field. Mirroring TypeScript types in `apps/web/src/modules/dashboard/api.ts:42-94` also gate filters via `unsupportedListFilters` (`api.ts:89-94`).
   - `validate_renderer_configuration` (`modules/dashboard/gadgets.py:474-534`) restricts non-map/non-metric `GadgetScope` fields to `{"source_item_ids"}` and currently only guards map-only `GadgetFilters` fields; media-only `GadgetFilters` fields must similarly be rejected on non-media renderers.
   - `GadgetDocumentProjectionRead` (`modules/knowledge/documents/schemas.py:979-999`; TypeScript `GadgetDocumentProjection` in `apps/web/src/modules/knowledge/api.ts:72-108`) has no typed `media` object (unlike `telegram`), and `listGadgetDocumentProjections` (`knowledge/api.ts:228-241`) currently only serializes `limit`, `language`, `since`, `cursor`, `source_ids`, and `channel_ids`.
   - `list_news_document_projections` (`modules/knowledge/documents/_news.py:626-633`) paginates strictly by `(Document.created_at, Document.id) DESC` with a keyset cursor (`_encode_news_projection_cursor`), so an ascending upcoming `release_date` calendar view requires a dedicated bounded query/sort mode rather than reusing the `DESC` cursor directly, and all filter/sort parameters must enter `_news_projection_cursor_fingerprint()` (`_news.py:1004-1025`).
2. **CSP & Hidden Remote Media Toggle (`apps/web/src/core/csp.ts:67`, `core/media_proxy.py:86-226`, `apps/web/src/modules/settings/ai-settings.tsx:411-419`):**
   - `img-src 'self' data: blob: ${OPENFREEMAP_ORIGIN}` blocks direct external poster/cover `<img>` URLs.
   - `core/media_proxy.py:check_feed_image_url` + `make_image_signer(kind="feed")` already proxy any public HTTPS `source_fields["image_url"]` through `/api/v1/media/proxy?token=...` — **no `PROXY_HOSTS` edit needed**, EXCEPT `allow_widget_remote_media` defaults to `false` inside a collapsed `<details>` in `/settings/models` (`#ai-widget-media`, `ai-settings.tsx:411-419`), causing silent missing artwork unless gadgets surface a helpful link.
   - Note on redirects/HTTPS: Google Books `http://` thumbnails must be upgraded to `https://` by the collector, and Cover Art Archive `/front` 302/307 redirects should be resolved or stored as final HTTPS URLs compatible with `check_feed_image_url`.
3. **Hardcoded Aspect Ratios, Missing Media UI, Source Option Gaps & UI Primitive Inventory:**
   - `FeedImage` (`apps/web/src/components/feed-image.tsx:9-18`) and `VideoThumb` (`apps/web/src/modules/dashboard/gadgets/video-panel.tsx:20-29`) hardcode `aspect-video` (16:9), cropping 2:3 movie/anime/manga/book covers and 1:1 album/podcast covers.
   - `TEMPLATES["feed"]` (`modules/dashboard/gadgets.py:397-420`) points to `news_feed` (`NewsFeed` story clustering), not raw `FeedStream`.
   - `VideoPanel` (`video-panel.tsx:38-113`) hardcodes `provider === 'youtube'` and `youtube.com`/`youtu.be`, has no cursor pagination (only `ShowMore` on 5 items), no published timestamp, no duration/channel badge, no search/tabs, no read/bookmark state, and no 1-click Ask AI.
   - `TimelineGadget` (`timeline-gadget.tsx:126-214`) binds 1 source only and shows past events without release calendar buckets or poster thumbnails.
   - Single-item row "Ask AI" in `FeedStream` (`feed-parts.tsx:43`) is restricted to `ctx.isTelegram`.
   - `YoutubeScope` (`apps/web/src/modules/sources/provider-scope.tsx:244-258`) allows only a single `UC...` channel ID.
   - `EnumOption` (`apps/web/src/modules/sources/builtin-options.tsx:84-108`) ignores `spec.labelled` and calls `humanize(item)` instead of `optionValueKey(item)` (unlike `MultiOption` at line 62). Also, `Command` (`cmdk`), `combobox.tsx`, `slider.tsx`, `accordion.tsx`, and `scroll-area.tsx` are **NOT installed** in `apps/web/src/components/ui/` — searchable multi-select filters must compose `SearchableSelect`, `Popover` + `Input` + `Checkbox`, or `DropdownMenuCheckboxItem`.
4. **Strict UI Design System, Registry Snapshots & i18n Static AST Gates:**
   - `apps/web/src/app/globals.css:32`: `bg-accent` is hover surface `--active`; data accent is `bg-data` / `text-data`. Semantic tokens only, Lucide icons only, sentence case, zero emoji.
   - Backend `modules/dashboard/gadgets.py:22-40, 427-447` uses `RendererDescriptor` + separate `DEFAULT_SIZES` (`tests/unit/modules/dashboard/test_default_sizes.py`), mirrored by `RENDERER_MIN_SIZES` / `RENDERER_DEFAULT_SIZES` (`apps/web/src/modules/dashboard/grid-geometry.ts`), `GADGET_READING_BODY_FLOORS` (`widget-registry.tsx:103`), and `widget-registry.lazy.test.tsx:10-33`.
   - `apps/web/locales/catalog-parity.test.ts` enforces 1:1 `en-us`/`vi-vi` parity; `apps/web/locales/key-usage.test.ts` parses TS/TSX AST and **forbids dynamic template-literal keys** (must use `as const` lookup maps).
   - `apps/web/src/modules/dashboard/gadget-editor.test.tsx` and `tests/unit/modules/dashboard/test_metric_catalog_snapshot.py` verify parity against `apps/web/src/test/fixtures/gadget-renderers.json` and `apps/web/src/test/fixtures/gadget-templates.json`.

---

## Part 3: R3 — Technical Implementation Plan

### 3.1 Proposed Provider Catalog & Scope Design

Implement new media providers in clean waves (**Wave 1** covering all 5 requested categories + Podcasts + TV Series, and **Wave 2/3** covering Music Releases and Video Games):

#### A. Keyless-First Providers (`key_required=False`)
| Provider ID | Category / Domain | Source Type | Scope Type | Upstream Official Endpoint(s) | Poll Floor |
|---|---|---|---|---|---|
| `wikidata_movies` | Movie (Upcoming & Recent) | `api` | Scoped (`genre_qids`, `language`, `lookback_days`) | `https://query.wikidata.org/sparql` | 360 min |
| `itunes_movies` | Movie (Search / Watchlist) | `api` | Scoped (`search_terms`, `country`) | `https://itunes.apple.com/search?media=movie&entity=movie` | 360 min |
| `letterboxd_rss` | Movie (User Diary/Reviews) | `rss` | Scoped (`username`) | `https://letterboxd.com/{username}/rss/` | 60 min |
| `anilist_anime` | Anime (Catalog, Seasonal, Airing Schedule) | `api` | Scoped (`mode: airing\|trending\|updated`, `season`, `year`, `media_ids`, `exclude_adult`) | `https://graphql.anilist.co` | 30 min |
| `livechart_episodes` | Anime (24h Episode Airings RSS) | `rss` | Fixed (`BUILTIN_PROVIDERS`) | `https://www.livechart.me/feeds/episodes` | 30 min |
| `ann_news` | Anime/Manga (Official News Atom) | `rss` | Fixed (`BUILTIN_PROVIDERS`) | `https://www.animenewsnetwork.com/news/atom.xml` | 60 min |
| `anilist_manga` | Manga / Manhwa / Manhua | `api` | Scoped (`country_of_origin: JP\|KR\|CN\|TW\|ALL`, `mode: updated\|trending`, `exclude_adult`) | `https://graphql.anilist.co` | 60 min |
| `mangaupdates_releases` | Manga / Manhwa (Chapters & Licensing) | `api` | Scoped (`series_ids`, `types: Manga\|Manhwa\|Manhua`, `licensed_only`) | `https://api.mangaupdates.com/v1/releases` & `/v1/series/search` | 60 min |
| `mangadex_releases` | Manga / Manhwa (Keyless Metadata & Chapters) | `api` | Scoped (`original_languages: [ko,ja,zh]`, `content_rating`, `manga_ids`) | `https://api.mangadex.org/manga` & `/chapter` | 60 min |
| `openlibrary_books` | Book (Search, Subjects, Recent Changes) | `api` | Scoped (`mode: subject\|search\|recent`, `subject`, `query`, `ebooks_only`) | `https://openlibrary.org/subjects/{subject}.json`, `/search.json`, `/recentchanges.json` | 360 min |
| `gutendex_books` | Book (Public Domain Ebooks) | `api` | Scoped (`languages`, `topic`, `search`) | `https://gutendex.com/books` | 1440 min |
| `apple_books_rss` | Book & Audiobook Charts | `api` | Scoped (`country`, `kind: books\|audio-books`, `chart`) | `https://rss.marketingtools.apple.com/api/v2/{country}/{kind}/...` | 360 min |
| `youtube` *(upgraded)* | YouTube New Releases (Atom RSS) | `rss` | Scoped (`youtube_channel_id` OR `youtube_channels` / `youtube_handles` / `youtube_playlist_id`, `youtube_filter: all\|long_form\|shorts`) | `https://www.youtube.com/feeds/videos.xml?channel_id=...` / `?playlist_id=UULF...` | 15 min |
| `podcast_rss` *(6a)* | Podcasts (Any RSS 2.0 / iTunes / Podcasting 2.0 Feed, with iTunes Search helper) | `rss` | Scoped (`feed_url` or `itunes_podcast_id`) | `<feedUrl>` + `https://itunes.apple.com/lookup?id=...` | 60 min |
| `tvmaze_schedule` *(6b)* | TV Series / Episodes (Schedule & Updates) | `api` | Scoped (`mode: schedule_web\|schedule_country\|followed_shows`, `country`, `show_ids`) | `https://api.tvmaze.com/schedule/web`, `/schedule`, `/updates/shows` | 60 min |
| `listenbrainz_releases` *(6c - Wave 2)* | Music Releases (Fresh Releases + CAA Covers) | `api` | Scoped (`days`, `sort`, `username`) | `https://api.listenbrainz.org/1/explore/fresh-releases/` + `https://coverartarchive.org` | 360 min |
| `steam_releases` *(6d - Wave 3)* | Video Games (New Releases, Coming Soon, News RSS) | `api` | Scoped (`mode: featured\|news`, `country`, `appids`) | `https://store.steampowered.com/api/featuredcategories`, `/api/appdetails`, `/feeds/news.xml` | 360 min |

#### B. Optional API-Key Providers (`key_required=True`, `source_type="api"`, `ConnectorRestCredential` / Native Adapter)
| Provider ID | Category / Domain | Execution Path & Auth Placement | Unlocked Capabilities | Poll Floor |
|---|---|---|---|---|
| `tmdb_movies` | Movie (`now_playing`, `upcoming`, `/movie/changes`) | `keyed.py` (`header` Bearer v4 or `query:api_key`, single-GET per endpoint) | High-res posters/backdrops, localized synopses, ratings, `/changes` delta-sync, upcoming calendar | 360 min |
| `tmdb_tv` *(6b)* | TV Series (`airing_today`, `on_the_air`) | `keyed.py` (`header` / `query:api_key`) | Episode stills, localized metadata, TMDB TV calendar | 360 min |
| `trakt_media` | Movie & TV (Trending/Anticipated/Calendars) | `keyed.py` (`header:trakt-api-key`, static header `trakt-api-version: 2`) | Community trending, multi-ID resolution (`imdb`/`tmdb`/`trakt`) | 360 min |
| `omdb_lookup` | Movie (Ratings Enrichment) | `keyed.py` (`query:apikey`) | IMDb, Rotten Tomatoes, Metacritic scores | 1440 min |
| `mal_anime` / `mal_manga` | Anime & Manga (Official MAL v2) | `keyed.py` (`header:X-MAL-CLIENT-ID`) | Official MAL seasonal rankings & canonical MAL catalog | 360 min |
| `google_books` | Book (ISBN/Query Volumes) | `keyed.py` (`query:key`) | Commercial descriptions, page counts, ISBN-10/13 | 1440 min |
| `hardcover_books` | Book (GraphQL Catalog & Upcoming) | `SendGate`-backed native adapter (`_collection/providers.py`, Bearer `authorization`, GraphQL `POST`) | Modern book catalog, series, upcoming release dates | 360 min |
| `youtube_api` | YouTube Data API v3 (`playlistItems.list` / `channels.list`) | `SendGate`-backed native adapter (`_collection/native.py` for `@handle` -> `channels.list` + `playlistItems.list` with ETag `304`), OR `keyed.py` single-GET if scoped directly by `UC...` -> `UU...` | >15-item upload history, `@handle` lookup (1 unit), `duration`, ETag `304`, no datacenter RSS 404s | 30 min |
| `podcast_index` *(6a)* | Podcasts (Podcast Index v1.0) | `SendGate`-backed native adapter (`_collection/native.py`, 2-secret `apiKey` + `apiSecret` -> `X-Auth-Key` + `X-Auth-Date` + SHA-1 `Authorization`) | `newestSince` delta, Podcasting 2.0 transcripts/chapters | 60 min |
| `igdb_games` *(6d - Wave 3)* | Video Games (IGDB v4) | `SendGate`-backed native adapter (`_collection/native.py`, Twitch OAuth2 `client_credentials` token exchange + Apicalypse `POST`) | Cross-platform game release calendar & covers (non-commercial) | 360 min |

*Note on Movie Keyless UX & Attribution:* Because keyless movie sources have trade-offs (Wikidata SPARQL is best for recent/upcoming structured films; Letterboxd RSS is per-user; iTunes is search-based), the Sources UI catalog entry for `tmdb_movies` and `wikidata_movies` will clearly display what is keyless vs. what an optional TMDB key unlocks, and display required attribution badges (`TMDB`, `TVMaze CC BY-SA 4.0`, `MangaDex`, `MangaUpdates`) in UI metadata.

#### C. Multi-Dimensional Filter Scope Architecture (`provider_options.py`, `ConnectorConfig`, `service/scope.py`, `builtins.py`, `provider-scope.tsx`, `builtin-options.tsx`)

1. **Canonical Filter Option Types (`modules/connectors/provider_options.py`):**
   - **Countries (`MediaCountry` / `IsoCountry`):** Reuse/extend `IsoCountry` (`provider_options.py:135-160`, `ISO_COUNTRY_PATTERN = r"^[A-Z]{2}$"` at line 345) covering ISO 3166-1 alpha-2 codes (`US`, `JP`, `KR`, `CN`, `TW`, `VN`, `GB`, `FR`, `DE`, `IN`, `SG`, `AU`, `CA`, `BR`, `ID`, `TH`, `MY`, `PH`, `IT`, `ES`, `RU`, `MX`, `HK`, etc.).
   - **Genres (`MediaGenre`):** Curated canonical `Literal[...]` taxonomy: `"action"`, `"adventure"`, `"sci_fi"`, `"fantasy"`, `"romance"`, `"thriller"`, `"horror"`, `"comedy"`, `"drama"`, `"mystery"`, `"documentary"`, `"animation"`, `"slice_of_life"`, `"history"`, `"music"`, `"sports"`, `"non_fiction"`, `"programming"`, `"rpg"`, `"strategy"`.
   - **Languages (`MediaLanguage`):** Curated ISO 639-1 / BCP-47 `Literal[...]` (`"en"`, `"vi"`, `"ja"`, `"ko"`, `"zh"`, `"zh-hk"`, `"fr"`, `"de"`, `"es"`, `"pt"`, `"it"`, `"ru"`, `"id"`, `"th"`).
   - **Sub-types / Formats (`MediaFormat`):** Curated `Literal[...]` spanning anime (`"tv"`, `"tv_short"`, `"movie"`, `"special"`, `"ova"`, `"ona"`, `"music"`), manga/books (`"manga"`, `"manhwa"`, `"manhua"`, `"novel"`, `"one_shot"`, `"ebook"`, `"audiobook"`, `"magazine"`), TV (`"scripted"`, `"animation"`, `"reality"`, `"talk_show"`, `"documentary"`), music (`"album"`, `"single"`, `"ep"`), podcasts (`"episodic"`, `"serial"`), and games (`"main_game"`, `"dlc"`, `"expansion"`, `"remake"`, `"remaster"`).
   - **Season (`AnimeSeason`):** `Literal["winter", "spring", "summer", "fall"]` (uppercased for AniList GraphQL `WINTER|SPRING|SUMMER|FALL`).
   - **Content Rating (`ContentRating`):** `Literal["safe", "pg", "pg_13", "r", "suggestive", "mature", "explicit", "clean"]`.
   - **Bounded Scalars:** `release_year: StrictInt` (`ge=1900, le=2100`), `min_rating: float` (`ge=0.0, le=100.0`, finite), `exclude_adult: StrictBool`.
   - **Validation & Determinism:** Register per-provider allowed keys in `OPTION_FIELDS` (`provider_options.py:348-377`), enforce list uniqueness and per-provider caps inside `validate_options(provider, cfg)` (`provider_options.py:468-520`), sort multi-selects deterministically with `_ordered()`, assemble native query parameters in `build_url()` / `KeyedProvider.build()` (returning `None` when options equal default behavior), and filter post-fetch in `filter_records()` (`provider_options.py:921-985`) where upstream APIs lack native filter params.

2. **`ConnectorConfig` Field Types & `_NATIVE_ONLY_DEFAULTS` Guard (`modules/connectors/service/schemas.py` & `service/config.py`):**
   - Multi-select options use `tuple[Enum, ...] | None = Field(default=None, min_length=1, max_length=...)` (matching `eonet_categories`, `job_cities` at `schemas.py:307-341`; Pydantic v2 coerces JSON arrays to `tuple` and `model_dump(mode="json")` emits JSON `list`).
   - Single-value enums use `Literal[...] | None = None` (`anime_season`, `youtube_filter`, `media_country`); integers use `StrictInt | None = Field(default=None, ge=1900, le=2100)` (`release_year`); ratings use `float | None = Field(default=None, ge=0.0, le=100.0)` (`min_rating`); booleans use `StrictBool | None = None` (`exclude_adult`).
   - **Critical Default Rule:** Every new `ConnectorConfig` filter field **MUST default to `None`** so `_NATIVE_ONLY_DEFAULTS` (`modules/connectors/service/config.py:364-371`) does not need expansion and generic RSS/Web/REST sources never serialize these keys or fail `registry._validate_generic_source` (`registry.py:184-255`, which also rejects `ALL_OPTION_NAMES` and any new `PROVIDER_SCOPE_FIELDS`).

3. **Architectural Placement Rule — `OPTION_FIELDS` vs. `CatalogEntry.scope_fields` (`modules/connectors/service/scope.py`, `builtins.py`, `builtin-options.tsx`):**
   - **Scope-Hash Invariant (`scope.py:51-90`):** `get_current_provider_scope()` computes `provider_scope_discriminator` from `CatalogEntry.scope_fields`, and `_scope_values` (`scope.py:86`) returns `None` (causing `_current_document_components` in `_news.py:258-272` to hide all ingested documents!) if any field in `scope_fields` is absent and not listed in `OPTIONAL_SCOPE_FIELDS` (`scope.py:60-67`).
   - **Placement Decision:** Keep *identifying source targets* (`youtube_channel_id`/`youtube_handles`/`youtube_playlist_ids`, `feed_url`, `username`, `show_ids`, `manga_ids`, `series_ids`) in `PROVIDER_SCOPE_FIELDS` / `CatalogEntry.scope_fields` (registering any optional target field in `OPTIONAL_SCOPE_FIELDS` and either/or sets in `_ALTERNATIVE_SCOPE_FIELDS` at `scope.py:52-55`, while keeping `MINIMAL_SCOPE` parity for `tests/unit/modules/connectors/test_wizard_payloads.py:186-190`). Place **filter dimensions** (`media_countries`, `media_genres`, `media_languages`, `media_formats`, `release_year`, `anime_season`, `content_ratings`, `min_rating`, `exclude_adult`) in **`OPTION_FIELDS`** (`provider_options.py`) + `native_fingerprint()` (`provider_options.py:843-877`) instead of `CatalogEntry.scope_fields`. That way, changing a country/genre/rating filter resets the collection cursor cleanly without invalidating `provider_scope_discriminator` on already-collected documents!
   - **Fixed-Scope Built-in Presets (`builtins.py:127-197` & `builtin-options.tsx:84-108`):** Any `OPTION_FIELDS` exposed on fixed-scope `BUILTIN_PROVIDERS` (such as `livechart_episodes`, `ann_news`) MUST be mirrored with identical bounded types on `BuiltinEnableRequest` (`builtins.py:127-197`) and registered in `OPTION_SPECS` (`builtin-option-specs.ts`) + `OPTION_LABEL_KEY` / `OPTION_VALUE_KEY` (`builtin-option-keys.ts`). Update `EnumOption` (`apps/web/src/modules/sources/builtin-options.tsx:84-108`) to support `spec.labelled ? optionValueKey(item) : humanize(item)` (matching `MultiOption` at line 62) so enum dropdowns localize cleanly in `en-us` and `vi-vi`. Note also that `ScheduleStep` (`connector-editor/schedule-step.tsx:85-89`) converts emptied arrays/`undefined` to explicit `null`, whereas `BuiltinWizard` (`builtin-sources.tsx:248-265`) omits them.

4. **Frontend Scoped Source UI (`apps/web/src/modules/sources/provider-scope.tsx`, `scope-fields.tsx`, `connector-editor-model.ts`, `guide-keys.ts`, `provider-label-keys.ts`):**
   - Register each new scoped media provider in `NativeProvider` (`provider-scope.tsx:16-38`) and `SCOPE_SECTIONS` (`provider-scope.tsx:658-681`), add `'media'` to `BuiltinCategory` (`sources/api.ts:262`), `PROVIDER_GROUP_ORDER`/`PROVIDER_GROUP_KEY` (`connector-editor-model.ts:94-104`), and `CATEGORY_ORDER`/`CATEGORY_KEY` (`builtin-sources.tsx:34-42`), and populate `PROVIDER_KEY`, `PROVIDER_LABEL_KEY`, `FIELD_LABEL_KEYS`, and `SCOPE_HINT_KEY` (`guide-keys.ts:44-83`).
   - Build a shared reusable **`MediaFilterFields`** component in `scope-fields.tsx` (reusing `Select`, `Checkbox` multi-grids, `Input`, and `Popover`-based searchable multi-select chips with `Badge` + `BareButton`) so scoped media panels expose Country, Genre, Language, Format, Release Year/Season, Content Rating, and Min Rating without duplicating form code. Keep optional filter fields outside `REQUIRED_SCOPE` (`connector-editor-model.ts:30-56`) so `scopeGate` (`use-connector-wizard.ts:63-68`) does not block wizard progression when left unfiltered.

#### D. Per-Provider Filter Pushdown vs. Post-Fetch (`filter_records`) Matrix & Spec Gotchas

| Provider ID | Server-Side Pushdown Parameters (`build_url` / `KeyedProvider.build`) | Post-Fetch `filter_records()` & `_MEDIA_FIELDS` Normalization | Verified Spec Gotchas & Edge Cases |
|---|---|---|---|
| `tmdb_movies` | `with_origin_country` (ISO2), `with_genres` (Movie IDs: `,`=AND, `\|`=OR), `with_original_language` (ISO 639-1), `language` (BCP-47), `region`, `primary_release_year`, `primary_release_date.{gte,lte}`, `release_date.{gte,lte}`, `certification_country` + `certification[.gte\|lte]`, `vote_average.gte`, `include_adult` | Normalize `rating = vote_average * 10`, `country` from `origin_country[0]` / `production_countries[0].iso_3166_1`, canonical `genres[]` | **Movie vs TV IDs differ** (Movie splits Action `28`/Adventure `12`/Fantasy `14`/Sci-Fi `878`); `certification` requires `certification_country` to take effect; mandatory TMDB attribution. |
| `tmdb_tv` | `with_origin_country`, `with_genres` (TV IDs), `with_original_language`, `first_air_date_year`, `first_air_date.{gte,lte}`, `air_date.{gte,lte}`, `vote_average.gte`, `include_adult` | Normalize `rating = vote_average * 10`, `country = origin_country[0]`, canonical `genres[]` | **TV merges Action & Adventure (`10759`) and Sci-Fi & Fantasy (`10765`)**. |
| `trakt_media` | `countries` (ISO2 csv, OR), `genres` (slug csv, OR), `languages` (ISO 639-1 csv), `years` (`YYYY` or range), `ratings` (`0-100`), `tmdb_ratings` (`0.0-10.0`), `certifications` | Normalize `rating = rating * 10`, canonical `genres[]` | Uses **hyphenated** `science-fiction` slug (not `science_fiction`). |
| `omdb_lookup` | `i` / `t` / `s`, `type` (`movie\|series\|episode`), `y` (`YYYY`) — **no country/genre/lang/rating params** | `filter_records()` on `country`/`genres`/`language`/`min_rating`; map `Country` (`"USA"`->`US`, `"UK"`->`GB`), `imdbRating * 10` | 1,000 req/day free; `Country` & `Language` are full English names in comma strings. |
| `itunes_movies` | `term`, `country` (ISO2), `media=movie`, `entity=movie`, `lang`, `limit` (<=200), `genreId` (int) | `filter_records()` on `min_rating` / `release_year` / `genres` | `country` storefront param; ~20 req/min. |
| `wikidata_movies` | SPARQL `wdt:P495/wdt:P297` (ISO2), `wdt:P136` (genre QID), `wdt:P364/wdt:P218` (ISO 639-1), `wdt:P577` (date range) | Normalize `?countryCode`, `?langCode`, `?genreLabel` -> canonical slugs | Mandatory descriptive `User-Agent`, 60s timeout. |
| `letterboxd_rss` | Path `{username}` only — **zero server-side filters** | `filter_records()` on `release_year` (`<letterboxd:filmYear>`), `min_rating` (`memberRating * 20`); country/genre/lang unmatched unless enriched | 50-item rolling cap; RSS lacks country/genre/language elements. |
| `anilist_anime` | `type: ANIME`, `countryOfOrigin` (ISO2), `genre_in`, `tag_in`, `format_in` (`TV\|TV_SHORT\|MOVIE\|SPECIAL\|OVA\|ONA\|MUSIC`), `season` (`WINTER\|SPRING\|SUMMER\|FALL`), `seasonYear`, `status_in`, `isAdult`, `averageScore_greater` (0–100) | Normalize `averageScore` (already 0–100), `countryOfOrigin`, `format`, `season`, `genres[]` | **Default `isAdult: false`** (omitting `isAdult` can return adult titles or fail unauthenticated if `true`); `genre_in` uses OR; `90 req/min` (drops ~30 under load). |
| `anilist_manga` | `type: MANGA`, `countryOfOrigin` (`JP\|KR\|CN\|TW`), `genre_in`, `tag_in`, `format_in` (`MANGA\|NOVEL\|ONE_SHOT`), `status_in`, `isAdult`, `averageScore_greater` | Same as `anilist_anime`, inferring manga/manhwa/manhua `media_type` from `countryOfOrigin` | `countryOfOrigin` cleanly distinguishes `JP` (Manga), `KR` (Manhwa), `CN`/`TW` (Manhua). |
| `livechart_episodes` | Fixed RSS URLs (`/feeds/episodes`, `/feeds/headlines`) — **zero server-side filters** | `filter_records()` on keywords/title; limited metadata in RSS | `If-Modified-Since` 304 support. |
| `ann_news` | Atom path category (`news\|review\|feature\|interview\|press-release\|all`), `?ann-edition=us\|w`; Encyclopedia XML `type=anime\|manga` | Atom `<category term="...">` normalized to `genres`/`tags` | Encyclopedia XML strict **1 req/sec**, non-bot `User-Agent`. |
| `mal_anime`, `mal_manga` | `ranking_type`, `/anime/season/{year}/{season}`, `nsfw` (`true\|false`) — **no genre/country/score query params** | `filter_records()` on `?fields=genres,media_type,status,rating,mean,nsfw,start_date`; normalize `mean * 10`, infer `country` from `media_type` (`manhwa`->`KR`, `manhua`->`CN`) | Header `X-MAL-CLIENT-ID`; <=1 req/s recommended. |
| `mangaupdates_releases` | `POST /v1/series/search`: `stype` (`Manga\|Manhwa\|Manhua\|Novel\|Doujinshi`), `filter_types[]`, `genre[]`, `exclude_genre[]`, `licensed`, `year` | `/v1/releases` has **no server filters** -> `filter_records()` + series metadata enrichment (`type`->`country`, `bayesian_rating * 10`) | `POST /v1/series/search` JSON body vs `GET /v1/releases`; mandatory attribution; integer `series_id`. |
| `mangadex_releases` | `originalLanguage[]` (`ja\|ko\|zh\|zh-hk`), `availableTranslatedLanguage[]`, `includedTags[]`, `includedTagsMode=OR`, `excludedTags[]`, `publicationDemographic[]`, `contentRating[]`, `status[]`, `year`, `updatedAtSince` | Map `originalLanguage` (`ja`->`JP`, `ko`->`KR`, `zh`->`CN`, `zh-hk`->`TW`) to `country` + tag UUIDs to canonical `genres[]` | **Separate `zh` vs `zh-hk`**; **`includedTagsMode` defaults to `AND`** so multi-genre MUST pass `includedTagsMode=OR`; proxy covers (no hotlinking); 5 req/s + `User-Agent`. |
| `openlibrary_books` | `/search.json`: `q`, `subject`, `language` (ISO 639-2/B e.g. `eng`, `jpn`, `fre`, `vie`), `first_publish_year`, `ebook_access`; `/subjects/{slug}.json`: `ebooks`, `published_in` | Normalize 3-letter ISO 639-2/B -> 2-letter ISO 639-1 on ingestion; `ratings_average * 20` | **Query must map 2-letter -> 3-letter (`en`->`eng`, `fr`->`fre`, `ja`->`jpn`, `vi`->`vie`)**; `User-Agent` raises limit 1->3 req/s. |
| `gutendex_books` | `languages` (ISO 639-1 csv), `topic` (subject/bookshelf substr), `copyright`, `mime_type`, `author_year_start`, `author_year_end` | Normalize `subjects`/`bookshelves` -> canonical `genres[]` | No book publication year or country field; pagination via `next` URL. |
| `apple_books_rss` | URL path `{country}` (ISO2), `{media-type}` (`books\|audio-books\|music\|podcasts`), `{chart}`, `{limit}` (`10\|25\|50\|100`) | `filter_records()` on `feed.results[].genres[].name`, `contentAdvisoryRating`, `releaseDate` | **Canonical host `rss.marketingtools.apple.com`** (`rss.applemarketingtools.com` 301-redirects, wasting 1 of `MAX_REDIRECTS=3`); **no v2 URL genre filter**; `limit > 100` errors. |
| `hardcover_books` | Hasura GraphQL `books(where: books_bool_exp)`: `release_date`, `release_year`, `cached_tags`, `pages`, `rating`, `audio_seconds` | Normalize `rating * 20`, `cached_tags` -> `genres[]` | Introspect Hasura `books_bool_exp` against live schema before finalizing query; 60 req/min, 5,000/day. |
| `google_books` | `q` (`subject:`, `intitle:`, `inauthor:`), `langRestrict` (ISO 639-1), `printType` (`all\|books\|magazines`), `filter` (`ebooks\|free-ebooks\|paid-ebooks`) | Normalize `averageRating * 20`, `accessInfo.country`, `categories` -> `genres[]` | **`subject:` MUST be inside `q=`** (NOT top-level param); **`imageLinks` often `http://`** -> upgrade to `https://` for `check_feed_image_url`. |
| `youtube` (Atom) | `channel_id=UC...` or `playlist_id=PL...\|UULF...\|UUSH...` — **no country/genre/lang filters** | `filter_records()` on `starRating * 20`, keywords | 15-item cap; `@handle` -> 400 (resolve via canonical discovery). |
| `youtube_api` | `channels.list` (`forHandle`/`id`), `playlistItems.list`, `videos.list` (`chart=mostPopular&regionCode={ISO2}&videoCategoryId={id}`) | Map `snippet.{defaultAudioLanguage, defaultLanguage}`, `channels.snippet.country`, `videoCategories` | Avoid `search.list` (100 units); `channels`/`playlistItems`/`videos` = 1 unit each (10,000/day). |
| `podcast_rss` | Feed URL has **no filters** (iTunes discovery helper supports `country`, `lang`, `genreId`) | `filter_records()` on `<language>`, `<itunes:category>`, `<itunes:explicit>`, `<itunes:type>` (`episodic\|serial`) | `ETag`/`Last-Modified` 304; `<itunes:duration>` -> `duration_seconds`. |
| `podcast_index` | `lang` (ISO 639-1 csv + `unknown`), `cat` (names/IDs), `notcat`, `since`, `val` | Map `categories` IDs -> canonical `genres[]`, `explicit` -> `content_rating` | 2-secret SHA-1 `Authorization` + `X-Auth-Key` + `X-Auth-Date`; clock skew causes 401. |
| `tvmaze_schedule` | `/schedule`: `country` (ISO2), `date`; `/schedule/web`: `date`, `country` (`""`=global, ISO2=local, omitted=all) — **no genre/lang/rating params** | `filter_records()` on `show.{genres, language, type, rating.average * 10}`; map full language name (`"English"`->`en`) | **Strictly `GB` (not `UK`)**; `/schedule/web?country=` empty string = global channels only; CC BY-SA 4.0 attribution mandatory. |
| `listenbrainz_releases` | ListenBrainz `fresh-releases`: `release_date`, `days` (<=90), `sort` (**no country/genre/lang/type**). MusicBrainz WS/2 Lucene: `primarytype`, `secondarytype`, `country`, `lang` (ISO 639-3), `tag`, `date`, `status` | `filter_records()` on MB `country`, `text-representation.language` (3->2 letter), `genres[]`/`tags[]` | MB strict **1 req/sec** + mandatory `User-Agent`; CAA 302/307 redirects. |
| `steam_releases` | `cc` (ISO2 storefront/currency), `l` (Steam language name e.g. `english`, `vietnamese`, `japanese`, `schinese`) — **no genre/year/rating params** | `filter_records()` on `appdetails` `genres[].description`, `metacritic.score` (0–100), `required_age`, `release_date` | `coming_soon` items have `final_price: null` and fuzzy date strings (`"Q4 2026"`); ~200 req/5m on `appdetails` (avoid multi-appid batching). |
| `igdb_games` | Apicalypse `where`: `game_type`, `release_dates.release_region`, `genres`, `themes`, `platforms`, `first_release_date`, `rating` | Map `genres` + `themes` -> canonical `genres[]`, `game_type` -> `format`, `rating` (0–100), `age_ratings` | **`game_type` replaces deprecated `category`; `release_dates.release_region` replaces `region`**; `genres` vs `themes` split; non-commercial Twitch OAuth only. |

#### E. Canonical Genre Taxonomy & Cross-Provider Normalization Table (`MediaGenre` -> Upstream Values)

| Canonical Genre (`MediaGenre`) | TMDB Movie ID | TMDB TV ID | Trakt Slug | AniList `genre_in` | MangaDex Tag UUID | MangaUpdates `genre` | Open Library `/subjects/` | Podcast Index `cat` ID | IGDB `genres` / `themes` | Steam `genres[].id` | Wikidata Film QID |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `action` | `28` | `10759` | `action` | `"Action"` | `391b0423-d847-456f-aff0-8b0cfc03066b` | `"Action"` | `action_adventure` | — | theme `1` | `1` (Action) | `Q41263` |
| `adventure` | `12` | `10759` | `adventure` | `"Adventure"` | `87cc87cd-a395-47af-b27a-93258283bbc6` | `"Adventure"` | `adventure` | — | genre `31` | `25` (Adventure) | — |
| `sci_fi` | `878` | `10765` | `science-fiction` | `"Sci-Fi"` | `256c8bd9-4904-4360-bf4f-508a76d67183` | `"Sci-fi"` | `science_fiction` | `26` (Fiction) / `67` | theme `18` | — | `Q471839` |
| `fantasy` | `14` | `10765` | `fantasy` | `"Fantasy"` | `cdc58593-87dd-415e-bbc0-2ec27bf404cc` | `"Fantasy"` | `fantasy` | `26` (Fiction) | theme `17` | — | — |
| `romance` | `10749` | — | `romance` | `"Romance"` | `423e2eae-a7a2-4a8b-ac03-a8351462d71d` | `"Romance"` | `romance` | — | — | — | — |
| `thriller` | `53` | — | `thriller` | `"Thriller"` | `07251805-a27e-4d59-b488-f0bfbec15168` | `"Psychological"` | `thrillers` | — | theme `20` | — | `Q2484376` |
| `horror` | `27` | — | `horror` | `"Horror"` | `cdad7e68-1419-41dd-bdce-27753074a640` | `"Horror"` | `horror` | — | theme `19` | — | `Q200092` |
| `comedy` | `35` | `35` | `comedy` | `"Comedy"` | `4d32cc48-9f00-4cca-9b5a-a839f0764984` | `"Comedy"` | `humor` | `16` (Comedy) | — | — | `Q157444` |
| `drama` | `18` | `18` | `drama` | `"Drama"` | `b9af3a63-f058-46de-a9a0-e0c13906197a` | `"Drama"` | `drama` | `27` (Drama) | — | — | `Q130232` |
| `mystery` | `9648` | `9648` | `mystery` | `"Mystery"` | `ee653e07-d70f-4ec3-807b-38b78795154c` | `"Mystery"` | `mystery_and_detective_stories` | — | theme `43` | — | — |
| `documentary` | `99` | `99` | `documentary` | — | — | — | `biography` / `history` | `79` (Documentary) | — | `81` | `Q93204` |
| `animation` | `16` | `16` | `animation` | *(format)* | — | — | — | `104`/`105` | — | — | — |
| `slice_of_life` | — | — | — | `"Slice of Life"` | `e5301a23-ebd9-49dd-aca1-05ae9733532d` | `"Slice of Life"` | — | — | — | — | — |
| `history` | `36` | — | `history` | — | `33208764-8efb-4232-a986-0eaca8d3f8ba` | `"Historical"` | `history` | `28` (History) | — | — | — |
| `music` | `10402` | — | `music` | `"Music"` | — | — | `music` | `53` (Music) | — | — | — |
| `sports` | — | — | — | `"Sports"` | `69964a64-2f90-4d33-beeb-f3ed2875eb4c` | `"Sports"` | `sports` | `86` (Sports) | genre `14` | `18` | — |
| `non_fiction` | `99` | `99` | `documentary` | — | — | — | `nonfiction` | `20` (Education) | — | `54` | `Q93204` |
| `programming` | — | — | — | — | — | — | `programming` (`computers` in GB) | `102` (Technology) | — | `50–60` | — |
| `rpg` / `strategy` | — | — | — | — | — | — | — | — | genres `12`, `15` | `3` (RPG), `2` (Strategy) | — |

---

### 3.2 Data Schemas, Contracts & Normalization Pipeline

#### 1. `DataShape` Extension (`modules/connectors/data_shapes.py` + `apps/web/src/core/data-shapes.ts`)
```python
DataShape = Literal[
    "news", "measurement", "event", "located", "messages",
    "video", "project_activity", "weather", "series", "job",
    "media",  # NEW: movies, anime, manga/manhwa, books, podcasts, TV, music, games
]
```
- **Mapping rules (`_NON_CATALOG` / `_EXTRA`):**
  - `"youtube"` -> `frozenset({"video", "media"})` (backwards-compatible with `video_panel` AND compatible with new `media_feed` / `media_calendar`!).
  - New media providers -> `frozenset({"media"})` (except `ann_news` which may emit `frozenset({"news", "media"})`).
  - `FreeProviderSpec.category` & `CatalogEntry.category`: extend `Literal` to include `"media"`.

#### 2. `ProviderRecordMetadata` & `_MEDIA_FIELDS` (`modules/knowledge/documents/schemas.py`)
Add `MEDIA_PROVIDER_IDS` to `PROVIDER_IDS` and allowlist a shared `_MEDIA_FIELDS` frozenset in `_PROVIDER_SOURCE_FIELD_ALLOWLIST`:
- **Core fields (compatible with `_FEED_FIELDS`):** `title`, `summary`, `canonical_url`, `guid`, `published_at`, `provider_updated_at`, `publisher`, `image_url`
- **Media-specific validated fields:**
  - `media_type`: `"movie" | "anime" | "manga" | "manhwa" | "manhua" | "light_novel" | "book" | "audiobook" | "youtube" | "tv" | "podcast" | "music" | "game"`
  - `artwork_aspect`: `"poster" | "square" | "video"` (drives `2:3`, `1:1`, `16:9` aspect rendering)
  - `country`: ISO 3166-1 alpha-2 uppercase string matching `^[A-Z]{2}$` (e.g., `"US"`, `"JP"`, `"KR"`, `"CN"`, `"TW"`, `"VN"`, `"GB"`, `"FR"`)
  - `language`: normalized lowercase ISO 639-1 / BCP-47 string, max 16 chars (e.g., `"en"`, `"ja"`, `"ko"`, `"zh"`, `"zh-hk"`, `"vi"`; mapped from 3-letter ISO 639-2/B on Open Library/MusicBrainz and full names on TVMaze/OMDb/Steam)
  - `genres`: `list[str]` of canonical lowercase slugs (max 12 items, max 48 chars each)
  - `format`: sub-type string, max 32 chars (e.g., `"tv"`, `"movie"`, `"ova"`, `"ona"`, `"manga"`, `"manhwa"`, `"novel"`, `"ebook"`, `"audiobook"`, `"album"`, `"single"`, `"episodic"`, `"main_game"`, `"dlc"`)
  - `content_rating`: normalized string, max 32 chars (e.g., `"safe"`, `"pg"`, `"pg_13"`, `"r"`, `"suggestive"`, `"mature"`, `"explicit"`, `"clean"`)
  - `release_status`: `"upcoming" | "airing" | "released" | "completed" | "hiatus"`
  - `release_date`: ISO-8601 date or UTC timestamp (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ`)
  - `release_year`: bounded `int` (`1800 <= y <= 2100`)
  - `release_season`: string, max 32 chars (e.g., `"winter"`, `"spring"`, `"summer"`, `"fall"`, or `"Fall 2026"`)
  - `rating`: normalized `float` (`0.0 <= r <= 100.0`, finite; reject `bool`, `NaN`, `inf`; collectors scale `0–5` `* 20`, `0–10` `* 10`, `0–100` `* 1`)
  - `creator`: string (director / author / studio / channel / artist / host, max 240 chars)
  - `season`: string (e.g., `"2"`, `"Fall 2026"`, max 32 chars)
  - `episode`: string (e.g., `"14"`, `"14.5"`, max 32 chars)
  - `chapter`: string (e.g., `"118"`, `"118.5"`, max 32 chars)
  - `volume`: string (max 32 chars)
  - `duration_seconds`: bounded `int` (`0 <= d <= 604800`) — runtime / video / episode duration
  - `page_count`: bounded `int` (`0 <= p <= 50000`) — books
  - `external_ids`: `dict[str, str]` (allowlisted ID keys `{tmdb, imdb, anilist, mal, kitsu, mangadex, mangaupdates, isbn13, openlibrary, mbid, tvmaze, youtube, steam, igdb}`, max 16 entries, max 64-char non-blank string values)
  - `trailer_url`: validated HTTPS URL via `_validate_https_link`
  - `attribution`: string (e.g. `"Data via TVMaze (CC BY-SA 4.0)"`, `"Metadata by TMDB"`, `"MangaDex"`, max 160 chars)
- **Validator updates (`modules/knowledge/documents/schemas.py:570-591`, `_validate_source_field`):**
  - Add explicit bounded `float` branch (`isinstance(value, (int, float)) and not isinstance(value, bool)` + `math.isfinite(float(value))` + `[0.0, 100.0]` check for `rating`),
  - Add bounded `int` range branch (`not isinstance(value, bool)` + `[1800, 2100]` for `release_year`, `[0, 604800]` for `duration_seconds`, `[0, 50000]` for `page_count`),
  - Tighten `genres` list bounds (max 12 items, max 48 chars each vs. general `_validate_string_list` 100/255 caps) and enforce lowercase canonical/slug format,
  - Add `dict[str, str]` validation branch for `external_ids`,
  - Route `trailer_url` through `_validate_https_link`,
  - Enforce regex `^[A-Z]{2}$` for `country` and extend `_SOURCE_FIELD_STRING_LIMITS` (`country`: 2, `language`: 16, `format`: 32, `content_rating`: 32, `release_season`: 32, `media_type`: 32, `artwork_aspect`: 16, `release_status`: 32, `release_date`: 64, `creator`: 240, `season`: 32, `episode`: 32, `chapter`: 32, `volume`: 32, `attribution`: 160).

#### 3. Typed `MediaProjectionRead`, `Document.language` Contract & Public Export (`modules/knowledge/documents/schemas.py`, `public.py`, `_news.py`, `apps/web/src/modules/knowledge/api.ts`)
Mirror the existing `telegram` projection block on `GadgetDocumentProjectionRead`:
```python
class MediaProjectionRead(BaseModel):
    media_type: str
    artwork_aspect: Literal["poster", "square", "video"] = "poster"
    country: str | None = None
    language: str | None = None
    genres: list[str] = Field(default_factory=list)
    release_year: int | None = None
    release_season: str | None = None
    format: str | None = None
    content_rating: str | None = None
    rating: float | None = None
    release_status: str | None = None
    release_date: str | None = None
    creator: str | None = None
    season: str | None = None
    episode: str | None = None
    chapter: str | None = None
    volume: str | None = None
    duration_seconds: int | None = None
    page_count: int | None = None
    trailer_url: str | None = None
    attribution: str | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)
```
- **Module Boundary Export:** Because `.importlinter` (`ModulePublicApiContract`) and mypy `implicit_reexport = False` require explicit public exports, import and add `MediaProjectionRead` (and updated projection helpers) to `__all__` in `modules/knowledge/documents/public.py` (`lines 286-508`).
- **`Document.language` (`FEED_LANGUAGES`) vs. `source_fields["language"]` Contract:**
  - `Document.language` (`_language.py:4-22`, `_normalize.py:455`) drops any subtag outside `FEED_LANGUAGES` (13 codes) to `NULL`. Media collectors MUST populate **both** `record.metadata["language"]` (so `Document.language` is populated for existing `news_feed`) **and** `source_fields["language"]` (preserving exact normalized ISO 639-1 / BCP-47 tags such as `zh-hk` or codes beyond the 13).
  - `GET /api/v1/documents/dashboard-projections` keeps `language: Annotated[str | None, Query(pattern=r"^[a-z]{2}$")] = None` untouched for backwards compatibility with `test_feed_filters.py`, and adds `languages: list[str] | None` querying `provenance_json["provider_record"]["source_fields"]["language"].astext`.
- Populate `projection.media` inside `modules/knowledge/documents/_news.py` (or helper `_media.py`) when `provider_record.source_fields` contains `media_type` (or for `provider == "youtube"`, synthesizing `media_type="youtube"`, `artwork_aspect="video"`, `creator=author`).
- Keep `projection.image_url` signed via `make_image_signer(kind="feed")` -> `/api/v1/media/proxy?token=...`.
- **Image URL normalization rule in collectors:** Collectors MUST upgrade `http://` cover URLs where HTTPS is supported (e.g., Google Books `imageLinks`) and resolve Cover Art Archive redirect URLs or emit HTTPS links valid under `check_feed_image_url()` (no IP literals, no custom ports).

#### 4. Backend Projection Filtering (`_news.py`, `routes.py`), `GadgetScope`, `GadgetFilters` & Calendar Sorting (`modules/dashboard/schemas.py`, `gadgets.py`, `apps/web/src/modules/dashboard/api.ts`, `knowledge/api.ts`)
- **HTTP & Service Projection Filtering (`modules/knowledge/documents/routes.py` & `_news.py`):**
  - Extend `GET /api/v1/documents/dashboard-projections` (`routes.py:248-291`) and `list_gadget_document_projections` / `list_news_document_projections` / `_news_projection_statement` (`_news.py:534-743`) with optional multi-dimensional filter parameters:
    `countries: list[str] | None`, `genres: list[str] | None`, `languages: list[str] | None`, `media_types: list[str] | None`, `formats: list[str] | None`, `content_ratings: list[str] | None`, `release_statuses: list[str] | None`, `release_year: int | None`, `release_year_from: int | None`, `release_year_to: int | None`, `release_season: str | None`, `min_rating: float | None`, `release_window_days: int | None`, and `sort: Literal["created_at_desc", "release_date_asc"] = "created_at_desc"`.
  - **SQL Predicate Pattern (`_news_projection_statement`):** Follow the verified `NormalizedVersionProvenance` `.exists()` subquery pattern used by `channel_ids` (`_news.py:570-581`), joining `DocumentVersion.id` and `Source.generation`:
    - **Scalar string `IN` filters (`countries`, `languages`, `media_types`, `formats`, `content_ratings`, `release_statuses`, `release_season`):** `provenance_json["provider_record"]["source_fields"][<key>].astext.in_(values)`.
    - **JSONB array overlap for `genres`:** Use `or_(*(genres_col.contains([g]) for g in genres))` (PostgreSQL `@>`, avoiding raw `?|` operator placeholder collisions in SQLAlchemy/asyncpg and compiling cleanly under `postgresql.dialect()` in `test_feed_filters.py`).
    - **Numeric range filters (`min_rating`, `release_year*`):** Guard with `func.jsonb_typeof(col) == "number"` before `cast(col.astext, Float / Integer)` so legacy non-numeric rows never throw SQL cast errors.
    - **Indexing Note:** `Document.source_id.in_(source_ids)` (`ix_documents_source_id`) already bounds scans to <=32 active sources; because `normalized_version_provenance.provenance` currently has no GIN index, an optional `CREATE INDEX CONCURRENTLY` GIN migration (`core.migration_support.create_index_concurrently` following `0014_perf_indexes_and_safety.py`) can be added if library size grows.
  - **Cursor Fingerprint & Calendar Keyset Pagination (`_news.py:626-633, 1004-1025`):**
    - **Mandatory Cursor Isolation:** Include EVERY new filter parameter (`countries`, `genres`, `languages`, `media_types`, `formats`, `content_ratings`, `release_statuses`, `release_year`, `release_year_from`, `release_year_to`, `release_season`, `min_rating`, `release_window_days`, `sort`) in `_news_projection_cursor_fingerprint()` so a cursor from one filter/sort state cannot replay against another.
    - **Calendar Ascending Cursor:** Keep `(Document.created_at DESC, Document.id DESC)` for `sort="created_at_desc"`, and add a dedicated `(release_date_ts ASC, Document.id ASC)` keyset cursor + bounded `release_window_days` window for `sort="release_date_asc"` (`media_calendar`).
- **Dashboard `GadgetScope` & `GadgetFilters` (`modules/dashboard/schemas.py:42-125`, `gadgets.py:474-534`, `apps/web/src/modules/dashboard/api.ts:42-94`):**
  - `GadgetScope` (all `list` selectors auto-checked for uniqueness by `ensure_unique_selectors` at `schemas.py:99-105`): add `media_types: list[MediaType] = Field(default_factory=list, max_length=13)`, `countries: list[IsoCountryCode] = Field(default_factory=list, max_length=24)`, `genres: list[MediaGenre] = Field(default_factory=list, max_length=16)`, `languages: list[MediaLanguage] = Field(default_factory=list, max_length=16)`, `formats: list[MediaFormat] = Field(default_factory=list, max_length=16)`, `content_ratings: list[ContentRating] = Field(default_factory=list, max_length=8)`, `release_year: StrictInt | None = Field(default=None, ge=1800, le=2100)`, `release_season: AnimeSeason | None = None`, `min_rating: float | None = Field(default=None, ge=0.0, le=100.0)`, `release_window_days: StrictInt | None = Field(default=None, ge=1, le=90)`.
  - `GadgetFilters`: add `media_type: MediaType | None = None`, `media_types: list[MediaType] = Field(default_factory=list, max_length=13)`, `release_status: Literal["all", "upcoming", "airing", "released", "completed", "hiatus"] | None = None`, `group_by: Literal["none", "date", "media_type", "source"] | None = None`, `countries: list[IsoCountryCode] = Field(default_factory=list, max_length=24)`, `genres: list[MediaGenre] = Field(default_factory=list, max_length=16)`, `languages: list[MediaLanguage] = Field(default_factory=list, max_length=16)`, `formats: list[MediaFormat] = Field(default_factory=list, max_length=16)`, `content_ratings: list[ContentRating] = Field(default_factory=list, max_length=8)`, `release_year: StrictInt | None = Field(default=None, ge=1800, le=2100)`, `release_season: AnimeSeason | None = None`, `min_rating: float | None = Field(default=None, ge=0.0, le=100.0)` (with uniqueness/lowercase normalization).
  - Update `validate_renderer_configuration` (`modules/dashboard/gadgets.py:474-534`) to whitelist the new media `GadgetScope` fields exclusively for `"media_feed"` and `"media_calendar"`, AND reject media-only `GadgetFilters` fields on non-media renderers (mirroring the `renderer_id != "map"` guard at lines 526-532).
  - Mirror all new `GadgetScope` / `GadgetFilters` fields in `apps/web/src/modules/dashboard/api.ts:42-94`, update `unsupportedListFilters` (`api.ts:89-94`), and extend `listGadgetDocumentProjections` (`apps/web/src/modules/knowledge/api.ts:228-241`) `URLSearchParams` serialization (repeated `append` for list params `countries`, `genres`, `languages`, `media_types`, `formats`, `content_ratings`, `release_statuses`, and `set` for scalar `release_year`, `release_season`, `min_rating`, `release_window_days`, `sort`).
- **Out-of-order / Upcoming Incremental Selection (`_collection/base.py:1053`):** Register schedule/calendar providers whose `release_date` is in the future or whose entries update out-of-order (e.g., `wikidata_movies`, `tmdb_movies`, `tmdb_tv`, `tvmaze_schedule`, `anilist_anime` airing schedules) in `DEDUP_ONLY_PROVIDERS` or drive delta-sync via upstream `updatedAt` cursors so future releases are never dropped by monotonic `observed_at` cursor skew.

#### 5. Shared Media Utilities & YouTube Collector Upgrade
- **Shared Provider Rate-Pacer & Helpers (`modules/connectors/providers/_media_common.py`):**
  - Per-host/provider cooperative request spacing (MusicBrainz 1 req/s, AniList `X-RateLimit-Remaining`, ANN 1 req/s, MangaDex 5 req/s),
  - Custom descriptive `User-Agent` builder (`Umwelt-OS/1.0 (+local-self-hosted)`),
  - Reuse repository's **`safe_xml_root()`** (`modules/connectors/providers/news.py:299-314` — zero new `defusedxml` dependency in `pyproject.toml`),
  - Fuzzy partial date normalizer (`YYYY` / `YYYY-MM` / `YYYY-MM-DD` -> UTC `datetime` + raw `release_date` string),
  - `http://` -> `https://` cover normalizer,
  - Podcast `<itunes:duration>` parser (`HH:MM:SS` | `MM:SS` | seconds -> `int`),
  - Cross-ID stable `identity` builder (`f"{provider}:{external_id}"` or `f"{provider}:{scope}:{item_id}"`).
- **YouTube Collector Upgrade (`modules/connectors/providers/feed_catalog.py` + `_collection/native.py`):**
  - Migrate HTTP fetching from bare `httpx.AsyncClient` in `_read_feed()` to `rest.fetch_bounded()` inside `SendGate` (preserving `youtube:<channel_id>:<videoId>` identity for existing single-channel sources!).
  - Support `youtube_channels` (list of `UC...` IDs), `youtube_handles` (`@handle` resolved once via `rest.fetch_bounded` HTML `<link rel="canonical">` and cached in `SourceIngestionState.continuation_state`), `youtube_playlist_ids` (`PL...`), and `youtube_filter` (`"all"` -> `channel_id=UC...`, `"long_form"` -> `playlist_id=UULF...`, `"shorts"` -> `playlist_id=UUSH...`).
  - Handle transient YouTube `404`/`500` Atom outages gracefully without clearing `SourceIngestionState` cursor.
  - Add optional `youtube_api` (`source.type = "api"`, `key_required = True`) using `channels.list?forHandle=` (1 unit) + `playlistItems.list` on `UU...` (1 unit) + `If-None-Match` ETag (`304`).

---

### 3.3 Dashboard Gadget Improvements, Gadget Editor & Multi-Dimensional Filter UI

All UI designs strictly adhere to `docs/DESIGN_SYSTEM.md`, `docs/design-system/README.md`, `apps/web/src/app/globals.css`, Lucide icons only, sentence case, zero emoji, semantic tokens (`bg-surface`, `bg-card`, `text-foreground`, `text-muted-foreground`, `border-line`, `border-rule`, `bg-data` / `text-data` — **never** `bg-accent` for data highlights since `--accent` in Tailwind `bg-accent` maps to `--active` hover fill!), density variables (`--gadget-pad`, `--gadget-row-y`, `--gadget-font`), touch targets (`min-h-9 pointer-coarse:min-h-11`), and static file-level `as const` i18n lookup keys (`en-us` / `vi-vi` parity).

**UI Primitive Constraint:** `apps/web/src/components/ui/` provides `Button`, `Badge`, `Popover`, `Select`, `SearchableSelect`, `Input`, `Checkbox`, `DropdownMenuCheckboxItem`, `Tabs`, and `Tooltip`. **`Command` (`cmdk`), `combobox.tsx`, `slider.tsx`, `accordion.tsx`, and `scroll-area.tsx` are NOT installed** — searchable multi-select filters inside both the Gadget Settings Editor and in-gadget popovers MUST compose `SearchableSelect`, `Popover` + `Input` + `Checkbox` list, or `DropdownMenuCheckboxItem` (and removable `<Badge variant="outline">` + `<BareButton>` chips matching `SymbolPicker` at `gadget-editor-pickers.tsx:524-539`).

#### 1. New Gadget: `media_feed` (Template: `media_shelf` — *Media Releases & Shelf*)
- **Backend registration (`modules/dashboard/gadgets.py:22-40, 93-205, 397-447`):**
  - `RendererDescriptor(id="media_feed", config_version=1, minimum_width=4, minimum_height=4, runtime_state="available", capability_keys=("media_feed",), source_mode="multi", accepted_shapes=frozenset({"media", "video"}), max_sources=32)`
  - `DEFAULT_SIZES["media_feed"] = (8, 6)` (and matching `RENDERER_MIN_SIZES` / `RENDERER_DEFAULT_SIZES` in `grid-geometry.ts` + `GADGET_READING_BODY_FLOORS` in `widget-registry.tsx:103`)
  - `GadgetTemplate("media_shelf", "media_feed")`
- **Frontend component (`apps/web/src/modules/dashboard/gadgets/media-feed.tsx` + `media-parts.tsx`):**
  - **Initial State, Expand Persistence & Freshness:** Seed interactive filter state from `instance.definition.scope` and `instance.definition.filters`, pass active server filters into `listGadgetDocumentProjections` (`useQuery` key includes all filter fields), keep state automatically preserved across `Expand` modals via `GadgetFrame`'s `useRendererHost` (`gadget-frame.tsx:104-160`, single reparented DOM subtree), and report `(collectedAt, attribution)` via `useReportGadgetFreshness(at, attr)` so `ProvenanceFooter` renders the single bottom `border-t border-rule` line.
  - **Compact `@container/gadget` Toolbar & `Filter (N)` Popover (`FeedToolbar` / `FeedFilterPopover` pattern, `feed-parts.tsx:335-477`):**
    - **Primary Toolbar Row (`h-8` compact controls, `pointer-coarse:min-h-11` touch target):** `FeedSourceTabs` (when 2–6 sources bound), quick `media_type` selector/pills (`All`, `Movie`, `Anime`, `Manga`, `Manhwa`, `Book`, `YouTube`, `TV`, `Podcast`, `Music`, `Game` — using file-level `as const` `MEDIA_TYPE_LABEL_KEY`), `release_status` quick `Select` (`All`, `Released`, `Airing`, `Upcoming`), `Filter (N)` `<Popover>` trigger (`ListFilter` icon + active count), `Unread only` toggle, `View Toggle` (`Poster Grid` vs `Compact List`, persisted in `localStorage`), and `Refresh`.
    - **`Filter (N)` `<PopoverContent className="w-80 space-y-3 p-3">`:** Houses:
      1. **Search `<Input type="search">`** (keyword needle),
      2. **Country multi-select** (`Popover` / expandable searchable `Input` + `<Checkbox>` list of ISO-2 countries with `as const` `COUNTRY_LABEL_KEY`),
      3. **Genre multi-select** (searchable `Input` + `<Checkbox>` list over canonical `MediaGenre` slugs with `as const` `GENRE_LABEL_KEY`),
      4. **Language select / multi-select** (`MediaLanguage` + `Intl.DisplayNames` / `as const` `LANGUAGE_LABEL_KEY`),
      5. **Format / Sub-type select** (`MediaFormat` with `as const` `FORMAT_LABEL_KEY`),
      6. **Year & Season** (numeric `release_year` `<Input type="number" min={1800} max={2100}>` + `AnimeSeason` `<Select>`),
      7. **Content Rating & Min Rating** (`ContentRating` `<Select>` + `min_rating` `<Select>` presets `Any`, `>= 60`, `>= 70`, `>= 80`, `>= 90` or numeric `0–100` `<Input>`),
      8. **`Reset to defaults` / `Clear all`** action footer.
    - **Active Filter Chips Strip (shown only when `activeFilterCount > 0`):** Wrapped row of removable `<Badge variant="outline">` chips (e.g., `Country: JP`, `Genre: Sci-Fi`, `Min rating: 80+`, `Year: 2026`) with `<BareButton>` + `<X className="size-3" />` and a `Clear all` link/button.
  - **Remote Media Notice Banner:** If `image_url` is omitted because `allow_widget_remote_media` is `false`, renders a compact `<Link href="/settings/models#ai-widget-media">` notice banner explaining how to enable proxied cover art.
  - **Cards & Rows (`media-parts.tsx`):**
    - **Adaptive `FeedImage`** (`apps/web/src/components/feed-image.tsx`): add `aspect?: 'video' | 'poster' | 'square'` (`aspect-video` 16:9 for YouTube/trailers, `aspect-[2/3]` for movie/anime/manga/book covers, `aspect-square` 1:1 for podcasts/music), using `object-cover` inside `bg-surface border border-line rounded-md`.
    - **Typed Metadata Chips** (`Badge` primitives): e.g., `Anime · S02 E14`, `Manhwa · Ch. 118`, `Book · 384 pp.`, `JP`, `Sci-Fi`, `128 min`, `Score 86`, `Upcoming · Nov 24`.
    - **Actions per item:** Bookmark, Mark Read/Unread, Dismiss + Undo, Open external `canonical_url` (and secondary `trailer_url` icon link when present), checkbox selection for bulk Ask AI, AND **1-click single-item "Ask AI"** (`Sparkles` icon button triggering `{ kind: 'selection', items: [singleItem] }`).
  - **Pagination:** Cursor-based `Load more` (`listGadgetDocumentProjections`).

#### 2. New Gadget: `media_calendar` (Template: `release_calendar` — *Release Calendar*)
- **Backend registration (`modules/dashboard/gadgets.py`):**
  - `RendererDescriptor(id="media_calendar", config_version=1, minimum_width=4, minimum_height=4, runtime_state="available", capability_keys=("media_calendar",), source_mode="multi", accepted_shapes=frozenset({"media", "video"}), max_sources=32)`
  - `DEFAULT_SIZES["media_calendar"] = (6, 6)`
  - `GadgetTemplate("release_calendar", "media_calendar")`
- **Frontend component (`apps/web/src/modules/dashboard/gadgets/media-calendar.tsx`):**
  - Multi-source schedule aggregator (fixes `TimelineGadget`'s single-source historical limitation).
  - Groups items into localized date sections (`Today`, `Tomorrow`, `This Week`, `Upcoming`, `Recently Released` via file-level `as const` `CALENDAR_BUCKET_KEY`) formatted via `Intl.DateTimeFormat` (`en-US` / `vi-VN`).
  - Shares the compact **`MediaFilterBar`** (`media-parts.tsx`): `media_type` quick select, `release_window_days` selector (`7d`, `14d`, `30d`, `90d`), `Filter (N)` `Popover` (`Country`, `Genre`, `Language`, `Format`, `Content Rating`, `Min Rating`, `Search`), and removable active filter chips — querying `listGadgetDocumentProjections(..., { sort: 'release_date_asc', release_window_days, ... })`.
  - Compact row layout: small adaptive thumbnail (`2:3` / `1:1` / `16:9`), release time/date pill, title, creator/studio, country/genre chips, episode/chapter tag, status badge (`Airing`, `Premiere`, `New Chapter`), external link, and 1-click "Ask AI".

#### 3. Gadget Settings Editor, Existing Gadget Upgrades & i18n Static AST Compliance
- **Gadget Settings Editor (`gadget-editor-model.ts`, `gadget-editor-sections.tsx`, `gadget-editor-pickers.tsx`, `gadget-editor.tsx`):**
  - Extend `FieldKey` (`gadget-editor-model.ts:61`) with `'mediaScope' | 'mediaFilters' | 'releaseWindow'` (or granular keys `'mediaTypes' | 'countries' | 'genres' | 'languages' | 'formats' | 'contentRatings' | 'releaseYear' | 'releaseSeason' | 'minRating' | 'releaseStatus' | 'releaseWindow' | 'groupBy'`), add `MEDIA_RENDERERS = ['media_feed', 'media_calendar'] as const`, and grant `'keywords' | 'limit' | 'rules'` + media fields inside `fieldsFor(renderer)` (`92-104`).
  - Extend `GadgetDraft` (`179-213`), `initialDraft(definition)` (`273-291`), `draftPatchForTemplate()` (`301-322`), `rendererScope()` (`500+`), and `buildDefinitionPayload(ctx)` (`572-596`) so persisted `scope` (`media_types`, `countries`, `genres`, `languages`, `formats`, `content_ratings`, `release_year`, `release_season`, `min_rating`, `release_window_days`) and `filters` (`media_type`, `release_status`, `group_by`, default `countries`/`genres`/`languages`/`min_rating`) round-trip cleanly on Create & Patch.
  - Extend `SectionFacts` (`gadget-editor-sections.tsx:43-60`), `hasAdvanced` (`gadget-editor.tsx:613`), `useAutoOpenAdvanced` (`592-604`), `FIELD_ERROR_KEY` (`266-273`), and `isFieldRendered` (`399-410`) so server 422 validation errors map inline to `[data-field="..."]`.
  - Add reusable chip + searchable multi-select pickers in `gadget-editor-pickers.tsx` (`MediaMultiChipPicker` built from `<Badge variant="outline">` + `<BareButton>` + `<SearchableSelect>` or `<Popover>` + `<Input>` + `<Checkbox>` — zero `Command`/`cmdk`), used for Countries, Genres, Languages, Formats, and Content Ratings inside `AdvancedSection` / `MediaFiltersSection`.
- **Upgrades to Existing Gadgets & Source UI:**
  - **`video_panel` (`apps/web/src/modules/dashboard/gadgets/video-panel.tsx`):** Replace hardcoded `provider === 'youtube'` and hostname check with `safeHttpUrl` + optional provider check (accepting any `"video"`-shaped source); render `published_at` relative timestamp, `media.creator` (channel), `media.duration_seconds` formatted (`MM:SS` / `H:MM:SS`), source tabs, search input, bookmark/read toggles, cursor `Load more`, and 1-click "Ask AI" (`kind: 'selection'`).
  - **`FeedStream` (`apps/web/src/modules/dashboard/gadgets/feed-parts.tsx:43`):** Enable single-item "Ask AI" button on non-Telegram feed/media rows (respecting `local_only`).
  - **`YoutubeScope` (`apps/web/src/modules/sources/provider-scope.tsx:244-258`):** Upgrade from single `UC...` input to support `channel_id` / `@handle` / `playlist_id` tags (`UULF` long-form / `UUSH` Shorts filter select), with inline validation and server-side `@handle -> UC...` verification during `Save and test fetch`.
- **`en-us` / `vi-vi` Locales & Static AST Gates (`apps/web/locales/{en-us,vi-vi}/{sources,gadgetSettings,dashboard}.json`):**
  - Use **flat dotted keys** with zero leaf/group prefix collisions (so `unflatten()` never throws; e.g., never define both `"media.filter"` and `"media.filter.country"` — use `"media.filterTrigger"` and `"media.filterCountry"`).
  - Maintain 1:1 key & `{placeholder}` parity across `en-us` and `vi-vi` (`locales/catalog-parity.test.ts`).
  - Place ALL dynamic enum-to-key lookup objects (`COUNTRY_LABEL_KEY`, `GENRE_LABEL_KEY`, `LANGUAGE_LABEL_KEY`, `FORMAT_LABEL_KEY`, `CONTENT_RATING_LABEL_KEY`, `MEDIA_TYPE_LABEL_KEY`, `RELEASE_STATUS_LABEL_KEY`, `CALENDAR_BUCKET_KEY`, `OPTION_LABEL_KEY`, `OPTION_VALUE_KEY`, `RENDERER_NAME_KEY`, `TEMPLATE_NAME_KEY`, `TEMPLATE_DESCRIPTION_KEY`) at **module top-level (`ts.isSourceFile(scope)`) with `as const`**, and NEVER call ``t(`prefix.${val}`)`` (`locales/key-usage.test.ts`).
  - Keep `apps/web/src/core/data-shapes.ts`, `core/renderer-labels.ts`, `grid-geometry.ts` (`RENDERER_MIN_SIZES`, `RENDERER_DEFAULT_SIZES`), `widget-registry.tsx` (`GADGET_REGISTRY`, `GADGET_READING_BODY_FLOORS`), `widget-registry.lazy.test.tsx`, `gadget-template-sample.tsx`, and `test/fixtures/gadget-{renderers,templates}.json` synchronized.

---

### 3.4 Exact List of Codebase Files to Add or Modify

*(Every existing file path below has been verified against `d:\Project\Umwelt-OS`.)*

#### A. Backend — Connectors, Providers, Ingestion, Knowledge & Public Facades
| Action | File Path | Exact Scope of Changes |
|---|---|---|
| **Add** | `modules/connectors/providers/_media_common.py` | Shared HTTP rate-pacer, descriptive `User-Agent`, `providers.news.safe_xml_root()` wrapper, fuzzy date normalizer, HTTPS cover URL normalizer, `<itunes:duration>` parser, canonical genre/country/language/rating normalizers (`0.0–100.0` rating scale, 2-letter ISO-2 country, 2-letter/BCP-47 language), attribution constants. |
| **Add** | `modules/connectors/providers/media_anime_manga.py` | Keyless collectors for `anilist_anime`, `anilist_manga`, `livechart_episodes`, `ann_news`, `mangaupdates_releases`, `mangadex_releases` (with upstream pushdown query builders + `filter_records` fallback), plus `mal_anime` / `mal_manga` mappers. |
| **Add** | `modules/connectors/providers/media_movies_tv.py` | Keyless collectors for `wikidata_movies`, `itunes_movies`, `letterboxd_rss`, `tvmaze_schedule` (`GB` not `UK`, global `country=""`), plus `tmdb_movies` / `tmdb_tv` (split vs merged genre IDs, `certification_country`), `trakt_media` (`science-fiction` slug), `omdb_lookup` mappers. |
| **Add** | `modules/connectors/providers/media_books_podcasts.py` | Keyless collectors for `openlibrary_books` (ISO 639-2/B `eng`/`jpn`/`fre`/`vie`), `gutendex_books`, `apple_books_rss` (`rss.marketingtools.apple.com`), `podcast_rss` (+ iTunes lookup), `hardcover_books` (GraphQL POST `books_bool_exp`), `google_books` (`q=subject:...` + `https://` upgrade), `podcast_index` (2-secret SHA-1 `lang`/`cat`/`notcat`), and Wave 2/3 (`listenbrainz_releases` + MusicBrainz WS/2 Lucene, `steam_releases` `cc`/`l`, `igdb_games` `game_type`/`release_region`/`genres`+`themes`). |
| **Modify** | `modules/connectors/providers/feed_catalog.py` | Migrate `youtube` HTTP fetch onto `rest.fetch_bounded()` inside `SendGate`; support `@handle` canonical discovery, `playlist_id`, `UULF`/`UUSH` filter, multi-channel list while keeping `youtube:<channel>:<videoId>` identity. |
| **Modify** | `modules/connectors/providers/keyed.py` | Register **single-secret, single-GET** optional-key `KeyedProvider` specs (`tmdb_movies`, `tmdb_tv`, `trakt_media`, `omdb_lookup`, `mal_anime`, `mal_manga`, `google_books`, and single-GET `UC...` mode if kept here), reading multi-dimensional scope/filter options inside `build(secret, cfg)` and `mapper(payload, collected_at, cfg)`. |
| **Modify** | `modules/connectors/_collection/providers.py` | Register `ADAPTERS` mappings for keyless/pure media providers, `keyed.py` providers, and `SendGate`-backed native adapters (`hardcover_books`). |
| **Modify** | `modules/connectors/_collection/native.py` & `base.py` | Update `youtube` native execution (`SendGate` + `rest.fetch_bounded`), add `SendGate`-backed multi-call/OAuth/2-secret native handlers (`youtube_api`, `podcast_index`, `igdb_games`), and register calendar/schedule feeds in `DEDUP_ONLY_PROVIDERS` (`base.py`). |
| **Modify** | `modules/connectors/backends.py` | Add new provider IDs to `PROVIDER_SOURCE_TYPES` (`"api"` for JSON/keyed, `"rss"` for RSS/Atom), `FIXED_SCOPE_PROVIDERS` / `GATED_EXECUTOR_PROVIDERS`, `PROVIDER_SCOPE_FIELDS` (identifying source targets only; keep filter dimensions in `OPTION_FIELDS`). |
| **Modify** | `modules/connectors/service/scope.py` | Register any optional `CatalogEntry.scope_fields` in `OPTIONAL_SCOPE_FIELDS` (`scope.py:60-67`) and either/or target sets in `_ALTERNATIVE_SCOPE_FIELDS` (`scope.py:52-55`) so `get_current_provider_scope()` never fails closed. |
| **Modify** | `modules/connectors/builtins.py` | Mirror all fixed-scope preset media filter options (`OPTION_FIELDS` for `BUILTIN_PROVIDERS` such as `livechart_episodes`, `ann_news`) on `BuiltinEnableRequest` (`builtins.py:127-197`) with identical bounded types. |
| **Modify** | `modules/connectors/provider_specs.py` | Extend `FreeProviderSpec.category` Literal with `"media"`; add `_SPECS_RAW` entries, `DISPATCHABLE`, `PROVIDER_MIN_INTERVAL_MINUTES`, `BUILTIN_PROVIDERS` / `INCREMENTAL_PROVIDERS` entries. |
| **Modify** | `modules/connectors/catalog.py` | Extend `CatalogEntry.category` Literal with `"media"`; add `SAMPLE_OUTPUTS` and `_SCOPED_EXAMPLES` for media providers. |
| **Modify** | `modules/connectors/data_shapes.py` | Extend `DataShape` with `"media"`; map media providers to `{"media"}` (and `"youtube"` to `{"video", "media"}`). |
| **Modify** | `modules/connectors/provider_options.py` | Define `MediaGenre`, `MediaLanguage`, `MediaFormat`, `AnimeSeason`, `ContentRating`, extend `IsoCountry`, register per-provider `OPTION_FIELDS`, uniqueness/cap checks in `validate_options()`, upstream query assembly in `build_url()`, `map_options()`, `native_fingerprint()`, and post-fetch `filter_records()` for filterless RSS/schedule feeds. |
| **Modify** | `modules/connectors/service/schemas.py`, `service/config.py` & `registry.py` | Add typed optional fields on `ConnectorConfig` (`tuple[Enum, ...] | None = None`, `Literal[...] | None = None`, `StrictInt | None = None`, `float | None = None`, `StrictBool | None = None` — all defaulting to `None` so `_NATIVE_ONLY_DEFAULTS` is unaffected), serialization/masking, and generic-source exclusion in `registry._validate_generic_source`. |
| **Modify** | `modules/connectors/public.py` | Expose updated `DataShape`, `CatalogEntry`, and scoped/keyed media registries cleanly through `modules/connectors/public.py` per `.importlinter`. |
| **Modify** | `modules/knowledge/documents/schemas.py` & `public.py` | Add `MEDIA_PROVIDER_IDS` to `PROVIDER_IDS`; add `_MEDIA_FIELDS` (including `country`, `language`, `genres`, `release_year`, `release_season`, `format`, `content_rating`, `rating`) to `_PROVIDER_SOURCE_FIELD_ALLOWLIST` and `_SOURCE_FIELD_STRING_LIMITS`; extend `_validate_source_field()` with bounded `float`, bounded `int`, tightened `genres` list bounds, `dict[str, str]` (`external_ids`), HTTPS link (`trailer_url`), and `^[A-Z]{2}$` (`country`); extend `MediaProjectionRead` on `GadgetDocumentProjectionRead` and re-export in `public.py` `__all__`. |
| **Modify** | `modules/knowledge/documents/_news.py` & `routes.py` | Populate `GadgetDocumentProjectionRead.media` from `source_fields`; keep `image_url` signed via `make_image_signer`; add multi-dimensional JSONB `.exists()` filtering (`countries`, `genres` via `or_(*(col.contains([g])))`, `languages`, `media_types`, `formats`, `content_ratings`, `release_statuses`, `release_year`, `release_year_from`/`release_year_to`, `release_season`, `min_rating` with `jsonb_typeof` guard), dedicated `(release_date_ts, Document.id)` cursor for `sort="release_date_asc"`, and include every filter + `sort` param in `_news_projection_cursor_fingerprint()`. Keep `language` (`FEED_LANGUAGES`) untouched for backwards compatibility. |
| **Modify** | `modules/ingestion/_normalize_metadata.py` | Register `MEDIA_PROVIDER_IDS` in `ENVELOPE_REQUIRED_PROVIDERS` and `PROVIDER_FIELD_KEYS`, ensuring collectors pass `"language"` into both `record.metadata` and `source_fields`. |
| **Optional Migration** | `infrastructure/postgres/migrations/versions/0024_*.py` | *(Optional for large libraries)* Add `CREATE INDEX CONCURRENTLY` GIN index (`jsonb_path_ops`) on `normalized_version_provenance (provenance)` using `core.migration_support.create_index_concurrently` (not required at initial single-user scale because `Document.source_id.in_(source_ids)` already bounds scans). |

#### B. Backend — Dashboard Gadgets
| Action | File Path | Exact Scope of Changes |
|---|---|---|
| **Modify** | `modules/dashboard/schemas.py` | Extend `GadgetScope` (`media_types`, `countries`, `genres`, `languages`, `formats`, `content_ratings`, `release_year`, `release_season`, `min_rating`, `release_window_days` — auto-checked by `ensure_unique_selectors`) and `GadgetFilters` (`media_type`, `media_types`, `release_status`, `group_by`, `countries`, `genres`, `languages`, `formats`, `content_ratings`, `release_year`, `release_season`, `min_rating`). |
| **Modify** | `modules/dashboard/gadgets.py` | Register `RendererDescriptor` + `DEFAULT_SIZES` for `"media_feed"` and `"media_calendar"`; register `GadgetTemplate("media_shelf", "media_feed")` and `GadgetTemplate("release_calendar", "media_calendar")`; update `validate_renderer_configuration` to whitelist media `GadgetScope` fields and reject media-only `GadgetFilters` fields on non-media renderers. |

#### C. Frontend (`apps/web/`) — Contracts, Gadgets, Source Wizards, Design System & i18n
| Action | File Path | Exact Scope of Changes |
|---|---|---|
| **Modify** | `apps/web/src/core/data-shapes.ts` | Add `'media'` to TypeScript `DataShape` union. |
| **Modify** | `apps/web/src/core/renderer-labels.ts` | Add static `as const` renderer label keys for `media_feed` and `media_calendar` (plus `media_shelf` / `release_calendar` template labels). |
| **Modify** | `apps/web/src/modules/knowledge/api.ts` | Add typed `MediaProjection` interface (`country`, `language`, `genres`, `release_year`, `release_season`, `format`, `content_rating`, `rating`, `release_status`, `release_date`, `creator`, `season`, `episode`, `chapter`, `volume`, `duration_seconds`, `page_count`, `trailer_url`, `attribution`, `external_ids`) on `GadgetDocumentProjection`, and extend `listGadgetDocumentProjections` `URLSearchParams` serialization for all multi-dimensional filter & `sort` params. |
| **Modify** | `apps/web/src/modules/dashboard/api.ts` | Mirror all new `GadgetScope` and `GadgetFilters` fields (`countries`, `genres`, `languages`, `formats`, `content_ratings`, `release_year`, `release_season`, `min_rating`, `media_type`, `media_types`, `release_status`, `release_window_days`, `group_by`) and update `unsupportedListFilters` (`api.ts:89-94`). |
| **Modify** | `apps/web/src/modules/sources/api.ts` | Add `'media'` to `BuiltinCategory` (`api.ts:262`) and add new media `ConnectorConfig` / `BuiltinTypeOptions` TypeScript fields (`media_countries`, `media_genres`, `media_languages`, `media_formats`, `release_year`, `anime_season`, `content_ratings`, `min_rating`, `exclude_adult`, `youtube_handles`, `youtube_playlist_ids`, `youtube_filter`, etc.). |
| **Modify** | `apps/web/src/components/feed-image.tsx` | Add `aspect?: 'video' | 'poster' | 'square'` prop (`aspect-video` default, `aspect-[2/3]`, `aspect-square`) preserving `object-cover` and semantic border/surface tokens. |
| **Add** | `apps/web/src/modules/dashboard/gadgets/media-feed.tsx` | `MediaFeedGadget` component (source tabs, compact `@container/gadget` toolbar + `Filter (N)` `Popover` + removable active filter chips, poster-grid/list toggle, remote-media notice banner, cursor pagination, bookmark/read/dismiss, 1-click Ask AI, attribution footer, `useRendererHost` expand state preservation, `useReportGadgetFreshness`). |
| **Add** | `apps/web/src/modules/dashboard/gadgets/media-parts.tsx` | Shared `MediaFilterBar`, `MediaFilterPopover`, `ActiveFilterChips`, media card/row components, typed metadata badges (country, genre, rating, episode/chapter/duration), adaptive artwork wrapper, and top-level `as const` label lookup tables (`COUNTRY_LABEL_KEY`, `GENRE_LABEL_KEY`, `LANGUAGE_LABEL_KEY`, `FORMAT_LABEL_KEY`, `CONTENT_RATING_LABEL_KEY`, `MEDIA_TYPE_LABEL_KEY`, `RELEASE_STATUS_LABEL_KEY`). |
| **Add** | `apps/web/src/modules/dashboard/gadgets/media-calendar.tsx` | `MediaCalendarGadget` component (multi-source release calendar grouped by `Today` / `Tomorrow` / `This Week` / `Upcoming` / `Recent`, sharing `MediaFilterBar` + `release_window_days` + `sort: 'release_date_asc'`). |
| **Modify** | `apps/web/src/modules/dashboard/gadgets/video-panel.tsx` | Generalize beyond `youtube` hostname lock; render timestamps, duration, creator, source tabs, search, read/bookmark, cursor `Load more`, and 1-click Ask AI. |
| **Modify** | `apps/web/src/modules/dashboard/gadgets/feed-parts.tsx` | Allow 1-click single-item Ask AI button on non-Telegram feed/media rows (respecting `local_only`). |
| **Modify** | `apps/web/src/modules/dashboard/widget-registry.tsx` & `widget-registry.lazy.test.tsx` | Register `"media_feed"` and `"media_calendar"` in `GADGET_REGISTRY`, `GADGET_READING_BODY_FLOORS`, and `widget-registry.lazy.test.tsx`. |
| **Modify** | `apps/web/src/modules/dashboard/grid-geometry.ts`, `gadget-editor-model.ts`, `gadget-editor-sections.tsx`, `gadget-editor-pickers.tsx`, `gadget-editor.tsx`, `gadget-template-sample.tsx` | Add `RENDERER_MIN_SIZES` / `RENDERER_DEFAULT_SIZES`; extend `FieldKey`, `MEDIA_RENDERERS`, `fieldsFor()`, `GadgetDraft`, `buildDefinitionPayload()`, `SectionFacts`, `hasAdvanced`, `FIELD_ERROR_KEY`, `isFieldRendered`, `useAutoOpenAdvanced`; add reusable `MediaMultiChipPicker` (`Badge` + `BareButton` + `SearchableSelect` / `Popover` + `Input` + `Checkbox`, zero `cmdk`); add template preview samples. |
| **Modify** | `apps/web/src/modules/sources/provider-scope.tsx`, `scope-fields.tsx`, `provider-label-keys.ts`, `connector-editor-model.ts`, `guide-keys.ts`, `builtin-sources.tsx`, `builtin-option-specs.ts`, `builtin-option-keys.ts`, `builtin-options.tsx`, `starter-packs.ts` | Add reusable `MediaFilterFields` in `scope-fields.tsx` + register scoped media providers in `NativeProvider` / `SCOPE_SECTIONS` / `REQUIRED_SCOPE` / `FIELD_LABEL_KEYS` / `SCOPE_HINT_KEY` / `PROVIDER_KEY` / `PROVIDER_LABEL_KEY`; upgrade `YoutubeScope`; add `'media'` to `PROVIDER_GROUP_ORDER` & `CATEGORY_ORDER`; add `OPTION_SPECS` + `OPTION_LABEL_KEY`/`OPTION_VALUE_KEY`; update `EnumOption` (`builtin-options.tsx:84-108`) with `spec.labelled` + `optionValueKey()` support; add optional Media Starter Pack. |
| **Modify** | `apps/web/locales/en-us/*.json` & `apps/web/locales/vi-vi/*.json` (`dashboard.json`, `gadgetSettings.json`, `sources.json`) | Add complete flat-dotted `en-us` and `vi-vi` translations with 1:1 key & `{placeholder}` parity (and zero prefix/leaf collisions) for all media categories, countries, genres, languages, formats, content ratings, gadget labels, filter controls, metadata chips, calendar buckets, attribution strings, and source options. |

#### D. Documentation & Test Snapshot Sync
| Action | File Path | Exact Scope of Changes |
|---|---|---|
| **Modify** | `docs/connectors/provider-catalog.md` & `docs/connectors/free-sources.md` | Add inventory/catalog rows for every new media provider (mandatory for `test_provider_docs_inventory.py`). |
| **Modify** | `docs/DESIGN_SYSTEM.md` & `docs/IMPLEMENTATION_STATUS.md` | Document new `"media"` gadgets/renderers, multi-dimensional filter patterns, and media collector implementation status when implemented. |
| **Modify** | `apps/web/src/test/fixtures/gadget-renderers.json` & `apps/web/src/test/fixtures/gadget-templates.json` | Regenerate exported backend renderer & template snapshots verified by `test_metric_catalog_snapshot.py` and `gadget-editor.test.tsx`. |

---

### 3.5 Automated Verification Plan (For Future Implementation Phase)

All checks below must pass without network reliance (using `httpx.MockTransport` + recorded JSON/XML fixtures for collectors and Vitest mocks for UI):

#### 1. Backend Unit, Boundary & Contract Gates (`pytest`)
- **Collector & Multi-Dimensional Filter Unit Tests (new):**
  - `tests/unit/modules/connectors/test_media_anime_manga.py` — AniList GraphQL (`countryOfOrigin`, `genre_in`, `format_in`, `season`, `seasonYear`, `isAdult=false` default, `averageScore_greater`, `X-RateLimit-Remaining`/`429`), LiveChart RSS (`If-Modified-Since` 304 + `filter_records`), MangaUpdates (`POST /v1/series/search` vs `/v1/releases`, `stype`/`genre`/`exclude_genre`), MangaDex (`originalLanguage[]` `zh`/`zh-hk`, `includedTagsMode=OR`, `publicationDemographic[]`, `contentRating[]`, attribution), MAL v2 (`X-MAL-CLIENT-ID` + `filter_records`).
  - `tests/unit/modules/connectors/test_media_movies_tv.py` — Wikidata SPARQL (`User-Agent`, `Q11424`, `P495`/`P136`/`P364`), iTunes Movies, Letterboxd RSS (`<letterboxd:filmYear>`, `memberRating * 20`, 50-item cap), TVMaze (`GB` not `UK`, global `country=""` on `/schedule/web`, CC BY-SA 4.0 attribution, `filter_records` genre/lang/rating), TMDB Movie vs TV (`10759`/`10765`, `certification_country` + `certification`, `/changes` 14-day chunking), Trakt (`science-fiction` slug, `countries`/`genres`/`languages`/`years`/`ratings`), OMDb (`Country`/`Language`/`Genre` normalization + `filter_records`).
  - `tests/unit/modules/connectors/test_media_books_podcasts_youtube.py` — Open Library (2-letter <-> 3-letter ISO 639-2/B `eng`/`jpn`/`fre`/`vie`, `cover_i`), Crossref, Gutendex (`languages`/`topic`), Apple Marketing Tools v2 (`rss.marketingtools.apple.com`, `{country}`, max `limit=100`, `filter_records` genre), Google Books (`q=subject:...`, `langRestrict`, `http://` -> `https://` `imageLinks`), Hardcover GraphQL (`books_bool_exp`), Podcast RSS (`<language>`, `<itunes:category>`, `<itunes:duration>`), Podcast Index (`lang`/`cat`/`notcat`, SHA-1 auth), upgraded YouTube (`@handle`, `UULF`/`UUSH`, `SendGate` + `rest.fetch_bounded`), YouTube Data API v3 (`1`-unit `playlistItems.list` + `videos.list` `regionCode`/`videoCategoryId`), ListenBrainz + MusicBrainz WS/2 Lucene, Steam (`cc`/`l`, `coming_soon`), and IGDB v4 (`game_type`, `release_dates.release_region`, `genres`+`themes`).
- **Projection Query, Cursor & Schema Filter Tests (new/extended):**
  - `tests/unit/modules/knowledge/documents/test_provider_snapshot_fields.py` — `_MEDIA_FIELDS` allowlist, bounded `float` (`rating` `0.0–100.0`, reject `bool`/`NaN`/`inf`), bounded `int` (`release_year` `1800–2100`, `duration_seconds`, `page_count`), `genres` max-12/48-char bounds, `country` `^[A-Z]{2}$`, `external_ids` `dict[str, str]`, `trailer_url` HTTPS check.
  - `tests/unit/modules/knowledge/documents/test_feed_filters.py` — SQL statement compilation under `postgresql.dialect()` for `countries`, `genres` (`or_(*(col.contains([g])))`), `languages` (`source_fields["language"]` vs `Document.language` `FEED_LANGUAGES` backwards compat), `media_types`, `formats`, `content_ratings`, `release_statuses`, `release_year` / `release_year_from` / `release_year_to`, `release_season`, `min_rating` (`jsonb_typeof` guard), `sort="release_date_asc"` keyset cursor, and **cursor fingerprint isolation** across every filter/sort param.
  - `tests/unit/modules/dashboard/test_gadget_shapes.py` — `GadgetScope` (`ensure_unique_selectors`) and `GadgetFilters` multi-dimensional field validation, `validate_renderer_configuration` whitelisting media scope on `media_feed`/`media_calendar` and rejecting media-only `GadgetFilters` on unrelated renderers (e.g., `news_feed`, `finance_chart`).
- **Mandatory Existing Invariant Suites:**
  ```powershell
  .venv/Scripts/python.exe -m pytest `
    tests/unit/test_provider_docs_inventory.py `
    tests/unit/architecture/test_module_boundaries.py `
    tests/unit/modules/connectors/test_free_provider_dispatch.py `
    tests/unit/modules/connectors/test_data_shapes.py `
    tests/unit/modules/connectors/test_keyed_providers.py `
    tests/unit/modules/connectors/test_provider_options.py `
    tests/unit/modules/connectors/test_wave_f_options.py `
    tests/unit/modules/connectors/test_collection_native_options.py `
    tests/unit/modules/connectors/test_selection_config.py `
    tests/unit/modules/connectors/test_wizard_payloads.py `
    tests/unit/modules/knowledge/documents/test_provider_snapshot_fields.py `
    tests/unit/modules/knowledge/documents/test_feed_filters.py `
    tests/unit/modules/knowledge/documents/test_gadget_provider_metadata.py `
    tests/unit/modules/knowledge/documents/test_routes_scope.py `
    tests/unit/modules/dashboard/ `
    tests/test_migrations.py
  ```
- **Backend Lint & Typecheck:**
  - `ruff check`, `lint-imports`, and `mypy` (`./scripts/dev.ps1 lint`, `./scripts/dev.ps1 typecheck`) on all added/modified Python modules.

#### 2. Frontend Unit, i18n AST Parity, Lint, Typecheck & Build (`apps/web`)
- **New & Updated Component / Contract Tests (`vitest`):**
  - `media-feed.test.tsx` — adaptive `aspect-[2/3]` / `aspect-square` / `aspect-video` rendering, in-gadget `Filter (N)` `Popover` (country, genre, language, format, year/season, content rating, `min_rating`, search), removable active filter chips + `Clear all` / `Reset to defaults`, `URLSearchParams` query serialization, `Expand` filter state preservation, remote-media disabled notice banner, cursor pagination, bookmark/read/dismiss, 1-click Ask AI `kind: 'selection'`, attribution display.
  - `media-calendar.test.tsx` — multi-source chronological grouping (`Today`/`Tomorrow`/`This Week`/`Upcoming`/`Recently Released`), `en-US`/`vi-VN` date formatting, `release_window_days` & multi-dimensional filter bar with `sort: 'release_date_asc'`.
  - `video-panel.test.tsx` — generalized video sources, pagination, timestamps, duration, Ask AI.
  - `provider-scope.test.tsx` & `builtin-options.test.tsx` — `MediaFilterFields` (country/genre/language/year/format/rating controls), `YoutubeScope` (`@handle` / multi-channel / `UULF`/`UUSH`), `EnumOption` localized `spec.labelled` rendering, `ScheduleStep` `null` reset vs `BuiltinWizard` omission.
  - `gadget-editor.test.tsx` — real snapshot fixtures (`gadget-renderers.json`, `gadget-templates.json`), `MediaMultiChipPicker` (countries, genres, languages, formats, content ratings), `min_rating` / `release_year` / `release_window_days` persistence, and 422 field-tagged error rendering.
  - `dashboard/api.test.ts` — `unsupportedListFilters` recognizing all new media `GadgetFilters` fields.
- **Mandatory Existing Frontend Gates:**
  ```powershell
  npm test --workspace apps/web -- `
    locales/catalog-parity.test.ts `
    locales/key-usage.test.ts `
    src/modules/dashboard/api.test.ts `
    src/modules/dashboard/gadget-editor.test.tsx `
    src/modules/dashboard/widget-registry.lazy.test.tsx `
    src/modules/dashboard/gadgets/feed-gadget.test.tsx `
    src/modules/dashboard/gadgets/video-panel.test.tsx `
    src/modules/dashboard/gadgets/media-feed.test.tsx `
    src/modules/dashboard/gadgets/media-calendar.test.tsx `
    src/modules/sources/builtin-option-specs.test.ts `
    src/modules/sources/builtin-options.test.tsx `
    src/modules/sources/provider-scope.test.tsx `
    src/modules/sources/source-wizard.test.tsx
  ```
  Plus `npx tsc --noEmit`, ESLint, and full `make lint` / `make typecheck` / `make test` / `make build` (or `./scripts/dev.ps1` equivalents).

#### 3. Optional E2E Playwright Verification (Requires Owner Approval for Docker Harness)
- Extend `tests/e2e/dashboard-ux.spec.ts` and `tests/e2e/sources-wizard.spec.ts` (under mocked/fixture upstreams) to verify adding a scoped media source with country/genre/language filters, placing `media_shelf` and `release_calendar` gadgets with default filters, toggling interactive in-gadget filters + `Expand`, verifying proxied poster images when `allow_widget_remote_media` is enabled, and triggering "Ask AI" from a media card.

---

## Part 4: Implementation Sequencing & Delivery Phases

| Phase | Scope | Dependencies |
|---|---|---|
| **Phase 1: Core Contracts, Multi-Dimensional Filter Types & Projection Query Engine** | `DataShape` (`"media"`), `CatalogEntry.category` (`"media"`), `MediaGenre`/`MediaLanguage`/`MediaFormat`/`AnimeSeason`/`ContentRating`/`IsoCountry` in `provider_options.py`, `ConnectorConfig` (`None`-defaulted) + `OPTIONAL_SCOPE_FIELDS` + `BuiltinEnableRequest`, `PROVIDER_IDS` & `_MEDIA_FIELDS` allowlist + `_validate_source_field`, `MediaProjectionRead`, `_news.py` JSONB filtering + cursor fingerprint + `release_date_asc` mode, `routes.py`, `GadgetScope`/`GadgetFilters` + `validate_renderer_configuration`, `_media_common.py`, and TypeScript contract types (`core/data-shapes.ts`, `knowledge/api.ts`, `dashboard/api.ts`, `sources/api.ts`). | None |
| **Phase 2: Wave 1 Keyless Collectors + YouTube Upgrade** | `anilist_anime`, `anilist_manga`, `livechart_episodes`, `ann_news`, `mangaupdates_releases`, `mangadex_releases`, `openlibrary_books`, `gutendex_books`, `apple_books_rss`, `wikidata_movies`, `itunes_movies`, `letterboxd_rss`, `podcast_rss`, `tvmaze_schedule`, upgraded `youtube` (`SendGate` + `fetch_bounded` + `@handle`/`UULF`/`UUSH`), wired to upstream pushdown (`build_url`) + post-fetch `filter_records()` + normalized `_MEDIA_FIELDS` + doc inventory sync. | Phase 1 |
| **Phase 3: Dashboard Gadgets, Filter UI, Editor Pickers & Source UI** | `FeedImage` aspect variants, `media_feed`, `media_calendar` (with `MediaFilterBar`, `Filter (N)` `Popover`, active chips, `Expand` preservation), `GadgetEditor` multi-dimensional pickers, `video_panel` upgrade, `FeedStream` 1-click Ask AI, `YoutubeScope` + `MediaFilterFields` + `EnumOption` `spec.labelled` fix, `en-us`/`vi-vi` locales (`as const` tables), Vitest + snapshot parity (`gadget-renderers.json`, `gadget-templates.json`). | Phase 1 & 2 |
| **Phase 4: Optional API-Key Upgrades & Wave 2–3** | `tmdb_movies`, `tmdb_tv`, `trakt_media`, `omdb_lookup`, `mal_anime`, `mal_manga`, `hardcover_books`, `google_books`, `youtube_api`, `podcast_index`, plus Wave 2 (`listenbrainz_releases` + MusicBrainz WS/2) and Wave 3 (`steam_releases`, `igdb_games`), each wired into the canonical genre/country/language/rating filter matrix. | Phase 1–3 |
