# Dashboard UI/UX improvement plan

Date: 2026-10-30 · Branch: `feature/fix-ui` · Design rationale: `docs/ux-proposals/2026-10-30-ledger-redesign.md`

## Evidence

The findings come from captures by the e2e harness of real dashboards:

- **Full dashboard:** `dashboard-datasources` connects every keyless source and the scoped wizard sources, and builds one dashboard of about 55 gadgets.
- **All gadget types:** `screens-capture` builds an "All gadget types" dashboard with one gadget per template, bound to the best compatible source.

The command for a run is:

```
E2E_KEEP_DATA=1 E2E_GLOBAL_TIMEOUT_MIN=150 E2E_MAX_FAILURES=0 SCREENS_DIR=<abs>/.taste-skill/<run> \
  ./scripts/dev.ps1 test -E2eOnly -E2eTarget "dashboard-datasources|screens-capture"
```

A run takes about 50 minutes. Its output is a capture of every screen, every dashboard (both the first screen and the full page) and a close-up of each gadget, written to `.taste-skill/<run>/`. The baselines are `.taste-skill/full` (before step 0) and `.taste-skill/after3` (after step 0).

## Rules for every step

- One step is one reviewable change set. Do not mix steps.
- Gadget-scoped steps (1, 2, 4) need no global token change. Step 3 changes tokens and needs the §8 decisions in the proposal.
- Before editing a symbol, run GitNexus impact. Before committing, run `detect_changes`.
- Add a JSDoc block to every changed function.
- Use semantic tokens only, and keep the en-us and vi-vi messages in sync.
- Run the smallest proof for each step: targeted vitest, `tsc`, and eslint on the changed files. Then make one e2e capture and compare it with the previous run.

## Step 0: Correctness and size fixes (done, awaiting the after3 capture)

| Fix | File |
|---|---|
| The route overlay closes once the dashboard is ready, instead of after up to 10 s more of gadget fetches. | `core/app-shell/route-loader.tsx` (+ test) |
| Gadget header is a container query. Actions are never clipped. The Ask label and badges hide when the gadget is narrow. | `modules/dashboard/gadget-frame.tsx` |
| "Today" no longer appears twice in the daily brief. | `modules/dashboard/daily-brief.tsx` |
| The map has no empty plot box when coordinates are hidden. | `gadgets/map-gadget.tsx` |
| The top bar no longer overflows at 390 px. | `core/app-shell/workspace-switcher.tsx`, `globals.css` |
| Default sizes per renderer, used on the server and the client, with a fallback to the minimum. | `modules/dashboard/gadgets.py`, `layouts.py`, `public.py`, `dashboard-grid.tsx`, `dashboard-page.tsx` (+ test) |
| Tasks and Goals no longer show a false "filters unavailable" error for default map filters. | `modules/dashboard/api.ts` (+ test), `tasks-gadget.tsx`, `goals-gadget.tsx` |
| The 3D Earth globe is visible: a distinct sphere plus world-atlas countries. | `gadgets/globe-map.tsx` |
| The weather gadget no longer prints a raw source UUID. | `gadgets/weather-panel.tsx` |
| The client minimum sizes for `provider_observation` and `intelligence_panel` were missing. | `dashboard-grid.tsx` |

## Step 1: Gadget bodies (gadget-scoped, low risk)

| # | Change | Where | Done when |
|---|---|---|---|
| 1.1 | "Ask AI about selection (0)" becomes an outline button. It is disabled and quiet with no selection, and becomes primary when there is one. | `gadgets/table-panel.tsx` | The 20 table gadgets show no filled green button at rest. |
| 1.2 | News feed has one header level. Remove the inner "News stories" header row and the per-story bordered cards. Stories are a divided list, and refresh is a small icon button in the body toolbar. | `gadgets/news-feed.tsx`, `feed-gadget.tsx` | A feed shows at most one line of chrome above its stories. |
| 1.3 | Human labels instead of raw identifiers: metric ids such as `fear_greed_index` and `temperature_2m`, and reason codes such as `method_data_license_unverified`. Use one `formatMetricLabel` helper with an i18n map of known metrics, falling back to "Sentence case". Reason codes map to sentences. | `provider-observation-gadget.tsx`, `weather-panel.tsx`, `intelligence-panel.tsx`, messages | No snake_case is visible in the all-types capture. |
| 1.4 | One setup/empty/error notice for every gadget: one sentence saying what is missing and one "Configure" link. Red is used only for real errors. | New `gadgets/gadget-notice.tsx`, used by tasks, goals, activity, watchlist, highlights, chart, feed, map, message list | All unconfigured gadgets look the same, with no red unless something failed. |
| 1.5 | Loading skeleton rows shaped like the final content (list, table, metric, chart) replace the loading sentences. | `gadget-notice.tsx` (`GadgetSkeleton`), the same gadgets | The 55-gadget first screen shows shapes, not repeated "Loading…" text. |
| 1.6 | The stale-sources panel collapses to one line, for example "6 sources failing · Review", linking to Settings › Sources. It expands on demand. | `dashboard-page.tsx` | The bottom of the dashboard has no multi-row error list. |

Proof: vitest for the touched gadgets, `tsc`, eslint, and one e2e capture compared with `after3`.

## Step 2: Dashboard layout and header

| # | Change | Where |
|---|---|---|
| 2.1 | Title row: title 24/600 without the "DASHBOARD" eyebrow. The group tabs are hidden when there is only one group. A summary line reads "Updated 3 min ago · 2 sources failing". | `dashboard-page.tsx` |
| 2.2 | A "Tidy layout" action in edit mode packs placements top-left with no holes. It reuses `findFreeCoordinates` and is undoable through the existing history. | `dashboard-page.tsx`, `layout-editor.tsx` |
| 2.3 | Preset layouts fill all 20 columns: the last item in each row stretches to the edge. | `modules/dashboard/layouts.py` (+ test) |
| 2.4 | Gadget titles in presets drop the "<Dashboard> · " prefix. | `public.py` preset apply |

## Step 3: Visual system (approved 2026-10-30: §8 decisions 1–5 = A)

| Change | Where |
|---|---|
| Ledger tokens: grounds, lines, text, a near-black primary, and green accent only on data. | `globals.css` + `docs/design-system/tokens.json` |
| Radii 8/6/4. No resting shadows on gadgets. One border layer. | `components/ui/card.tsx`, `gadget-frame.tsx` |
| Controls are 36 px for fine pointers and 44 px for coarse. Body text is 14 px. | `components/ui/button.tsx`, `input.tsx`, `globals.css` |
| The ✦ Ask icon is muted, not green, on every gadget. | `gadget-frame.tsx` |
| Self-hosted Inter. | `app/layout.tsx` |
| Shell: a 56 px icon rail with the logo at its top (labels shown on hover) replaces the 240 px sidebar. Rail and top bar merge into one 48 px row. | `core/app-shell/workspace-shell.tsx`, `globals.css` |

## Step 4: Mobile

| Change | Where |
|---|---|
| The "Mobile reading sizes are shown…" notice shows once and can be dismissed. | `dashboard-page.tsx` |
| Connection status becomes a dot in the top bar, and the footer row above the bottom nav is removed. | `core/app-shell` status footer |

## Step 5: Freshness and provenance (approved 2026-10-30: §8 decision 6 = A)

| Change | Where |
|---|---|
| A freshness strip under the title: one tick per source on a 24 h axis, with stale sources marked. | New `modules/dashboard/freshness-strip.tsx` |
| Each gadget footer carries provenance in words, for example "Open-Meteo · 12 min ago". | `gadget-frame.tsx` + `gadget-freshness.tsx` |

## Step 6: Backend follow-ups (owner decision)

- Preset apply should offer to bind the matching enabled sources. Today every preset gadget lands unbound.
- The e2e harness is flaky on single gadget adds: the Add dialog sometimes fails to open within 15 s. Make `addGadget` retry the dialog once.

## Follow-ups found while verifying

- Final capture `.taste-skill/after6` (2026-10-31): 115 passed, 5 skipped, 3 failed (GDELT 429 timeout, Wikipedia daily-publication lag, arXiv feed). All three are remote providers. No console errors, no screen stuck on the loader.
- The Add-gadget template list takes over 15 s to load on a dashboard with ~55 live gadgets (e2e now waits up to 60 s). Profile `/gadget-renderers/compatible-counts`.
- News stories still repeat "Some relevance inputs are unavailable" under every story. Collapse it to one line per list.
- Settings and Chat screens have not been redesigned yet (proposal §6 Chat and Settings).
- Done since: the globe fills its plot with stronger contrast, and the chart ticks are adaptive and distinct.

- Globe: draws now, but small and low-contrast in light theme. Fill the plot (camera altitude) and use stronger land/ocean contrast.
- Finance chart: y-domain, compact ticks and flat fill are unverified on real data (the diag runs had no sources).
- Flat map: verified (right way up, no antimeridian streaks).

## Order and status

| Step | Status |
|---|---|
| 0 | Done and verified in captures after3/after4/diag2 (globe fixed: polished could not parse `transparent`) |
| 1 | Done and verified in after4 |
| 2 | Done and verified in after6 |
| 3 | Done and verified in after4/diag2 |
| 4 | Done and verified in after6 |
| 5 | Done and verified in after6 |
| 6 | Done (preset source proposal, e2e Add-gadget waits) and verified in after6 |
