# Built-in Source Options — Wave A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Built-in news/event sources resume from the last collected record (no loss, no duplicates), honour per-sync caps and keyword/age filters, and keep owner settings across Resume.

**Architecture:** A pure helper `select_incremental` (new `modules/connectors/selection.py`) picks which mapped records to accept, using the existing ISO `source_ingestion_state.cursor` as an inclusive lower bound plus the existing `seen_record_keys` dedup read **before** the cap. Preset feed / HN / pure runners in `collection.py` call it and pass `cursor_after` to the existing `_accept_native`. New optional `ConnectorConfig` fields (default `None` = today's behaviour) are allowlisted per provider in the registry and accepted by `enable_builtin`.

**Tech Stack:** Python 3 / FastAPI / Pydantic v2 / SQLAlchemy async; pytest (asyncio mode).

**Spec:** `docs/superpowers/specs/2026-11-02-builtin-source-options-design.md` (goals 1, 4, 7, 8; wave A row).

## Global Constraints

- No cursor format change: cursor stays a plain ISO-8601 UTC string (`datetime.isoformat()`), ≤4096 bytes.
- Do not modify `_apply_state_update`, generation scoping of `seen_record_keys`, coverage enums, or Google/GitHub/Telegram cursor handling.
- Never put cap/backlog/section/truncation info into `IngestionRecord.metadata` (it is hashed into `record_hash`).
- All new config fields default `None`; a built-in with no new fields must send the same request and accept the same records as today.
- Bounds (verbatim from spec): `max_items` 1..100 (default 100); keywords ≤20 terms each, each 1..64 chars; `max_age_days` 1..365; `schedule_interval_minutes` ∈ {15, 30, 60, 360, 1440}; `name` 1..200 chars.
- Keyword match: whole-word / phrase on folded tokens — lowercase, NFKD with combining marks stripped, `đ→d`, split on non-alphanumerics. Include = any-of; exclude wins.
- Every new/changed named function gets a docstring stating its contract (AGENTS.md "Mandatory Code Documentation").
- Run `gitnexus impact <symbol> --direction upstream` before editing each existing symbol named in a task; report risk. `_apply_state_update` is HIGH and must not be touched.
- No commits unless the owner asks (repo memory rule); "Commit" steps below mean "stage and stop" unless the owner has approved commits for this run.

## Review Focus

1. **Cap smaller than a run of already-seen items** (e.g. `max_items=2`, feed of 10 items all seen except the newest) → must still take the new item, never stall. Test in Task 1 and Task 4.
2. **Pause → Resume on a preset feed** (generation bump resets dedup scope) → next sync re-observes at most the items inside the skew window (`cursor − 10 min`); everything older is blocked by the cursor floor. Documents are not duplicated (unique on `accepted_record_hash`). Test in Task 4.
3. **Feed item edited** (same id, new `updated`, older `published`) → accepted as a new version, not dropped by the cursor. Test in Task 1.
4. **Feed crosses 100 items between polls** → same article keeps the same `record_hash`. Test in Task 3.
5. **Undated items** (`timestamp_basis == "collection"`) → accepted via dedup, never advance the cursor. Test in Task 1.

---

### Task 1: Pure selection helper

**Files:**
- Create: `modules/connectors/selection.py`
- Test: `tests/unit/modules/connectors/test_selection.py`

**Interfaces:**
- Produces:
  - `fold(text: str) -> tuple[str, ...]` — folded tokens.
  - `matches_keywords(text: str, include: Sequence[str] | None, exclude: Sequence[str] | None) -> bool`
  - `@dataclass(frozen=True) class SelectionFilters: include: tuple[str, ...] = (); exclude: tuple[str, ...] = (); max_age_days: int | None = None; max_items: int = 100`
  - `@dataclass(frozen=True) class Selection: taken: tuple[IngestionRecord, ...]; backlog: int; cursor_after: str | None; gap: tuple[str, str] | None; duplicates: int`
  - `select_incremental(records: Sequence[IngestionRecord], *, cursor: str | None, seen: Callable[[IngestionRecord], bool], filters: SelectionFilters, now: datetime, skew: timedelta = timedelta(minutes=10)) -> Selection`
  - `record_time(record: IngestionRecord) -> datetime | None` — `record.observed_at` when `metadata["provider_record"]["timestamp_basis"] != "collection"`, else `None`.

- [ ] **Step 1: Write the failing tests** (build records with a local helper `_rec(pid, observed_at, basis="provider_published", title="t")`; `seen=lambda r: r.provider_id in SEEN`)

```python
def test_fold_strips_vietnamese_diacritics_and_d_stroke():
    assert fold("Đà Nẵng – Giá vàng!") == ("da", "nang", "gia", "vang")

def test_keywords_match_whole_words_not_substrings():
    assert matches_keywords("Gà rán giá rẻ", ["ga"], None)
    assert not matches_keywords("New garage opens", ["ga"], None)
    assert not matches_keywords("Check your mail", ["AI"], None)
    assert matches_keywords("giá vàng hôm nay", ["gia vang"], None)   # phrase
    assert not matches_keywords("vàng giá", ["gia vang"], None)       # order matters

def test_exclude_wins_over_include():
    assert not matches_keywords("Quảng cáo giá vàng", ["gia vang"], ["quang cao"])

def test_oldest_first_cap_leaves_backlog_and_advances_cursor_to_last_taken():
    recs = [_rec(f"a{i}", T0 + timedelta(minutes=i)) for i in range(5)]
    s = select_incremental(recs, cursor=None, seen=lambda r: False, filters=SelectionFilters(max_items=2), now=NOW)
    assert [r.provider_id for r in s.taken] == ["a0", "a1"]
    assert s.backlog == 3 and s.cursor_after == (T0 + timedelta(minutes=1)).isoformat()

def test_seen_items_do_not_consume_the_cap():
    recs = [_rec(f"a{i}", T0) for i in range(10)] + [_rec("new", T0)]
    s = select_incremental(recs, cursor=T0.isoformat(), seen=lambda r: r.provider_id != "new",
                           filters=SelectionFilters(max_items=2), now=NOW)
    assert [r.provider_id for r in s.taken] == ["new"] and s.duplicates == 10

def test_cursor_is_inclusive_lower_bound_minus_skew():
    old, edge = _rec("old", T0 - timedelta(hours=1)), _rec("edge", T0 - timedelta(minutes=5))
    s = select_incremental([old, edge], cursor=T0.isoformat(), seen=lambda r: False, filters=SelectionFilters(), now=NOW)
    assert [r.provider_id for r in s.taken] == ["edge"]

def test_edited_item_with_newer_modified_time_is_taken():
    edited = _rec("x", T0 + timedelta(hours=2), basis="provider_modified")
    s = select_incremental([edited], cursor=(T0 + timedelta(hours=1)).isoformat(), seen=lambda r: False,
                           filters=SelectionFilters(), now=NOW)
    assert [r.provider_id for r in s.taken] == ["x"]

def test_undated_items_are_taken_last_and_never_move_cursor():
    s = select_incremental([_rec("u", NOW, basis="collection"), _rec("d", T0)], cursor=None,
                           seen=lambda r: False, filters=SelectionFilters(), now=NOW)
    assert [r.provider_id for r in s.taken] == ["d", "u"] and s.cursor_after == T0.isoformat()

def test_no_records_taken_keeps_cursor():
    s = select_incremental([], cursor=T0.isoformat(), seen=lambda r: False, filters=SelectionFilters(), now=NOW)
    assert s.cursor_after == T0.isoformat() and s.taken == ()

def test_max_age_drops_old_dated_items_only():
    s = select_incremental([_rec("old", NOW - timedelta(days=10)), _rec("u", NOW, basis="collection")],
                           cursor=None, seen=lambda r: False, filters=SelectionFilters(max_age_days=7), now=NOW)
    assert [r.provider_id for r in s.taken] == ["u"]

def test_gap_reported_when_oldest_returned_is_newer_than_cursor():
    s = select_incremental([_rec("a", T0 + timedelta(days=2))], cursor=T0.isoformat(), seen=lambda r: False,
                           filters=SelectionFilters(), now=NOW)
    assert s.gap == (T0.isoformat(), (T0 + timedelta(days=2)).isoformat())

def test_keyword_filter_uses_title_and_content():
    s = select_incremental([_rec("a", T0, title="Giá vàng tăng"), _rec("b", T0, title="Bóng đá")], cursor=None,
                           seen=lambda r: False, filters=SelectionFilters(include=("gia vang",)), now=NOW)
    assert [r.provider_id for r in s.taken] == ["a"]
```

- [ ] **Step 2: Run** `pytest tests/unit/modules/connectors/test_selection.py -v` — Expected: FAIL (module not found).

- [ ] **Step 3: Implement `modules/connectors/selection.py`.** Order inside `select_incremental`: (1) floor = `parse(cursor) − skew`, drop dated records with time < floor; (2) drop `seen(r)`, counting `duplicates`; (3) drop dated records older than `now − max_age_days`; (4) keyword filter on `metadata.get("title","") + " " + content`; (5) sort dated by `(time, provider_id)` ascending, undated after in input order; (6) take `max_items`, `backlog` = remainder; (7) `cursor_after` = max dated time among taken (ISO) else `cursor`; (8) `gap` = `(cursor, oldest_dated_returned)` when cursor set and the oldest dated record **in the input** is newer than `parse(cursor)`. Gap uses raw input (before dedup/filters).

- [ ] **Step 4: Run** the same command — Expected: all PASS.

- [ ] **Step 5: Stage** `git add modules/connectors/selection.py tests/unit/modules/connectors/test_selection.py`

---

### Task 2: Config fields and per-provider allowlist

**Files:**
- Modify: `modules/connectors/public.py` (`ConnectorConfig`, ~291-363 + validators)
- Modify: `modules/connectors/provider_specs.py` (after `BUILTIN_PROVIDERS`, ~608)
- Modify: `modules/connectors/registry.py:55-64`
- Test: `tests/unit/modules/connectors/test_selection_config.py`

**Interfaces:**
- Produces:
  - `ConnectorConfig.max_items: StrictInt | None = Field(None, ge=1, le=100)`
  - `ConnectorConfig.include_keywords: tuple[str, ...] | None = Field(None, max_length=20)` and `exclude_keywords` (same); each term stripped, 1..64 chars, empty tuple normalised to `None`.
  - `ConnectorConfig.max_age_days: StrictInt | None = Field(None, ge=1, le=365)`
  - `INCREMENTAL_PROVIDERS: frozenset[str]` in `provider_specs.py` = `{s.id for s in FREE_PROVIDER_SPECS if s.id in BUILTIN_PROVIDERS and s.data_kind in ("news", "event")} | {"google_news"}`
  - `COLLECTION_FILTER_FIELDS = frozenset({"max_items", "include_keywords", "exclude_keywords", "max_age_days"})` in `registry.py`
  - `selection_filters(configuration: Mapping[str, Any]) -> SelectionFilters` in `selection.py` (reads the four fields; `max_items` default 100).

- [ ] **Step 1: Write failing tests**

```python
def test_filter_fields_accepted_for_incremental_builtin():
    validate(_src("tuoitre_news", {"max_items": 20, "include_keywords": ["giá vàng"], "max_age_days": 3, **BASE}))

def test_filter_fields_rejected_for_measurement_builtin():
    with pytest.raises(ValueError, match="unsupported fields"):
        validate(_src("binance", {"max_items": 5, **BASE}))

@pytest.mark.parametrize("bad", [{"max_items": 0}, {"max_items": 101}, {"max_age_days": 366},
                                 {"include_keywords": ["x" * 65]}, {"include_keywords": ["k"] * 21}])
def test_filter_bounds(bad):
    with pytest.raises(ValidationError):
        ConnectorConfig(**bad)

def test_selection_filters_defaults_to_today():
    assert selection_filters({}) == SelectionFilters(max_items=100)
```
(`BASE = {"history_mode": "returned_snapshot", "schedule_interval_minutes": 30, "timeout_seconds": 30}`; `_src` builds an active `ConnectorSource` like `test_builtins._source`.)

- [ ] **Step 2: Run** `pytest tests/unit/modules/connectors/test_selection_config.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement.** In `registry.validate`, allowed keys become `scope_fields | common | {"history_mode"} | (COLLECTION_FILTER_FIELDS if provider in INCREMENTAL_PROVIDERS else ∅)`; import `INCREMENTAL_PROVIDERS` lazily inside the function if a module-level import cycles. Keyword sanitiser lives next to the existing `news_query` validator in `ConnectorConfig`.

- [ ] **Step 4: Run** the new test file plus `pytest tests/unit/modules/connectors/test_builtins.py tests/unit/modules/connectors/test_google_news.py -q` — Expected: PASS.

- [ ] **Step 5: Stage** the four files.

---

### Task 3: Hash-stable feed records

**Files:**
- Modify: `modules/connectors/providers/news.py:274-338` (`map_feed`, `_with_coverage`)
- Modify: `modules/connectors/providers/json_feed.py:170-178`
- Test: `tests/unit/modules/connectors/test_news_providers.py`, `tests/unit/modules/connectors/test_json_feed.py`

**Interfaces:**
- Produces: `map_feed(provider, body, collected_at) -> list[IngestionRecord]` now returns up to `MAX_FEED_ITEMS_SCANNED` (500) deduped records, newest first, all with `coverage="returned_snapshot"`. JSON feed mapper: same rule, bounded by its existing scan bound. `_with_coverage` deleted (no other importer after this task; grep to confirm).
- Consumes: nothing new.

- [ ] **Step 1: Write failing tests**

```python
def test_same_item_hashes_identically_below_and_above_100_items():
    small, big = _rss(items=50), _rss(items=150)   # same first item in both
    a = next(r for r in news.map_feed("bbc_world", small, NOW) if r.provider_id == ID0)
    b = next(r for r in news.map_feed("bbc_world", big, NOW) if r.provider_id == ID0)
    assert ingestion.native_record_key(a) == ingestion.native_record_key(b)

def test_map_feed_keeps_all_scanned_items_for_selection():
    assert len(news.map_feed("bbc_world", _rss(items=150), NOW)) == 150
```
Mirror both for one JSON feed provider in `test_json_feed.py`. (`native_record_key` comes from Task 4; if executing strictly in order, assert on `r.metadata == r2.metadata` here and switch to `native_record_key` in Task 4.)

- [ ] **Step 2: Run** `pytest tests/unit/modules/connectors/test_news_providers.py tests/unit/modules/connectors/test_json_feed.py -v` — Expected: new tests FAIL; update any existing assertion that expected `truncated` coverage or a 100-item result, noting each in the task report.

- [ ] **Step 3: Implement**: drop the `[:MAX_FEED_ITEMS]` slice and the coverage rewrite from both mappers. `MAX_FEED_ITEMS` stays exported (it is the `max_items` default).

- [ ] **Step 4: Run** the same command — Expected: PASS.

- [ ] **Step 5: Stage.**

---

### Task 4: Runners use incremental selection

**Files:**
- Modify: `modules/ingestion/public.py` (add `native_record_key`; refactor inline digest at ~2330-2341 to call it)
- Modify: `modules/connectors/collection.py` (`_run_feed` ~1165, `_run_hn` ~1191, `_run_pure` ~1108; new helper)
- Test: `tests/unit/modules/connectors/test_collection_incremental.py`

**Interfaces:**
- Consumes: Task 1 `select_incremental`, `SelectionFilters`; Task 2 `INCREMENTAL_PROVIDERS`, `selection_filters`.
- Produces:
  - `native_record_key(record: IngestionRecord) -> tuple[str, str]` in `modules/ingestion/public.py` — exactly `(record.provider_id, _digest({"version": record.version, "content": record.content, "metadata": record.model_dump(mode="json", exclude={"collected_at"})["metadata"]}))`; the accept path must use this same function so runner and transaction keys cannot diverge.
  - `async def _select(run: Run, lease: Any, records: Sequence[IngestionRecord], collected_at: datetime) -> tuple[list[IngestionRecord], str | None]` in `collection.py`: returns `(records, None)` unchanged when provider ∉ `INCREMENTAL_PROVIDERS`; otherwise opens `run.factory()`, calls `ingestion.seen_record_keys(...)` with `native_record_key` for all records, rolls back, runs `select_incremental(records, cursor=lease.cursor_before, seen=…, filters=selection_filters(source.configuration or {}), now=collected_at)`, stores `backlog`/`gap`/`duplicates` in `run.extras["selection"]`, returns `(list(s.taken), s.cursor_after)`.
  - `_run_feed`, `_run_hn`, `_run_pure` call `_select` after mapping and pass `cursor_after=` to `_accept_native`. Coverage passed stays `_coverage(taken)` / `"returned_snapshot"`.

- [ ] **Step 1: Write failing tests** (reuse `test_collection.harness`/`attempt` patterns; stub `_lease` to return `SimpleNamespace(token="t", cursor_before=CUR)`, stub `_get_body` to return a fixed feed body, capture `_accept_native` kwargs, stub `ingestion.seen_record_keys`)

```python
async def test_feed_run_passes_cursor_after_and_caps_oldest_first(monkeypatch): ...
    # config {"max_items": 2}, 5 dated items newer than CUR -> accepted == 2 oldest, cursor_after == 2nd item time

async def test_resume_after_generation_bump_only_reobserves_skew_window(monkeypatch): ...
    # feed items at T0, T0+1h, T0+2h; cursor = T0+2h; seen_record_keys -> set() (new generation)
    # assert accepted ids == ["item@T0+2h"] (inside skew), cursor_after == (T0+2h).isoformat()

async def test_measurement_provider_is_untouched(monkeypatch): ...
    # provider "binance": records passed through unchanged; _accept_native gets cursor_after=None

async def test_cap_smaller_than_seen_run_still_takes_new_item(monkeypatch): ...
    # config {"max_items": 1}; 10 items seen + 1 new at same timestamp -> accepted == [new]

def test_accept_path_uses_native_record_key(): ...
    # refactored accept-path digest and native_record_key(record) return the same pair
```

- [ ] **Step 2: Run** `pytest tests/unit/modules/connectors/test_collection_incremental.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces. `gitnexus impact` first on `_run_feed`, `_run_hn`, `_run_pure`, `accept_native_collection`.

- [ ] **Step 4: Run** `pytest tests/unit/modules/connectors tests/unit/modules/ingestion -q` — Expected: PASS (fix any pinned expectation that assumed newest-100 feed output, and list each in the report).

- [ ] **Step 5: Stage.**

---

### Task 5: Guard the no-change cursor write

**Files:**
- Modify: `modules/ingestion/public.py` (`accept_collection_no_changes` ~2659-2713)
- Modify: `modules/connectors/collection.py:_accept_generic` (~444-466) to pass the expected cursor
- Test: `tests/unit/modules/ingestion/test_public.py` (or the file that already covers `accept_collection_no_changes`; grep)

**Interfaces:**
- Produces: `accept_collection_no_changes(..., expected_cursor: str | None | object = _UNSET)` — when supplied and `state.cursor != expected_cursor`, raise `HTTPException(409, "Collection cursor changed")` before any write. `_accept_generic` passes `expected_cursor=cursor_before`. Callers that omit it keep today's behaviour.

- [ ] **Step 1: Write failing test** `test_no_change_settlement_rejects_moved_cursor` — state row cursor `"B"`, call with `expected_cursor="A"` and `state_update.cursor_after="C"` → 409, cursor still `"B"`, nothing settled.
- [ ] **Step 2: Run** it — Expected: FAIL.
- [ ] **Step 3: Implement** (do not touch `_apply_state_update`). `gitnexus impact accept_collection_no_changes` and `_accept_generic` first.
- [ ] **Step 4: Run** `pytest tests/unit/modules/ingestion tests/unit/modules/connectors/test_collection.py -q` — Expected: PASS.
- [ ] **Step 5: Stage.**

---

### Task 6: Enable/Resume keeps owner settings and accepts options

**Files:**
- Modify: `modules/connectors/builtins.py` (`BuiltinEnableRequest` :73-80, `enable_builtin` :174-295)
- Modify: `apps/web/src/modules/sources/api.ts:546-554` (`enableBuiltinSource` options type only)
- Test: `tests/unit/modules/connectors/test_builtins.py`

**Interfaces:**
- Consumes: Task 2 fields and `INCREMENTAL_PROVIDERS`.
- Produces:
  - `BuiltinEnableRequest` adds optional `name: str | None (1..200)`, `schedule_interval_minutes: Literal[15,30,60,360,1440] | None`, `max_items`, `include_keywords`, `exclude_keywords`, `max_age_days` (same bounds as `ConnectorConfig`).
  - Precedence per field in step 3 of `enable_builtin`: request value → `source.configuration.get(field)` → spec default (`spec.default_interval_minutes` for interval, `None` otherwise). Same rule for existing `realtime_mode`.
  - Filter fields sent for a provider ∉ `INCREMENTAL_PROVIDERS` → 422 `"Provider configuration is invalid"` (via the existing `registry.validate` try/except).
  - `name` used only in `SourceCreate(name=payload.name or spec.label, …)`; ignored when reusing a source.
  - TS: `options?: SourceAiOptions & { realtime_mode?: RealtimeMode; name?: string; schedule_interval_minutes?: 15|30|60|360|1440; max_items?: number; include_keywords?: string[]; exclude_keywords?: string[]; max_age_days?: number }`.

- [ ] **Step 1: Write failing tests** (use `_World`; let `saved` also persist `desired` into `self.source.configuration` so a second enable sees it)

```python
async def test_resume_keeps_saved_interval_and_filters() -> None:
    with _World("tuoitre_news") as world:
        await builtins.enable_builtin("tuoitre_news", _session(), _request(), SCOPE,
            builtins.BuiltinEnableRequest(schedule_interval_minutes=60, max_items=20, include_keywords=("giá vàng",)))
        await builtins.disable_builtin("tuoitre_news", _session(), _request(), SCOPE)
        await _enable("tuoitre_news")
    cfg = world.row.desired_configuration
    assert cfg["schedule_interval_minutes"] == 60 and cfg["max_items"] == 20 and cfg["include_keywords"] == ["giá vàng"]

async def test_enable_uses_spec_default_when_nothing_saved() -> None:
    with _World("tuoitre_news") as world:
        await _enable("tuoitre_news")
    assert world.row.desired_configuration["schedule_interval_minutes"] == 30
    assert "max_items" not in world.row.desired_configuration

async def test_enable_names_new_source() -> None: ...   # create_source called with SourceCreate.name == "TT kinh tế"

async def test_filter_fields_on_measurement_builtin_are_422() -> None:
    with _World("binance"), pytest.raises(HTTPException) as exc:
        await builtins.enable_builtin("binance", _session(), _request(), SCOPE,
                                      builtins.BuiltinEnableRequest(max_items=5))
    assert exc.value.status_code == 422
```

- [ ] **Step 2: Run** `pytest tests/unit/modules/connectors/test_builtins.py -v` — Expected: new tests FAIL.
- [ ] **Step 3: Implement.** Serialise only non-`None` values into `ConnectorConfig` so `desired` equality (and therefore "no re-save when unchanged") still holds for default sources. Update the `enable_builtin` docstring with the precedence rule. `gitnexus impact enable_builtin` first.
- [ ] **Step 4: Run** `pytest tests/unit/modules/connectors/test_builtins.py -q` and `cd apps/web && npx tsc --noEmit` — Expected: PASS / no errors.
- [ ] **Step 5: Stage.**

---

### Task 7: Wave gate

- [ ] **Step 1:** `pytest tests/unit -q` — Expected: all pass (report counts).
- [ ] **Step 2:** `ruff check modules tests && mypy modules/connectors modules/ingestion` — Expected: clean.
- [ ] **Step 3:** `cd apps/web && npx tsc --noEmit && npx vitest run src/modules/sources` — Expected: pass.
- [ ] **Step 4:** `gitnexus detect-changes` (or `npx gitnexus detect_changes --scope all`) — Expected: only symbols named in Tasks 1-6.
- [ ] **Step 5:** Report to owner: test counts, any pinned expectations changed (with reason), anything not run (Postgres integration needs owner approval).
