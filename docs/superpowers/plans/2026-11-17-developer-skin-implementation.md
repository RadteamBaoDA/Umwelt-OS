# Developer (terminal) skin — implementation plan

Approved design: `docs/design-system/demo/developer-theme.html` (sources in `docs/design-system/demo/src/`:
`base.html`, `parts/10-dashboard.html`, `20-chat-knowledge.html`, `30-settings-public.html`, `40-components.html`,
`50-motion.html`). Published copy: https://claude.ai/artifact/QBvVoZdi7tsc2THAD1ndXf

Owner constraint: **UI/UX only.** No change to API calls, data fetching, query keys, state semantics, routing,
permissions, backend. All existing vitest + Playwright specs must keep passing: preserve accessible names, roles,
`data-testid`, ids, `aria-*`, visible catalog text.

## Decisions (owner defaults)

1. Skin is per device (localStorage `umwelt-skin`, `data-skin="terminal"` on `<html>` via pre-paint script), like density.
2. Skin picker applies immediately (like `DensityToggle`); outside the preferences draft/dirty/save flow; Cancel does not revert it.
3. Terminal ornaments are CSS-only pseudo-elements under `html[data-skin='terminal']`, with alt text `content: '[' / ''`.
4. Both skins: statusline footer (new, 24px, **no `role="status"`, no live region, no "Realtime: Connected" text** —
   `tests/e2e/realtime-status.spec.ts:19-20`), greeting under h1, "Updated…" summary portalled into the footer,
   compact live-map overview (globe flexes, events list ≥5 rows, legend counts), gadget data-refresh reveal
   (terminal = typing clip-path, default = fade; none under reduced motion). Statusline mode segment terminal-only.
5. Terminal forces the dark palette; no `@custom-variant dark` change.
6. Deferred: statusline source/model counts, fetch spinner, number roll, ASCII goal bars, login ASCII banner, `owner@` crumb.
7. Dashboard fit-to-viewport already exists (`dashboard-page.tsx:655`, `globals.css:608`) — nothing to do.

Token specificity: accent rules are `html.dark[data-accent=x]`, so the terminal block uses
`html[data-skin='terminal'], html[data-skin='terminal'].dark, html[data-skin='terminal'][data-accent] { … }`
placed after the accent blocks. Parity test (`modules/chat/generative-ui/__tests__/design-system-parity.test.ts`) reads only
the first `:root {` and `html.dark {` blocks — do not add new ones above l.113; in `tokens.json` put skin values under a
new top-level `skins` key. `rounded-*` compile to literal px, so terminal radius uses
`html[data-skin='terminal'] *:not(.rounded-full):not([data-slot='radio-group-item']) { border-radius: 0 !important; }`.

## Slices (no file overlap)

### S1 Skin plumbing
New: `apps/web/src/core/skin.ts`, `core/skin.test.ts`, `modules/account/skin-picker.tsx`, `modules/account/skin-picker.test.tsx`.
Edit: `app/layout.tsx`, `modules/account/preferences-dialog.tsx` (one import + `<SkinPicker />` before `<AccentPicker>` ~l.259),
`locales/{en-us,vi-vi}/preferences.json`, `modules/dashboard/gadgets/world-map.tsx`, `modules/landing/source-canvas.tsx`,
`modules/chat/generative-ui/sandbox/renderer.tsx`.
- `skin.ts` mirrors `core/density.ts`: `SKINS=['default','terminal'] as const`, `SkinId`, `SKIN_STORAGE_KEY='umwelt-skin'`,
  `SKIN_INIT_SCRIPT` (sets attribute only when stored value is `terminal`, try/catch), `readSkin()`, `applySkin(id)`
  (terminal sets attr+storage; default removes both; storage errors swallowed).
- `layout.tsx`: script with nonce after density script; `JetBrains_Mono({subsets:['latin','vietnamese'], variable:'--font-mono-skin', display:'swap', preload:false})` from `next/font/google`, variable class on `<html>`.
- `SkinPicker`: RadioGroup like `ThemeField`; ids `preference-skin-*`; label `skin.label`, help `skin.help`, note `skin.terminalNote` when terminal.
- i18n flat keys: `skin.label` Skin / Kiểu giao diện; `skin.help` Applies immediately to this device only. / Áp dụng ngay, chỉ trên thiết bị này.;
  `skin.default` Default / Mặc định; `skin.terminal` Terminal / Terminal; `skin.terminalNote` Terminal uses its own dark palette; theme and accent apply to the Default skin. / Terminal dùng bảng màu tối riêng; chủ đề và màu nhấn chỉ áp dụng cho kiểu Mặc định.
- Theme observers: `world-map.tsx` `isDarkTheme()` true also for `data-skin==='terminal'`, `attributeFilter:['class','data-skin']`; `source-canvas.tsx:112` add `'data-skin'`; `renderer.tsx` `updateFromDom` treat terminal as dark + filter.
- Tests: skin.test.ts (like accent.test.ts), skin-picker.test.tsx.

### S2 Styling foundation
Edit only: `apps/web/src/app/globals.css`, `docs/design-system/tokens.json`, `docs/DESIGN_SYSTEM.md`.
One block after the last `.dark .accent-swatch-plum` rule (~l.218), before `@layer base`, header `/* ===== Terminal skin ===== */`:
- tokens from `base.html:15-27`: color-scheme dark; --bg #0d1117 --surface #0f141a --raised #161b22 --active #1f2630 --text #c9d1d9
  --muted #8b949e --line #232a33 --rule #161b22 --control #30363d --primary #3fb950 --primary-foreground #0d1117 --accent #3fb950
  --danger #f85149 --positive #3fb950 --negative #f85149 --status-success #3fb950 --status-warning #d29922, pill bgs
  (#12261a / #2d1416 / #2b2111), --destructive-foreground #0d1117, --chart-1..5 from demo, plus --strong #e6edf3 --info #58a6ff
  --warning #d29922 (only here; consumers use `var(--info, var(--accent))`), --scrim rgb(0 0 0/.6), --shadow-popover none.
  Check every token name exists in `:root` first; map demo roles onto real names.
- body mono font stack `var(--font-mono-skin), 'JetBrains Mono', 'Cascadia Code', ui-monospace, Consolas, monospace`; radius rule; thin scrollbars; focus outline 2px accent.
- primitive ornaments (`base.html:110-256`, `40-components.html`): button `[ ]` (not ghost/link/icon sizes — check `button.tsx` data attrs), badge flat + `●` (destructive = red outline like destructive button), tabs active `*`, `.workspace-main h1::before '# '`, table th uppercase 11px, dialog flat 1px border, switch optional.
- tokens.json `skins.terminal` (same hex), DESIGN_SYSTEM.md "Skins" section.
- Test: design-system-parity test green.

### S3 Shell
New: `core/app-shell/statusline.tsx`, `statusline.test.tsx`, `shell-skin.css` (imported by statusline.tsx).
Edit: `core/app-shell/workspace-shell.tsx` (~5 lines net), `locales/{en-us,vi-vi}/shell.json`.
- `Statusline`: `<footer className="statusline" aria-label={t('statusline.label')}>`; mode span aria-hidden (INSERT on /chat else NORMAL, terminal only); route path (`hide-sm`); `<span id="statusline-info" className="statusline-info" />` portal slot; aria-hidden transport dot + `api`/`realtime` words (reuse `statusColor` from `connection-footer.tsx`); locale · timezone + `StatuslineClock` (own 1s interval).
- `ShellCrumb` aria-hidden `umwelt:~{pathname} $`, terminal only, first child of topbar header.
- CSS both skins: statusline 24px, 12px, raised bg, top border line, flex, nowrap, `flex:none`; `.statusline-info:empty{display:none}`; mode + crumb hidden unless terminal; `.hide-sm` hidden ≤720px. Terminal: mode chip primary bg, crumb, rail active inset accent bar, settings nav `//` groups and `> ` current (`base.html:80-108,234-248`).
- i18n `statusline.label` Status line / Dòng trạng thái; `statusline.modeNormal` NORMAL; `statusline.modeInsert` INSERT.
- Tests: statusline.test.tsx (no role=status; has #statusline-info; mode follows route) + existing shell tests.

### S4 Dashboard header, greeting, live-map overview
New: `modules/dashboard/dashboard-skin.css` (imported by dashboard-page.tsx).
Edit: `dashboard-header.tsx`, `dashboard-page.tsx`, `gadgets/live-map-gadget.tsx`, dashboard locales only if needed.
- Header: `greeting?: ReactNode` rendered under h1; summary `<p>` replaced by `StatuslineSummary` portal into `#statusline-info`, fallback inline `<p>` when slot missing.
- `ConnectedHeader` passes `<TodayHeaderGate …/>` as greeting on overview group; remove it from `DashboardGroupContent` (keep FreshnessStrip).
- `LiveMapContent` overview: map box → `min-h-24 flex-1 basis-0`; events h3 + list wrapper `flex min-h-[10.5rem] shrink-0 flex-col`; h3 keeps exact text, appends non-interactive legend counts by tag (token colours).
- `LiveEventList` rows (not inDialog): one-line grid `auto minmax(0,1fr) auto`, time / title Button (truncate) / source; keep names, `<time dateTime>`, fallbacks, show-more.
- Tests: dashboard-page (greeting, portal vs inline), live-map legend counts.

### S5 Gadget frame
New: `modules/dashboard/use-gadget-reveal.ts`, `use-gadget-reveal.test.ts`, `gadget-skin.css` (imported by gadget-frame.tsx).
Edit: `gadget-frame.tsx`.
- `useGadgetFooter` also returns `collectedAt`; `useGadgetReveal(ref, key)` toggles `data-reveal` on the body (reflow restart, 1.5s clear, no-op under reduced motion, guard matchMedia); no remount.
- header `gadget-header`, h3 `gadget-title` classes. Terminal: `── title ──` pseudo, raised header. Reveal: terminal `tw-type` clip-path steps(24) 360ms, default `tw-fade` 260ms, stagger nth-child 1..14 × 45ms; reduced-motion off.
- Tests: use-gadget-reveal.test.ts + existing frame/grid tests.

### S6 Chat, palette, public (CSS only)
New: `modules/chat/chat-skin.css` (import in chat-transcript.tsx), `core/command-palette-skin.css` (import in command-palette.tsx), optional public-skin.css.
Terminal: user message `$ ` prefix no bubble; tool steps `→`; composer flat with accent focus; palette groups `// `, selected inset accent bar; login/setup card as terminal window. No markup/role changes.

## Rules for every implementer
- GitNexus `impact` upstream before editing any symbol (index is from another branch; UNKNOWN/not-found ⇒ confirm callers by text search and report).
- JSDoc on every new/changed named function/component/hook.
- Semantic tokens only; hex only in globals.css token block and tokens.json.
- New strings in both en-us and vi-vi.
- Gates: functions ≤100 lines, complexity ≤15, Prettier, file-size baseline, `npm run typecheck`, eslint on changed files, targeted vitest.
- Do not commit; the coordinator commits after review. Never push. No Docker/dev server.
