# Umwelt-OS UI redesign proposal: "Ledger"

Status: proposal, not implemented. Needs owner approval for the items marked **Decision**.
Source of method: taste-skill app-screen run, style `achromatic-ledger` (seed 75673334), shell redrawn to `icon rail`
because the dealt "no sidebar, centred 1152px column" contradicts DESIGN_SYSTEM.md §8 (full-width shell, sidebar nav).
Palette and type kept from the brand (`--keep palette,type`). Evidence: source review of `globals.css`, the shell,
`gadget-frame.tsx`, `dashboard-page.tsx`, chat layout, plus 216 before-screenshots (32 screens × 1440/768/390 ×
light/dark) captured by `tests/e2e/screens-capture.spec.ts` on a disposable e2e stack into `.taste-skill/before/`.
Data was empty apart from one preset dashboard, so the screenshots show first-run and empty states.

## 2a. Observed in the screenshots

| Screen | Observation |
|---|---|
| Dashboard 1440 | Gadget grid ends at ~1180px, leaving ~240px of dead space on the right. The two rows are ragged (Feed card 460px tall, Highlights 400, Map 580). Headers clip their actions ("Ask with U", "Ask wi"). Every title repeats the dashboard name ("Overview · Feed"). The Highlights card is a shield icon in empty space. The Daily brief shows "Today" twice and a native date input. "DASHBOARD" eyebrow above a 30px bold title. |
| Dashboard 390 | The top bar overflows to the left (the Quick chat button is cut off at the screen edge). Title row actions wrap onto 2 lines. A green info notice about mobile sizes. The status footer and bottom nav take ~110px. |
| Chat 1440 | Template empty state: robot icon in a tinted circle, three cards with an icon each, a pill row with a green "All" pill, a green sparkle chip. The thread sits in an 18px panel with its own icon rail beside the main sidebar (two rails). The send button is pale green while disabled. |
| Settings › Data sources | Five equal-weight action buttons (one green) wrap to two lines, right-aligned over an empty list. "Data sources" appears three times (inner nav, tab, 30px h1). Below the 560px panel the page is empty. |
| Settings › Models | "SETTINGS" eyebrow + 30px h1. Descriptions at 11–12px, smaller than the controls. The "Saved" stamp shows before anything was edited. Nested bordered panels inside the bordered settings panel (three levels). |
| Source wizard | Provider tiles say "Available · Collection code is wired", which is developer language. Each category is a bordered box holding bordered tiles. Next is a pale green button when disabled. |
| Login | "PRIVATE WORKSPACE" eyebrow, "Welcome back" greeting, 18px card with a shadow. Green links in the footer. |
| Status footer | Two 44px tinted circles for API and realtime, with no words; the meaning only shows on hover. |
| Dark | Navy-green ground (#141a17); the green accent on every nav label and link makes the chrome louder than the content. |

## 2b. Full dashboard run (2026-10-30)

Run: `dashboard-datasources` (every keyless source plus scoped wizard sources, one gadget each, about 55 gadgets) followed by `screens-capture`, which also applied all 8 presets. Result: 113 passed, 3 failed (frankfurter UI timeout, arXiv weekend feed empty, and the resulting 2-3 missing gadgets). Screens are in `.taste-skill/full/`.

| Finding | Where | Fix |
|---|---|---|
| A dashboard with many gadgets shows only a full-screen spinner. All 12 captures (each taken after more than 7 s) were blank. | `core/app-shell/route-loader.tsx`: dashboard routes wait for readiness and then up to 10 s more while gadgets fetch. | Close the overlay as soon as the dashboard itself is ready. Gadgets show their own loading states. |
| Gadget actions are cut off ("Ask with U"), and the expand button is lost in narrow gadgets. | `gadget-frame.tsx` header: the title is fixed at 65% width and does not shrink. | Header becomes a container query. The Ask label shows only when there is room, and the actions are always visible. |
| The daily brief shows "Today" twice. | `daily-brief.tsx` relation badge next to the DateSelector's Today button | Show the badge only when the selected day is not today. |
| The map shows an empty white box when coordinates are hidden. | map renderer | No empty plot box. The explanatory text is the empty state. |
| At 390 px the top bar overflows to the left. | shell top bar | Quick chat becomes icon-only, and the workspace switcher truncates. |
| Presets applied after ~50 sources were enabled still bind no source ("Choose at least one source"). | preset apply (backend) | Not a UI fix: presets should offer matching enabled sources. Open. |
| Preset layouts use about two thirds of the width and leave rows ragged. Every title repeats the dashboard name ("World · Feed"). | preset definitions (data) | Open: preset layouts should fill the columns, and titles should drop the dashboard prefix. |

## 1. What the app is

- One owner, self-hosted. Opened daily: a glance at the dashboard in the morning, long chat sessions later.
- Three destinations: **Dashboard** (user-composed gadgets on a 20-column grid: news, markets, weather, maps,
  feeds, tasks, briefs), **Chat** (threads, citations, evidence rail, quick-chat Sheet), **Settings** (data sources,
  models, dashboard & gadgets).
- What makes it this product and no other: every value has a **source and an age**. The brand voice already says
  it: "calm, dense, honest about what it knows". The connection footer and the gadget freshness badge are the
  seeds of that, but they are presented as chrome, not as the product.

## 2. Findings (current UI)

| # | Finding | Where | Rule broken |
|---|---|---|---|
| 1 | Brand green is spent on chrome: `text-primary` 90×, `border-primary` 27×, `bg-primary/10` 19×, green primary buttons, green active tab underline, green "Ask Umwelt" sparkle in every gadget header, green tinted reading-queue bar. Data (charts) has to compete with it. | components-wide | colour on data, not chrome |
| 2 | Middle radii everywhere: panels 18px, sub-panels 12px, controls 9px, gadgets `rounded-lg`. Reads soft and template-like. | `globals.css`, `@theme` | radius bimodal: 4–8 or 16–24 |
| 3 | Shadows on resting cards (`shadow-xs` on every gadget, `shadow-sm` on shadcn Card default, auth card shadow). | `card.tsx`, `gadget-frame.tsx` | border **or** shadow, never both |
| 4 | Landing-page titles: global `h1` clamp(27–36px) at −0.04em; dashboard h1 30px bold; empty-state h2 20px bold. | `globals.css:79`, `dashboard-page.tsx:1040` | app h1 18–32, median 24/600 |
| 5 | Shell spends width on three links: 240px labelled sidebar + 64px top bar whose left two thirds are empty (only right-aligned actions). | `.workspace-nav`, `.topbar` | empty top bar flagged; ≤5 destinations → rail |
| 6 | Gadget header noise: outline badge for freshness, primary badge for unread, uppercase tracked 10px severity badge, `font-mono text-[10px]` revision, 11px scope chips in mono. | `gadget-frame.tsx` | no text under 11px, no tracked caps, mono only for IDs |
| 7 | Loading skeleton is a shimmering gradient. | `.skeleton` | flat grey bars at real size |
| 8 | `muted` (#69756e) on `bg` (#f4f5f2) is ~4.4:1 — under AA (already noted in the design-system README). | tokens | 4.5:1 floor |
| 9 | `.brand` eyebrow: 13px accent uppercase tracked .12em. | `globals.css` | no eyebrows |
| 10 | Chart palette is five unrelated hues (green, blue, amber, violet, teal); violet is on the owner's banned list. | `--chart-1..5` | one hue per chart, ≤4 per screen |
| 11 | Body 15px with 44px controls everywhere on desktop: low density for a daily data tool. | tokens | body 14, controls 32–36 on desktop |

## 3. The idea

> On this screen, **what Umwelt currently perceives and how old each perception is** becomes **the ledger itself**:
> ink for every value, one green on the data that just arrived, grey on what is ageing.

"Umwelt" is the world as one organism perceives it. The design makes perception and its freshness the visible
structure, instead of decoration.

- **Face** (one, from the sheet): the brand green moves off the chrome and onto the **main data element** of each
  gadget — today's series, the newest items — and nowhere else. The ground keeps the existing sage tint.
- **Signature element: the freshness strip.** One row under the dashboard title: every active source as a mark on
  a shared 24-hour axis — an ink dot at its last successful collection, a hollow tick where the next one is due,
  red only when overdue. It answers "can I trust what I'm reading?" before any gadget is read. It replaces the
  separate stale-sources block and draws from data the app already has (source schedule + last run).
- **Every gadget states its provenance in words**, at 12px grey in its header: "Open-Meteo · 12 min ago".
  No badges for state that is fine; a pale pill only for state that needs action (stale, setup required, rule hit).

## 4. Tokens

**Decision 1:** apply these values to `globals.css` and `docs/design-system/tokens.json` together.

| Role | Light now | Light proposed | Dark proposed | Note |
|---|---|---|---|---|
| `--bg` ground | #f4f5f2 | #f2f5f2 | #0e110f | sage kept; dark is true neutral, not slate |
| `--surface` | #ffffff | #ffffff | #161a17 | |
| `--raised` (new) | — | #f7f9f7 | #1d221e | hover rows, user chat turns, settings panels |
| `--active` (new) | — | #e8ece8 | #252b27 | nav pill, selected list row |
| `--line` card edge | #dfe4df | #e3e7e3 | #252b27 | |
| `--rule` (new) row rules | — | #eef1ee | #1d221e | |
| `--control` (new) | — | #cfd5d0 | #3a423c | inputs/selects one step stronger |
| `--text` | #17211c | #17211c | #edf3ee | |
| `--muted` | #69756e | #5c6660 (5.4:1 on bg) | #a7b1aa | fixes finding 8 |
| `--primary` (button fill) | = accent green | #17211c near-black | #edf3ee inverted | **Decision 2** |
| `--accent` (data hue) | #176a4a | #176a4a | #77c69c | charts' main series, focus ring, checked controls |
| `--positive` / `--negative` | — | #247b50 / #c4242a | #3fc07a / #e5566f | with sign, never colour alone |
| pill pairs | — | #dcfae5/#04250e, #ffedea/#831c1e, #fffbeb/#92400e | dark tints of the same hue | |
| `--chart-1..5` | 5 hues | accent, text, #8a948e, #b8c0ba, accent dashed | same roles | fixes finding 10 |
| radius card / control / chip | 18·12 / 9 / 6 | 8 / 6 / 4 | same | dialogs and Sheet 12 |
| body / title / section / small | 15 / 27–36 / 20 / 13 | 14 / 24·600 / 16·600 / 12 | same | one weight of 700, for money only |
| control height | 44 | 36 fine pointer, 44 coarse | same | **Decision 3**: README mandates 44 everywhere; propose `@media (pointer: fine)` 36 |

Font stays Inter → system stack (README: "don't introduce a second family"); add `tabular-nums` on every figure.
Note: Inter is named but not shipped, so most machines render Segoe UI. **Decision 4**: self-host Inter via
`next/font` so the face is real (one dependency-free change; the study's top failure was falling back to a default face).

## 5. Shell

```
┌──┬──────────────────────────────────────────────────────────────┐
│◉ │ Morning desk ▾           [⌘K Search…]          🔔  Workspace ▾ ◯ │  48px, on ground, rule only when scrolled
│  ├──────────────────────────────────────────────────────────────┤
│▦ │                                                              │
│💬│   canvas: 24px gutter, full width                            │
│⚙ │                                                              │
│  │                                                              │
├──┴──────────────────────────────────────────────────────────────┤
│ ● API connected   ● Realtime live                    ⓘ          │  footer 28px, grey text, dot carries colour
└─────────────────────────────────────────────────────────────────┘
```

- **Icon rail 56px** on the ground with a 1px `--line` edge: Umwelt mark at top (links to dashboard), Dashboard /
  Chat / Settings as 18px outline icons on a 44px pitch, tooltip + accessible name each. Active = `--active` fill
  square, icon in ink; inactive icons `--muted`. Replaces the 240px sidebar and its collapse toggle (−184px).
  **Decision 5**: the README places the logo in the header; here it sits at the top of the rail (still top-left).
- **Top bar 48px** on the ground, no border until content scrolls under it. Left: the page's scope (dashboard
  switcher on Dashboard, thread title on Chat, "Settings" breadcrumb). Centre: command palette trigger 360×32 with
  the shortcut. Right: notifications, workspace switcher, user menu. Nothing empty.
- **Connection footer 28px**: text statuses with a 6px dot ("API connected", "Realtime live"), not 44px tinted
  circles. Desktop only; on phones the status moves into the user menu with a dot on the avatar when degraded.
- **Phone (≤720px)**: bottom bar of the three destinations with labels (already exists), top bar keeps scope +
  search icon + avatar.

## 6. Screens

### Dashboard — question: "What changed since I last looked, and can I trust it?"

- Title row: h1 = dashboard name at 24/600 (not 30 bold). Second line, 14px grey, the finding computed from data:
  "2 sources overdue · last update 09:14". Right: `Edit layout` (secondary) and `Add gadget` (the one near-black primary).
- Freshness strip (signature, §3) directly under, 56px tall, full width, collapsible.
- Gadget frame:
  - radius 8, 1px `--line`, **no shadow**; header 40px, title 14/600 ink, provenance 12px grey after it.
  - "Ask Umwelt": ghost icon button, ink sparkle (not green); label only on hover/focus width ≥ 1280.
  - Unread: ink count "4 new" in grey text; rule hit / stale / setup: one pale pill, sentence case, ≥11px.
    Severity never uppercase. Revision and scope chips move to the Configure dialog.
  - Reading-queue bar: `--raised` strip, ink text, ghost "Show 3 updates" button.
  - Body: the renderer's main series in `--accent`; comparisons in grey; every other mark ink.
- Edit mode: grid lines dashed `--line` (dashes allowed in grids), selected gadget 2px ink outline (not green ring).
- Empty dashboard: keep the title row and the strip; the first action ("Add gadget") sits where gadgets go, with
  the preset picker inline, not a centred empty panel.

### Chat — question: "What did we conclude, and on what evidence?"

Three regions (inbox pattern): history 280px · transcript · evidence rail 320px.
- History rows two-line 64px: title 14/600 ink, relative time right in grey; line 2 last message 13px grey, one
  line ellipsis. Selected = `--active` fill. No avatars.
- Transcript measure 720px max, centred in its region. Assistant turns are plain text on the surface (no bubble);
  user turns sit on `--raised`, radius 8. Citations are superscript numerals in ink linking to the rail.
- Evidence rail: cited sources as rows — source name, collected time, a one-line excerpt; the hovered citation's
  row takes `--active`. This is where provenance (the idea) is strongest.
- Composer: the one floating element (shadow allowed), 12 radius, model picker and effort as quiet selects inside.
- Phone: one region at a time; rail opens from a "Sources (3)" button in the thread header.

### Settings — question: "Is each source and model set up and healthy?"

- Drop the 18px outer panel. Inner nav becomes a 200px text list on the ground (`--active` pill on current).
- Each section is a row: 360px label column (16/600 title, 14px grey description, plus a summary of current values
  so it's never an empty column) beside a `--raised` panel, radius 8, no border.
- Sources list: table with rules, not cards — name (ident.), provider, status pill only when not OK, last run
  right-aligned ("12 min ago"), next run, one kebab. Provider grid in the wizard: tiles become a searchable list
  with provider logo 16px and a one-line description; selected row `--active` + 2px ink inner edge.
- Destructive actions last, in their own section, outlined `--negative`, AlertDialog naming the object.

### States (all screens)

- Loading: flat `--rule` bars at the real size of the value; no shimmer gradient (fixes finding 7).
- Error inside the block that failed: one sentence with last-good time + "Retry" text button.
- Missing value = grey em dash; real zero printed.

## 7. Delivery plan (biggest effect, least risk first)

| Phase | Scope | Files (main) | Risk |
|---|---|---|---|
| 0 Bugs seen in screenshots | 390px top bar overflowing to the left; gadget header actions clipped; daily brief "Today" twice; "Saved" shown before any edit; developer copy "Collection code is wired" | `workspace-shell.tsx`, `gadget-frame.tsx`, `daily-brief.tsx`, models settings, wizard copy | low |
| 1 Tokens | §4 values, radii, type scale, remove `.brand` eyebrow, flat skeleton, `tokens.json` mirror | `globals.css`, `tokens.json`, `card.tsx`, `button.tsx` | low code risk, whole-app visual change; e2e visual assertions may need updates |
| 2 Accent audit | green off chrome: `text-primary` icons → muted/ink, links ink with underline on hover, tabs ink indicator | ~40 components (grep list) | medium, mechanical |
| 3 Shell | rail + 48px top bar + compact footer | `workspace-shell.tsx`, `connection-footer.tsx`, CSS | medium: shell tests, nav e2e |
| 4 Gadget frame + dashboard title row | §6 dashboard | `gadget-frame.tsx`, `dashboard-page.tsx` | medium |
| 5 Freshness strip | new component from existing source schedule/last-run data | new `freshness-strip.tsx`, sources public API read | new feature, needs data contract check |
| 6 Chat | three-region layout, rail rows, turn styling | `app/chat/page.tsx`, `chat-*` | medium |
| 7 Settings | two-column sections, sources table | `settings-nav.tsx`, sources pages | medium |

Each phase: GitNexus impact on edited symbols, `next build`, vitest for touched components, taste-skill
`inspect.mjs --app` at 1440/768/390 before and after, screenshots in the PR.

## 8. Decisions needed

**Owner decision 2026-10-30: all six approved (1A 2A 3A 4A 5A 6A).**

1. Adopt the §4 token table (changes the look of every screen).
2. Primary buttons near-black instead of brand green (green stays on data, focus and checked state).
3. 36px controls on fine pointers (44px kept on touch) — amends README "every control ≥44px".
4. Self-host Inter via `next/font`.
5. Icon rail with the logo at its top instead of a labelled 240px sidebar + header logo.
6. Build the freshness strip (new feature) in phase 5.
