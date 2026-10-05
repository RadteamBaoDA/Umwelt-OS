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
- Canvas is `bg`; content lives on `surface` panels; text is `text`, supporting copy `muted`, all borders `line`.
- `accent` is the only brand hue. Spend it on: the one primary action per section, links and text buttons, checked/selected controls, focus rings. Never as a decorative card fill.
- Text on an `accent` fill uses `on-accent` (white in light, `#102117` in dark — the dark accent is light).
- `danger` only for errors and destructive actions, always with words.
- shadcn primitives read the Tailwind aliases (`color-background`, `color-border`, `color-ring`, …). Note the trap: Tailwind's `bg-accent` is `color-accent` → `bg` (a quiet hover fill), not the brand green.
- Feature code never passes literal colors; add a semantic token instead. Planned but not yet in code: `chart-1…5` series colors and `market-positive/negative/neutral` (always with sign + text, never color alone).
- `muted` on `bg` in light is ~4.4:1, just under AA. Prefer `muted` on `surface`.

### Type
- One family, `sans` (Inter → system UI stack). Inter is named but not shipped; most machines render the system face. Don't introduce a second family.
- Page: `eyebrow` (accent, uppercase) → `page-title` → body in `muted`. Sub-panels use `section-title`. Overlays use `dialog-title`.
- Body is 15px / 1.6. Labels are `label` (13px / 650). Status and timestamps `meta` or `caption`, in `muted`.
- The wordmark is the text "Umwelt-OS" in `brand-name` (800, −0.03em). There is no logo file.

### Space, size, layout
- Scale `space-1…space-6` (4/8/12/16/24/32px). Every control is at least `control-height` (44px).
- Shell fills 100% width — no centred max-width. Desktop: `nav-width` (180px) column + `minmax(0, 1fr)` main, `space-5` gap. Under 720px the nav becomes a horizontal scroller and two-column detail grids stack.
- Respect safe-area insets on shell padding and dialogs; content uses `min-height: 100dvh`, never fixed height with hidden overflow.
- Main navigation is exactly **Dashboard / Chat / Settings**. Settings has three groups: Data sources, AI & Ommi Router, Dashboard & Gadget. Account, theme and language live only in the user menu.

### Shape and depth
- Radii nest outward: `radius-sm` items inside `radius-md` controls inside `radius-lg` sub-panels inside `radius-panel` panels.
- Separate with 1px `line` borders, not shadows. Shadows exist only on raised layers: `shadow-popover` (menus, select), `shadow-dialog` (modals), `shadow-panel` (auth card, nearly invisible).
- Empty states: dashed `line` border, `radius-lg`, `muted` text.

### States
- Focus: 3px outline of `accent` at 40% opacity, 2px offset, on every interactive element (shadcn primitives use a 2px `color-ring` ring). Never remove it.
- Disabled: opacity .55–.65 and `not-allowed` cursor; a submitting button shows `progress`.
- Selected: nav item becomes a `surface` pill with a `line` border; option cards get an `accent` border plus a 9% `accent` tint.
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
