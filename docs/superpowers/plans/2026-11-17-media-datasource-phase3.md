# Media datasource Phase 3 implementation plan

**Source of truth:** `specs/new-datasource-v2.md`, Phase 3 summary and Section 3.4 C–D. This plan divides that approved scope by file ownership; the specification remains authoritative.

## Global constraints

- Follow `AGENTS.md`, `docs/DESIGN_SYSTEM.md`, `docs/design-system/README.md`, and the existing Phase 1/2 contracts. Use semantic tokens, shadcn primitives, Lucide icons, and `en-us`/`vi-vi` catalogs.
- Keep media renderer filters, source scope, query serialization, and server filter names aligned. Preserve `local_only`, source selection, remote media policy, `useRendererHost` expand state, and the existing read/bookmark/dismiss behavior.
- Keep API cursor pagination and release ordering server-backed. Never derive provider data from UI-only `excerpt` parsing or direct browser provider fan-out.
- Every production function/component/hook changed or added needs adjacent documentation as required by `AGENTS.md`.
- Run GitNexus impact before editing production symbols and change detection before commits. Report HIGH/CRITICAL risk, including stale-index limits.

## Task 1: Wire media renderers into the dashboard editor

The Phase 1 media contracts already exist in this checkout: `DataShape`, `MediaProjection`, dashboard `GadgetScope`/`GadgetFilters`, and projection query serialization are present. Reuse these contracts. Add the missing renderer/template labels and gadget-editor support: `MEDIA_RENDERERS`, media field keys, `fieldsFor()`, persisted scope/filter payloads, media multi-chip pickers, editor validation/advanced-field visibility, template preview samples, and renderer geometry. Keep `gadget-renderers.json` / `gadget-templates.json` snapshots in parity. Do not rework projection APIs or implement the feed/calendar rendering components in this task.

Primary files: `apps/web/src/core/renderer-labels.ts`, dashboard gadget editor/model/picker/section/sample files, `grid-geometry.ts`, relevant dashboard tests, and renderer/template snapshots.

## Task 2: Media feed and release calendar gadgets

Add `FeedImage` aspect variants; shared, documented media cards/filter bar/filter popover/active chips/metadata badges; `media_feed` and `media_calendar` renderers; renderer lazy registration and media locale strings. Implement multi-source grouping, backend query/filter serialization, cursor pagination, attribution, remote-media notice, bookmarks/read/dismiss, Ask AI, filter reset/chips, and preserved expanded state as specified. Reuse existing dashboard, feed, chart, and shadcn patterns. A projection's null `image_url` does not say whether remote media is disabled or the provider has no image; use the existing privacy setting query to make the notice accurate. Update `docs/DESIGN_SYSTEM.md` and `docs/IMPLEMENTATION_STATUS.md` for the delivered renderers and supported filter patterns.

Primary files: `apps/web/src/components/feed-image.tsx`, new `apps/web/src/modules/dashboard/gadgets/media-parts.tsx`, `media-feed.tsx`, `media-calendar.tsx`, widget registry and tests, relevant dashboard locales, `docs/DESIGN_SYSTEM.md`, `docs/IMPLEMENTATION_STATUS.md`, and backend/frontend renderer/template snapshots.

## Task 3: Source configuration UI and YouTube scope

Add reusable `MediaFilterFields` to provider scope panels; register the Phase 2 media providers and required scopes; add bounded localized option specs and source labels; support multi-channel IDs, handles, playlists and long-form/Shorts options in `YoutubeScope`; fix localized enum rendering for labelled option specs; add the optional media starter pack. Optional filters must not block source wizard progress. Preserve schedule-step `null` reset semantics and builtin wizard omission semantics.

Primary files: `apps/web/src/modules/sources/provider-scope.tsx`, `scope-fields.tsx`, `provider-label-keys.ts`, `connector-editor-model.ts`, `guide-keys.ts`, `builtin-sources.tsx`, `builtin-option-specs.ts`, `builtin-option-keys.ts`, `builtin-options.tsx`, `starter-packs.ts`, source locales, and focused source UI tests.

## Task 4: General video panel and feed item Ask AI

Remove the YouTube-only source/hostname restriction from `VideoPanel`; add source tabs, timestamps, duration/creator metadata, search, cursor load-more, read/bookmark, and one-click Ask AI while respecting ownership and local-only rules. Allow the existing single-item Ask AI action in non-Telegram feed/media rows without changing the Telegram flow.

Primary files: `apps/web/src/modules/dashboard/gadgets/video-panel.tsx`, `feed-parts.tsx`, their focused tests, and any required dashboard locale entries.

## Phase acceptance

Run the Phase 3 frontend tests and required locale parity/key-usage, dashboard API/editor, gadget lazy registry, feed/video, source wizard/options/scope suites listed in `specs/new-datasource-v2.md`. Run focused ESLint, TypeScript, and production build checks as applicable. Run backend snapshot/renderer parity tests. Live providers, Docker/E2E, and browser acceptance are separate and must be reported as unrun unless actually executed.
