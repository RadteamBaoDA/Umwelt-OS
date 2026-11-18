# Media datasource Phase 4 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` and review each task before starting the next.

**Goal:** Implement every Phase 4 keyed upgrade and Wave 2/3 source in `specs/new-datasource-v2.md`, with validated source scope, credentials, normalized media records, filter matrix integration, tests and catalog documentation.

**Architecture:** Use `keyed.py` only where one bounded request uses one static secret. Use existing `SendGate`-backed native adapters for GraphQL POST, multi-request lookups, OAuth token exchange and two-secret HMAC. Reuse Phase 1/2 media option validation, `native_fingerprint`, `_MEDIA_FIELDS`, provenance and source/UI contracts; do not add a second collector or cursor system.

**Tech Stack:** Python 3.12, FastAPI/Pydantic v2, existing connector registry/keyed/native adapters and SendGate, pytest, Ruff, mypy; existing Sources UI, Vitest, TypeScript and ESLint where UI changes are needed.

**Spec:** `specs/new-datasource-v2.md`, especially Part 3 tables A–E, keyed/native constraint at lines 73–74, Part 3 provider rules (TMDB through IGDB), and Phase 4 at line 562. Phase 1–3 contracts are prerequisites and remain authoritative.

## Global constraints

- Treat the spec as the authority for provider IDs, credentials, endpoints, option names, field normalization, attribution, rate limits, licensing and acceptance.
- `keyed.py` supports a single static secret and exactly one gated request. Multi-call, GraphQL POST, OAuth exchange and two-secret HMAC MUST use `SendGate`-backed native adapters.
- New `ConnectorConfig` media filter options default to `None`; identify sources with provider scope fields and keep display filters in `OPTION_FIELDS`/`native_fingerprint` so filter edits reset cursors without invalidating already-collected provenance.
- Use `build_url()` or `KeyedProvider.build()` for exact upstream pushdown; also apply `filter_records()` where the provider lacks a matching query parameter.
- Persist records, accepted cursor/validator and query fingerprint atomically through existing receipts. A failed or truncated page must not advance the cursor.
- Normalize only allowlisted typed media fields; preserve source provenance, HTTPS-safe images, canonical IDs, mandatory attributions, owner access and local-only restrictions.
- Follow AGENTS.md: GitNexus impact before edits, precommit `detect_changes`, adjacent Python docstrings/JSDoc for changed production symbols, focused tests/Ruff/mypy/frontend gates, local commit per accepted phase; no push/deploy.
- Live provider calls and Docker/E2E need owner approval under repository instructions. If not authorized, keep these as explicit acceptance limits and do not claim live readiness.

## Review focus

- Filter-only edits reset cursor/fingerprint but do not change provider-scope identity or hide previously accepted records.
- Missing, rotated or invalid credentials fail safely and do not expose secret material in errors, logs, UI or reports.
- 401/403, 429/cooldown, bounded redirects, timeouts, malformed payloads and unsafe image URLs do not publish partial cursors or records.
- Multi-step lookups, token exchange and page truncation remain within provider quotas and `SendGate` before every outbound request.
- Future releases and out-of-order provider updates survive dedupe/cursor selection; duplicate external IDs do not create duplicate media observations.

---

### Task 1: Keyed movies and television upgrades

**Files:** `provider_options.py`, connector schemas/scope/config, `providers/keyed.py`, provider specs/catalog/inventory, source scope labels/options/locales, and focused connector/source tests.

**Scope:** Add `tmdb_movies`, `tmdb_tv`, `trakt_media`, and `omdb_lookup` with exact endpoint, credential placement, query parameter, canonical genre-ID, country/language/rating, certification, attribution and poll-floor rules from the corresponding provider rows in Part 3. Keep TMDB Movie and TV genre ID maps separate. Keep OMDB filters post-fetch where upstream has no parameter.

**Interfaces:** Consume Phase 1 media options/record schemas and Phase 2 provider registration/receipt patterns. Produce four active provider IDs with scoped credentials, catalog rows, filter validation, normalization, cursor/fingerprint behavior and localized source fields.

- [ ] Add failing provider-option/build/normalization/credential tests for exact IDs and provider-specific field maps.
- [ ] Implement keyed single-GET providers through existing keyed registration; keep options optional with `None` defaults.
- [ ] Add Source UI credential/scope fields and localized labels while keeping display filters optional.
- [ ] Run focused movie/TV connector, catalog, wizard, source-scope, provider-document inventory tests; Ruff, mypy and focused frontend checks.
- [ ] Run GitNexus change detection, update Task 1 report, commit only Task 1-owned files.

### Task 2: MAL anime/manga and Google Books

**Files:** provider options/schema, keyed adapters, provider specs/catalog, source scope and credential fields/locales, focused provider and UI tests.

**Scope:** Add `mal_anime`, `mal_manga`, and `google_books` using the exact MAL v2 ranking/season paths, `X-MAL-CLIENT-ID`, `nsfw` handling, `mean * 10` rating, canonical media types and country inference; add Google Books `query:key`, `q=subject:...`, `langRestrict`, ratings, categories, ISBNs and HTTPS-upgraded image links. Use canonical filter post-processing when no upstream param exists.

**Interfaces:** Reuse Task 1 keyed credential/UI pattern and Phase 1 media normalization. Produce these three providers with per-provider query fields, bounded source targets, filter fingerprints, docs and localized UI.

- [ ] Add failing tests for MAL `nsfw` defaults, manga country/media-type mapping, `mean * 10`, Google `subject:` inside `q`, `langRestrict`, ISBNs and HTTPS cover URLs.
- [ ] Implement the three keyed adapters with one bounded send each and no undeclared secondary calls.
- [ ] Add required scopes, credential request labels, provider catalog inventory and source wizard coverage.
- [ ] Run focused connector, provider specs, catalog, source UI and locale tests; Ruff/mypy/TypeScript/ESLint as applicable.
- [ ] Run GitNexus change detection and commit only Task 2 files.

### Task 3: Hardcover GraphQL books

**Files:** native provider adapter/registration, provider options/spec/catalog, credential/scope UI and focused tests.

**Scope:** Add `hardcover_books` as a `SendGate`-backed GraphQL POST. Normalize release dates/years, rating `* 20`, cached tags to genres, pages/audio duration and ISBN/external IDs; apply filters and bounded pagination as specified. Do not route GraphQL through `keyed.py`.

**Interfaces:** Reuse native credential isolation, send fences and atomic receipt; reuse Phase 1 book/media projection contract. Produce provider/catalog/scope registration and stable cursor behavior. Live GraphQL schema introspection is an acceptance gate explicitly separate from mocked unit tests and requires owner approval for live calls.

- [ ] Add failing tests for GraphQL request body, headers/secret redaction, cursor/page limits, metadata normalization, filter pushdown and no cursor advance on partial failure.
- [ ] Implement bounded native collection using `SendGate` for every call and accepted receipt for records/cursor.
- [ ] Add credential/scope UI and localized labels with optional filters outside required-scope gate.
- [ ] Run focused backend tests, Ruff/mypy, source wizard/frontend and catalog parity checks.
- [ ] Run GitNexus change detection and commit only Task 3 files.

### Task 4: YouTube Data API and Podcast Index native collectors

**Files:** `providers/native.py` and/or existing native handlers, provider registry/options/credentials, Source scope UI/catalog/locales, focused tests.

**Scope:** Add `youtube_api` and `podcast_index` using their exact distinct authentication and quota contracts. YouTube handle resolution (`channels.list`) then uploads pagination (`playlistItems.list`) and video metadata (`videos.list`) use SendGate for each request; direct stable channel/playlist IDs may use one-request keyed path only if the existing catalog/credential contracts support it. Preserve ETag/304 and bounded API units; never use `search.list`. Podcast Index uses the exact two-secret `apiKey`/`apiSecret`, timestamp and SHA-1 Authorization header, with language/category `lang`/`cat`/`notcat`/`since`/`val` mapping.

**Interfaces:** Reuse YouTube Atom source targets and media filters from Phase 2/3; do not fork IDs/provenance. Produce API-key upgrades with stable receipts, source-scope UI and provider inventory.

- [ ] Add failing tests for handle resolution call order, quota/bounds, ETag `304`, video metadata, HMAC headers/clock-skew failure, secret redaction and cursor atomicity.
- [ ] Implement bounded SendGate native handlers and exact credential requests.
- [ ] Register optional API-key upgrades and UI scopes without changing keyless YouTube behavior.
- [ ] Run focused connector/auth/receipt/provider catalog/source UI tests, Ruff/mypy/TS/ESLint.
- [ ] Run GitNexus change detection and commit only Task 4 files.

### Task 5: Wave 2 music releases

**Files:** media/music provider and shared rate-pacer modules, native/provider registry/options, catalog/source UI/locales, documentation inventory, focused tests.

**Scope:** Add `listenbrainz_releases` with ListenBrainz fresh releases, MusicBrainz WS/2 enrichment and Cover Art Archive. Apply strict MusicBrainz 1 request/sec pacing and mandatory descriptive User-Agent; normalize MB country/language/tags/types, dates/status, IDs and cover redirects. Use the exact ListenBrainz and MusicBrainz filter limits in Part 3; never imply unsupported ListenBrainz country/genre/language/type pushdown.

**Interfaces:** Reuse `_media_common`, SendGate and Phase 1 canonical genre/country/language options. Produce a source with a bounded cursor, exact query fingerprint, canonical metadata and CAA-safe image URL handling.

- [ ] Add failing tests for 1 req/sec pacing, MB 3-letter language normalization, CAA redirects, query filters and no cursor advance on failed enrichment.
- [ ] Implement bounded collector and deterministic dedup/cursor behavior.
- [ ] Add source scope/options/catalog and localized UI with optional filters.
- [ ] Run focused provider, quota, source/UI and locale tests; Ruff/mypy/TS/ESLint.
- [ ] Run GitNexus change detection and commit only Task 5 files.

### Task 6: Wave 3 Steam and IGDB games

**Files:** native provider adapters/registry, provider specs/options/credentials/catalog, source scope UI/locales, provider inventory and focused tests.

**Scope:** Add `steam_releases` featured/news modes with bounded per-app detail lookups, ISO2 storefront and Steam language names, genres/metacritic/age/release normalization, preserving `coming_soon` null price and fuzzy date strings. Add `igdb_games` with Twitch OAuth2 client-credentials token exchange and Apicalypse POST through `SendGate`; use `game_type` and `release_dates.release_region`, map genres+themes separately, enforce non-commercial use, pacing, quotas, age ratings, external IDs and token expiry.

**Interfaces:** Native adapter for both because Steam detail fanout and IGDB OAuth/POST exceed `keyed.py`'s single-request static-secret contract. Reuse canonical `MediaGenre`, `MediaFormat`, countries/ratings and atomic receipt. Do not batch Steam appdetails multi-appid calls.

- [ ] Add failing tests for Steam bounded appdetails, fuzzy dates/null price, region/language normalization; IGDB OAuth, token fencing/expiry, Apicalypse request, genre/theme and age-rating normalization.
- [ ] Implement quota-bounded native collection and credentials without logging secrets/tokens.
- [ ] Add catalog/UI source scopes, provider inventory and optional filters.
- [ ] Run focused provider/auth/quota/wizard/catalog tests; Ruff/mypy/TS/ESLint.
- [ ] Run GitNexus change detection and commit only Task 6 files.

### Task 7: Whole Phase 4 parity and acceptance

- [ ] Verify all 13 IDs (`tmdb_movies`, `tmdb_tv`, `trakt_media`, `omdb_lookup`, `mal_anime`, `mal_manga`, `hardcover_books`, `google_books`, `youtube_api`, `podcast_index`, `listenbrainz_releases`, `steam_releases`, `igdb_games`) appear consistently in provider registry, catalog inventory, docs, option registry, credential schema/UI, dashboard media compatibility and source wizard contract.\n- [ ] Compare each provider against its Part 3 pushdown/post-fetch matrix; verify every supported canonical genre, country, language, format, year, rating and content-rating filter is mapped, and unsupported dimensions are omitted rather than falsely advertised.
- [ ] Run all focused connector/provider/filter/cursor/receipt/catalog tests from Tasks 1–6, plus media source/UI locale tests and renderer/snapshot parity.
- [ ] Run full relevant Ruff/mypy, TypeScript/ESLint, and repository prescribed build if resource allows; report unavailable/environment-specific gates accurately.
- [ ] Independently review the pinned Phase 4 range for spec coverage, auth/data safety, cursor semantics, quotas, docs/JSDoc and regression gaps.
- [ ] Update `docs/IMPLEMENTATION_STATUS.md` and `docs/superpowers/plans/EXECUTION.md`; run final GitNexus `detect_changes` and commit the completed Phase 4 checkpoint locally on the current branch.

## Delivery limits

This plan does not authorize push, deploy, Docker/E2E, or live provider calls. Request owner approval for live Hardcover/MusicBrainz/IGDB/other provider acceptance if it becomes necessary; continue all mock/fixture-based implementation and verification independently.