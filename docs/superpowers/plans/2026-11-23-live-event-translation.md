# Live map NEWS: per-item translation (`live_event`)

Owner decisions (2026-11-23):
- Add per-item Translate for NEWS events on the dashboard live map gadget through a new translation resource type `live_event`.
- Approved ADR exception to `docs/superpowers/plans/2026-11-04-live-world-map.md:15` ("no provider content in database…"): only for owner-requested `live_event` translations, title ≤500 / summary ≤1000 chars, stored with the translation for at most 30 days (existing `expires_at` + nightly `expire_translations`).
- Owner only. Members get no button and the server answers `resource_unavailable`.
- No "translate all": per-record button only (same as other gadgets).

## Why inline source (not a server lookup)
- The only server cache is per-process memory in the API (`modules/dashboard/live_map_routes.py:35`, `_cache`, 30–60 min); the ARQ worker cannot read it; 2 API workers.
- No Redis/DB copy by design; browser keeps events in IndexedDB 30 days (`apps/web/src/modules/dashboard/live-feed/store.ts:13`); some NEWS sources (hackernews, devto) are browser-direct and never reach the server.
- So the owner's client sends title + summary inline; the server recomputes the revision hash from that text and stores it on the translation row (`source_text`), which the worker reads back.

## Backend (slice 1)
1. Migration `infrastructure/postgres/migrations/versions/0029_translation_live_event.py` (`down_revision = "0028_translation_doc_type"`, copy `_replace_check` from 0028): add `content_translations.source_text JSONB NULL`; widen `ck_content_translations_resource_type` and `ck_translation_batch_items_resource_type` to include `'live_event'`. Downgrade: delete `live_event` rows in both tables, drop column, restore 3-value checks.
2. `modules/translations/models.py`: `RESOURCE_TYPES` += `"live_event"`; `ContentTranslation.source_text: Mapped[dict[str, str | None] | None] = mapped_column(JSONB)` ("inline client snapshot; live_event only").
3. `modules/translations/schemas.py`: `ResourceType` += `"live_event"`; `LiveEventSource(title 1..500, summary ≤1000 | None, extra=forbid)`; `TranslationItemRequest.source: LiveEventSource | None = None` + validator rejecting `source` on non-live_event; do NOT require it on the item (worker `worker.py:300,646` rebuilds items without it); `TranslationBatchRequest` validator requires `source` for every `live_event` item.
4. New `modules/translations/live_events.py` (imports models/schemas/service only, not public): `live_event_revision(title, summary)` = sha256(`f"{title}\n{summary or ''}"`) hex; `live_event_input(scope, resource_id, source, revision)` → TranslationInput fields `{title, excerpt: summary or ""}`, visibility_hash sha256(`live_event:{workspace_id}:owner`), `local_only=False`, `source_ids=()`; `request_verdict(scope, item)` → `"resource_unavailable"` (non-owner or no source) / `"stale_request"` (revision mismatch) / input; `read_live_event_translation_input(session, *, scope, multi_workspace_enabled, event_id)` → None unless owner, else newest row's `source_text` for (workspace, actor, live_event, id, source_text not null).
5. `modules/translations/inputs.py` `_OWNERS` += `"live_event": ("modules.translations.live_events", "read_live_event_translation_input", "event_id")`.
6. `modules/translations/public.py`: `_authorize_items` handles `live_event` via `live_events.request_verdict` before `_authorize` (first submit has no row); `request_translations` stores `source_text=item.source.model_dump() if item.source else None` (+1 line; function is already over the length limit).
7. `modules/translations/service.py`: `_TITLE_EXCERPT = ("news_story", "document", "live_event")` used where title/excerpt types are checked (~:243, ~:257).
8. Docs: ADR entry in `docs/ARCHITECTURE_DECISIONS.md` (exception above); status line in `docs/IMPLEMENTATION_STATUS.md`.
Tests: new `tests/unit/modules/translations/test_live_event_input.py` (schema bounds/required source/rejected source on other types; hash vector for `live_event_revision("Hello","World")` shared with Vitest; verdicts member/mismatch/owner; reader member None + newest snapshot; translate_input live_event too-large + normal path); extend `test_translations.py` with mixed batch owner-persisted vs member-unavailable; a test that a hash-derived UUID (non-standard version nibble) is accepted.

## Frontend (slice 2, after `npm run gen:api --workspace apps/web`)
9. `apps/web/src/modules/translations/types.ts`: `ResourceType` += `'live_event'`; request item `source?`. `use-content-translation.ts`: `TranslationTarget` gains optional `source`; `runTranslationGroup` forwards it.
10. New `apps/web/src/modules/dashboard/live-feed/translation-target.ts`: `liveEventTarget(event)` via `crypto.subtle.digest` (id = first 32 hex of sha256(`live_event:` + event.key) as 8-4-4-4-12; revision = sha256(title + "\n" + (summary ?? "")) hex; source {title, summary}); `isNewsEvent` (`gadgets/event-kind.ts` label NEWS); hook `useLiveEventTranslation(events, enabled)` → `{ controller, targetOf(key) }` using `useItemTranslation('live_event', …)`, stale-async guarded.
11. `gadgets/live-map-gadget.tsx`: owner-only; list rows (`li` + `group/item`, translated title, `ItemTranslateButton` first in links cell), terminal rows (`live-map-terminal.tsx` `TerminalEventRow` optional `translation` prop), selected-event popup (translated title/excerpt + button). Translation state (pending/ready toggle/unchanged/error) always visible; idle button revealed on row hover/focus/touch (same pattern as feed rows). No new i18n keys.
Tests: `translation-target.test.ts` (UUID format, stable id, revision parity vector), `live-map-gadget.test.tsx` (owner NEWS row has Translate and shows translated title; non-NEWS row and member view have none), `use-content-translation.test.ts` (source forwarded in POST body).

## Gates
Backend: ruff format/check, mypy modules/translations, lint-imports, pytest tests/unit/modules/translations, function-length & file-size scripts, alembic migration file check. Frontend: gen:api, vitest (translations, live-feed, live-map gadget, locales), tsc, eslint, prettier. GitNexus impact before each symbol edit; detect-changes before commit.
