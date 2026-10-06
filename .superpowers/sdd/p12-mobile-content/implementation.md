# P12 mobile content and draft consistency

## Source freeze

- Author branch: `codex/umwelt-p12-mobile-content`.
- Composed accepted `develop` at `f6da2e2d5bb71c5721715cd118bfc12dfde949b0` by fast-forward; the mobile implementation remains a working-tree delta on that base.
- GitNexus tools were unavailable. Caller relationships below were traced in source; no graph impact claim is made.
- Production delta: dashboard grid/page/frame/editor/registry, EN/VI dashboard messages, and bounded scrolling/fixed plot sizing in the renderer files listed in Git status.

## Ownership and behavior

- `DashboardPage` owns the saved-layout projection, dirty edit origin, complete history snapshots and Save request. `projectMobilePlacements` performs local recovery only for malformed stored mobile geometry. `projectReadableMobilePlacements` remains view presentation and is adopted only by the explicit toolbar action. `isValidLayoutSnapshot` checks instance coverage, renderer minima, integer bounds, row endpoints and non-overlap before history accepts a callback and before Save captures items.
- `DashboardGrid` is called only by `DashboardPage`. It receives every instance and every draft rectangle; `visibleInstanceIds` controls rendering only. Pointer and keyboard commits resolve collisions over the complete snapshot. Escape clears transient pointer state without adding a history entry. Shared grid metrics derive from the observed grid width and stored columns.
- `LayoutEditor` is called only by `DashboardPage`. It exposes the optional mobile adoption action and preflights space before a quick gadget definition can be created; the instance handler repeats the bounded placement check before instance creation.
- `GadgetFrame` is rendered by `DashboardGrid` for each visible instance. Its single portal host is reparented between inline and expanded surfaces, preserving one renderer subtree; compact cards can scroll the outer labelled region to reach header controls, while ordinary card content scrolls inside its body.
- `widget-registry` resolves renderer components and owns bounded body floors for each registered ID plus the 180px shared fallback. The floors reserve source-based loading, plot, root padding and fixed controls without scaling with returned records. Map/chart plot roots and controls are kept from flex shrinking; the affected feed/entity/timeline/watchlist/table/GitHub/weather roots scroll instead of clipping content.
- Locale messages provide the mobile projection/pending/unavailable/adoption/origin text in English and Vietnamese.

## Source-only review limits

- Source checks only: callers and mutation paths were read directly; `git diff --check` reported no whitespace errors before the final source edits.
- No tests, lint, standalone typecheck, build, runtime, browser, SQL, migration, or environment operation was run. Parent owns source review and the serialized build slot; request that slot only after approval of this source freeze.
- Browser acceptance remains open for actual touch/focus reachability, narrow viewport map/chart behavior, fixed-header sizing under translated text, and content stability during streamed updates. Static source floors do not establish those runtime outcomes.
