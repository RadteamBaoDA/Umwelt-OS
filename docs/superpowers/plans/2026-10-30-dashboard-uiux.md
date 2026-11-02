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

## Design-system unification and slop audit (2026-10-31)

Four review rounds (Opus, read-only) against six criteria: ledger consistency, no slop tells, layout/responsive, actionable states, gadgets/maps/charts, clean console. Each round was fixed and re-captured (`after7` → `after10`).

- **Closing verification (2026-10-31, rounds 8–11):** the round-7 backlog and every P2 found in rounds 8–10 are fixed.
  - The full capture `after16` (e2e 128/0) passed review round 10 with no P0 or P1. That round raised 7 more P2s.
  - Those 7 P2s were fixed and checked against `after17`, a screens-only capture (21/0, round 11). One leftover from that check, the 768 px Models field overlap, was fixed and checked against `after18` (11/0).
  - The console is clean in every capture.
  - Known residual: the CVE severity badge is covered by a render test. The live captures carried no severity word, so it has not been seen on screen.
- **Final verdict (after13, round 7, 2026-10-31):** no P0 or P1 left and all six criteria pass on a capture of the current code. e2e: 128 passed, 0 failed. Console is clean. Rounds 5–7 also fixed:
  - the old "BBD-OS Assistant" prompt
  - raw values in Global settings
  - the chart's false "UTC" label
  - a literal backspace in the severity regex
  - the 2-column tablet dashboard
  - the settings select below 1024 px
  - the removed chat banner
  - always-underlined link buttons
- **Round-7 P2 backlog:**
  - the timeline status line duplicates the empty state
  - disabled checkboxes have no visual cue
  - chart hour format and the under-1h range note
  - the globe in an empty map card
  - jargon on secondary screens (Memory page, MCP, Prompts, System export, Onboarding)
  - the source wizard subtitle repeats step 1
  - the 390 chat header "←" character
  - the Personal data permissions stub tab
  - "Last indexed: Not recorded" noise
  - collapsible filters on mobile Search/Timeline
- **Earlier verdict (after10):** no P0 or P1 left, all six criteria pass. Console is clean.
- **after10 e2e:** 114 passed, 6 skipped, 3 failed. All three failures were remote: arXiv, and a transient example.com rejection that blocked the `web` source and its table gadget.
- **Fixed across the rounds:**
  - Global CSS: border reset, ink links, flat skeletons, underline tabs, no resting shadows, radii 8/6/4 (12 for dialogs), 36/44 px controls, nothing under 11 px.
  - Docs: rewritten `tokens.json`, the design-system README and `DESIGN_SYSTEM.md`.
  - GenUI tokens aligned with the app.
  - News excerpts cleaned in the backend and frontend; measurement records no longer become stories.
  - Developer copy, raw enums and IANA zone ids replaced with plain language.
  - Contradictory status copy removed.
  - Top bar at 768 px; chart ticks; flat-map fit; weather order; Quick chat matches the Chat page; 390 px tabs scroll or become selects.
- **P2 backlog (deferred):**
  - A tablet (2-column) dashboard layout mode at 768 px.
  - News page refresh in the title row.
  - Measurement records listed in Documents (backend data model).
  - Highlights empty copy reuses the watchlist text.
  - Search "Load more sources" placement.
  - Web-search settings shows the env var name.
  - Onboarding step 1 shows an enabled "Previous step".
  - The 390 px freshness strip is cramped.
  - Error codes are humanized in English only.

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

## Checkpoint 2026-11-01 04:10 (paused by owner, nothing committed)

- **Round 14 fixes D1–D7** (fresh tick colour `--accent`, schedule nowrap, timeline spacing, brief clamp, setup/invite footers, provider card height): verified in capture final4 (129 passed).
- **Round 15** (8 items: chart toggle, globe context guard, phone map, quake excerpt, Members/Memory copy, chat border, palette hints): fixed; capture final5 (129 passed, console 0 bytes).
- **Round 16:** full sweep of all 353 final5 PNGs by 4 auditors: 0 P0, ~6 P1, ~22 P2.
  - Dashboard batch done (tsc clean):
    - Map engines in an absolute layer, so the footer stays in the card.
    - Phone/no-precise map shows one message and no empty box.
    - Globe re-fits and resumes on the next frame, with a lost-context fallback.
    - Map copy now matches the Configure link.
    - Recharts animation off.
    - Capture `settle` waits for "Loading…" status.
    - Daily brief padding and nowrap date row.
    - Dashboard header basis on phones.
    - Video count hidden while loading.
    - Weather values aligned.
  - Settings batch (in progress when paused):
    - Memory tabs overflow; `--scrim` token for dialog/sheet.
    - Settings-nav active state; legal selected state.
    - Wizard step bar; provider card centring.
    - Search mode span; Global-settings copy.
    - "Saved" on load; Realtime badge; schedule "—" while loading.
    - Models copy; disclosure summaries.
    - Invite input; palette chip height; onboarding stepper.
- **Resume steps:**
  1. Review the settings-batch diff (`git diff --stat`).
  2. From `apps/web`: `npx tsc --noEmit -p .` and `timeout 600 npx vitest run`. Two vitest failures were open: `provider-terms.test.tsx` en/vi key parity and `design-system-parity.test.ts` token parity. Likely the new `--scrim` / badge keys; mirror them in genUI tokens and the vi messages.
  3. Run the control-char scan.
  4. Run full capture final6: `SCREENS_DIR=.taste-skill/final6`, target `dashboard-datasources|screens-capture|system.spec|chat-live`.
  5. Run a 4-slice full audit (1440 / 768 / 390 / gadgets).
  6. Repeat until there are 0 P0/P1/P2.
- **Unverified risks:**
  - The Overview blank-globe root cause is unconfirmed (hardening only).
  - The `settle` wait applies to every route.

## Checkpoint 2026-11-01 20:00 (stopped by owner: CPU spike from parallel agents + hooks)

- Settings batch: all 16 items landed. Tracking-mode jargon replaced ("Live"/"Scheduled"). tsc clean, vitest 549/549.
- final6 capture: 129 passed, 353 PNGs, console 0 bytes (`.taste-skill/final6`).
- Overview blank globe root cause:
  - Old code drew the plot box even with precise off, and the globe paint raced.
  - The current code shows only the setup notice when precise is off.
- Round 17 audit was interrupted. Only the gadgets slice finished: 2 P1, 2 P2.
  - **P1, map footer still clipped (map-earth/map-flat).**
    - Cause: the inline `minHeight: bodyFloor` on the body div (`gadget-frame.tsx:~368`) beats `min-h-0`.
    - The map floor of 608px (`widget-registry.tsx:~80,89`) exceeds the default 9-row card (~545px body).
    - Fix: move `minHeight` to the inner host div (~384). Lower the map floor to `30*rem+32` (512).
  - **P2, duplicate loading block.** `map-gadget.tsx:~105` shows a skeleton above the drawn plot. Replace it with a small overlay inside the plot.
  - **P2, oversized Configure link.** `gadget-notice.tsx:~37` hard-codes `text-sm` in `actionClass`, so the link is bigger than the `text-xs` message. Remove it.
- Resume: 1440/768/390 slices still unaudited on final6.
  - Run them ONE AT A TIME (4 parallel agents x ~7 hooks per tool call, plus the remember plugin's post-tool bash, spawned dozens of bash trees and hit 100% CPU).
  - Agreed closing rule: round 17 is the last full sweep. Fix its list in one batch. Capture final7 and verify only those items. Later nits go to the backlog.
- **Round 17, 768 slice (112/112 reviewed): 0 P0, 0 P1, 4 P2.** All round-16 fixes hold.
  - **D1:** the map credit line shows when precise is off. Guard `map-gadget.tsx:~134` with `precise &&`. Change the `mapPreciseDisabled` copy to "Exact locations are hidden. Turn on location sharing in Data sources settings." (update vi too).
  - **D2:** the empty status `<p>` leaves a gap in System prompts. Make `SettingsPageHeader` (`settings-layout.tsx:~16`) skip an empty status.
  - **D3:** the daily brief "Ask AI about this day" wraps and is cut at 768. Shorten `askAboutDay` to "Ask AI" (update vi too).
  - **D4:** legal nav links are crammed. `policy-document.tsx:~27` gap-1 → gap-x-6 gap-y-1.
- Still unaudited on final6: 1440 and 390 slices.
- **Round 17, 390 slice (112/112 reviewed): 0 P0, 0 P1, 5 P2.** Round-16 fixes hold except the map height.
  - **D1:** empty or no-precise map cards keep the 608px map floor, leaving 450–550px of blank card. Fixed together with the gadgets P1: apply the map floor only when a map is drawn, else 204px.
  - **D2:** the daily brief floor of 180px is too small (`widget-registry.tsx:~66`). Raise it to 240.
  - **D3:** raw ISO reference date in `provider-observation-gadget.tsx:~126`. Format it as a date only, then update the test at :86.
  - **D4:** legal nav gap. Same fix as 768 D4; add `min-h-11` to the links.
  - **D5:** "See service health on the [System status]" is missing a trailing noun. Add " page."
- Still unaudited on final6: the 1440 slice only (the agent was killed by the owner).

## Result 2026-11-01 22:15: closing rule met (nothing committed)

- Round 17 was the last full sweep: all 4 slices of final6, 353/353 PNGs. 0 P0, 1 P1, 14 P2. All were fixed in one batch:
  - map floor 352 on the host
  - daily brief floor 240 and "Ask AI"
  - map loading overlay, credit guard, notice copy
  - Configure link size
  - legal nav gap
  - reference date formatting
  - "System status page." wording
  - empty settings status hidden
  - System status, onboarding and search copy and spacing
- final7 (full e2e: 129 passed, console 0 bytes) verification: 12/13 PASS, no new P0/P1. The open item was the Overview/World map with precise off: the 1440 card was blank.
- Fix: the base map is always drawn, with the setup notice overlaid (same as the no-locations state). The phone on-demand state shows the notice in flow.
- final8 (screens-capture: 11 passed) verified by direct inspection:
  - light/1440 Overview and dark/1440 World show the globe with the overlaid notice and the footer inside the card.
  - 390 shows the notice only.
- Checks: tsc clean, vitest 549/549.
- **Backlog (P2, not blocking):**
  - Phone notice-only cards (Map with precise off, Feed without sources, Highlights) keep their projected layout height, leaving ~200px blank. Fix needs a fit-to-content mode in the grid.
  - Admin terms in Global/n8n/Models ("Secure cookies", "Chat (LLM)", "Time precision").
  - Table gadget long strings scroll sideways at 390.
  - Wikipedia view counts have no thousands separator (provider text).
  - Earlier backlog items above still apply.
