A private personal-intelligence workspace: calm, dense, honest about what it knows. One green, quiet neutrals, hairline borders, and copy that says exactly what state the system is in.

## Content fundamentals

- **Voice: plain, precise, unhurried.** Say what happened and what to do next. Never cheerlead, never apologise twice. Real copy:
  - "Reload after checking the local API."
  - "Connection status does not indicate source freshness."
  - "Could not refresh the document list. Your update notice is still available; try again."
- **Name the exact state.** Distinguish *Connecting*, *Reconnecting*, *Client offline*, *Server unreachable*, *Session expired* — don't collapse them into "Error". Transport health never implies data freshness.
- **Sentence case** for every label, button, title and menu item ("Sign in again", "Apply filters", "Load more events"). Uppercase appears only in the `eyebrow` style.
- **Second person, imperative.** "Choose both dates and make the end date later than the start date." No "we", no "oops", no exclamation marks, no emoji.
- **Two languages, one catalog.** Every visible and accessible string comes from the `en-us` / `vi-vi` message catalogs (normalised to `en-US` / `vi-VN`). Use ICU plurals and interpolation (`{count, plural, one {# new document update} other {…}}`); never concatenate translated fragments. Vietnamese strings run ~20–30% longer — let buttons and nav wrap, never truncate.
- **Translate chrome, not content.** Source titles, entity names, tickers, URLs and user messages stay as written.
- **Numbers and time via `Intl`.** Pass an explicit timezone; say which one ("Events shown in {timezone}"). Locale changes formatting, never currency or value. Use tabular numerals for prices and quantities.

## Visual foundations

### Color
- Canvas is `bg`; content lives on `surface` panels, with `raised` for hover rows and user turns and `active` for the selected nav item or list row. Text is `text`, supporting copy `muted`; card edges are `line`, row rules `rule`, input borders `control`.
- `primary` is near-black ink (inverted in dark) and fills the one primary button per section. `accent` is the data green: a chart's main series, the newest data, focus rings and checked controls (`text-data` / `bg-data`). Never as chrome or a decorative card fill.
- Text on a `primary` fill uses `primary-foreground` (white in light, `#0e110f` in dark).
- `positive` / `negative` and the `pill-*` pairs mark state, always with a sign or word.
- `danger` only for errors and destructive actions, always with words.
- shadcn primitives read the Tailwind aliases (`color-background`, `color-border`, `color-ring`, …). Note the trap: Tailwind's `bg-accent` is `color-accent` → `bg` (a quiet hover fill), not the data green (use `text-data`).
- Feature code never passes literal colors; add a semantic token instead. `chart-1…5` are accent, ink, two greys and a dashed accent: one hue per chart.
- `muted` holds at least 4.5:1 on `bg` and `surface` in both themes (5.4:1 on `bg` in light).

### Type
- One family, `sans` (Inter → system UI stack). Inter is self-hosted through `next/font/google` (downloaded at build, served from this origin). Don't introduce a second family. Figures use tabular numerals (`.tabular`, and every `table`).
- Page: `eyebrow` (accent, uppercase) → `page-title` → body in `muted`. Sub-panels use `section-title`. Overlays use `dialog-title`.
- Body is 14px / 1.5; page titles 24/600. Labels are `label` (13px / 650). Status and timestamps `meta` or `caption`, in `muted`.
- The wordmark is the text "Umwelt-OS" in `brand-name` (800, −0.03em). There is no logo file.

### Space, size, layout
- Scale `space-1…space-6` (4/8/12/16/24/32px). Controls are `control-height` 36px on fine pointers and 44px on coarse pointers (`pointer-coarse:` / `@media (pointer: coarse)`); bottom-nav tabs and theme options stay 44px or taller.
- Shell fills 100% width — no centred max-width. Desktop: a `nav-width` (56px) icon rail (logo at its top, tooltip labels) + a 48px top bar + `minmax(0, 1fr)` main. At 720px and below the rail becomes a labelled bottom bar and two-column detail grids stack.
- Respect safe-area insets on shell padding and dialogs; content uses `min-height: 100dvh`, never fixed height with hidden overflow.
- Main navigation is exactly **Dashboard / Chat / Settings**. Settings owns data sources, models and system prompts; dashboard layouts, gadget templates, topics and highlight rules are managed from Dashboard. Account, theme and language live only in the user menu.

### Shape and depth
- Radii are 8 (cards, gadgets, panels), 6 (buttons, inputs) and 4 (chips, menu items); dialogs and Sheet use 12.
- Separate with 1px `line` borders; no resting shadows. Shadows exist only on floating layers: `shadow-popover` (menus, select) and `shadow-dialog` (modals).
- Empty states: dashed `line` border, `radius-lg`, `muted` text.

### States
- Focus: 3px outline of `accent` at 40% opacity, 2px offset, on every interactive element (shadcn primitives use a 2px `color-ring` ring). Never remove it.
- Disabled: opacity .55–.65 and `not-allowed` cursor; a submitting button shows `progress`.
- Selected: nav item takes the `active` fill with ink icon; option cards get an `accent` border plus a 9% `accent` tint.
- Loading: a shimmering skeleton between `surface` and `line`, `radius-panel`. All animation is cut under `prefers-reduced-motion`.

### Themes
- `light` and `dark`, plus a `system` preference (default). The root `.dark` class is the single authority (next-themes `attribute="class"`); set `color-scheme` so native controls match.
- Preview changes live in the User settings dialog; Save persists theme and language together; Cancel restores both.

## Components

All UI composes the repository's own shadcn/ui (new-york, Radix) primitives in `apps/web/src/components/ui`. No MUI, Ant, Chakra or hand-rolled parallel buttons/dialogs. Map product needs to primitives:

| Need | Use |
| --- | --- |
| User preferences | Dialog + RadioGroup + Select |
| Quick chat | Sheet, right side, ~65vw capped at 56rem, full width on mobile |
| Destructive confirm | AlertDialog, naming the consequence and affected items |
| Account actions | DropdownMenu from the header user icon |
| Settings sub-views | Tabs |

Previews here are static renditions of the source CSS — the React library is not bundled into this system.

## Iconography

- Lucide (`lucide-react`) only, via shadcn conventions: 16px in menus/buttons (`size-4`), 14px for check indicators, 20px for sheet close.
- Icons inherit `currentColor`; menu icons default to `muted`.
- An icon-only control always has a translated accessible name (e.g. close buttons carry an `sr-only` "Close").
- No emoji in UI.
