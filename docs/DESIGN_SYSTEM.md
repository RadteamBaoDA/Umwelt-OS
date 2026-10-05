# Umwelt-OS UI design system

Status: UI technology and experience decisions selected by the owner. This document defines implementation requirements; it does not claim that dependencies, migration, theme persistence or translation are already implemented.

## 1. Foundation and ownership

- Use **shadcn/ui** for application UI components and their composition, **Tailwind CSS** for layout, and the shadcn semantic CSS-variable token system. Use one Radix-based shadcn component family consistently. Do not mix Radix and Base UI recipes accidentally.
- shadcn components are source owned by this repository, not a remote runtime widget or a single `shadcn-ui` component package. Generate/adopt only needed components with the official CLI, review the generated changes, and record the selected version/configuration in `components.json`, dependency lockfile and OSS inventory during implementation.
- Use **Recharts** through shadcn Chart components for stock, coin and other numeric charts. Maps continue to use globe.gl/deck.gl; chart choice does not replace the map engines.
- Use existing React Hook Form + Zod for forms. For the Next.js implementation use **next-themes** for light/dark/system and **next-intl** for typed message catalogs and App Router integration, together with native `Intl` formatting.
- Use Lucide icons through the shadcn conventions, with visible labels or translated accessible names. Keep the existing font family unless a separate typography decision is made.
- Add no Material UI, Ant Design, Chakra or parallel button/dialog/form design system. The MUI drawer link supplied earlier describes behavior; implement that behavior with **shadcn Sheet**.
- Initially own app components in `apps/web/src/components/ui`, application compositions beside their feature, and tokens in `apps/web/src/app/globals.css`. If a shared UI package already owns a component when implementation starts, extend that owner instead of creating a duplicate. Do not create an unused package for future consumers.

## 2. Product navigation and component mapping

Main navigation contains exactly **Dashboard / Chat / Settings**. Settings has exactly three local groups: **Data sources**, **AI & Ommi Router**, **Dashboard & Gadget**. Account, theme and language belong only in the user menu. Reading a story, entity or feed can open a detail view without adding a main navigation entry. Canonical approved requirements are specification section 165.

| Product element | shadcn component/composition | Required behavior |
| --- | --- | --- |
| App navigation | Sidebar + Button/NavigationMenu as appropriate | Three main destinations, current-route state, mobile collapse |
| App header | Logo/home link + Button + DropdownMenu | Logo left; login/user icon right; account actions include User settings and sign out |
| Login screen | Card, Label, Input, Button, Alert | Reuse existing authentication; translated pending/error/session-expired states; no application data before authentication |
| Connection footer | Badge, status text, Button | Client-to-server/API and realtime states separately; accessible live status; retry when appropriate; never infer source freshness from transport health |
| Gadget container | Card + CardHeader/CardContent | Header, source/freshness, empty/error state; geometry owned by dashboard grid |
| Dashboard editing | Button, DropdownMenu, Tooltip | View hides grid/handles; Edit creates a draft; Save/Cancel explicit |
| Gadget management | Card, Tabs, Select/Combobox, Input, Label, form components | Data template + compatible renderer; only configured/permitted sources |
| General user preferences | **Dialog**, RadioGroup, Select, Button | Theme/language preview, Save, Cancel, translated labels/errors |
| Quick chat | **Sheet**, ScrollArea, Textarea, Button | Right side, large width, New chat/messages/composer/close only |
| Full Chat page | Sidebar/list, ScrollArea, Textarea, Sheet on mobile | History, open thread, search/manage history, context and advanced controls |
| Data tables | Table; shadcn Data Table recipe when needed | Semantic header/cells; sort/filter/pagination only when useful |
| Financial charts | ChartContainer, ChartTooltip, ChartLegend + Recharts | Explicit sizing, locale-aware axes/tooltips, timestamps/units, accessible alternative |
| Destructive actions | AlertDialog | Explicit consequence and affected items |
| Feedback | Skeleton, Alert, Badge, Sonner | Save/loading/error/stale state; notifications do not replace inline errors |

Use shadcn primitives rather than hand-built substitutes. Business components such as GadgetCard, FinancialChart and ChatComposer compose these primitives. Dashboard drag/resize behavior belongs to its grid layer; shadcn does not itself implement a draggable dashboard.

### Quick chat Sheet

- Use Sheet on the right. Desktop proposal: about 65vw, capped at 56rem; mobile: full width. The content height follows the dynamic viewport and safe areas.
- Only New chat, user/assistant messages, composer with send/stop, and close are visible. An accessible Sheet title/description may be visually hidden.
- History, thread selector, model/context controls, attachment management and web-search controls belong on the full Chat page. Main Chat navigation opens the current thread without clearing its draft or restarting generation.
- Body scrolls independently; composer stays at the bottom. Preserve focus trapping, Escape/close, accessible naming and return focus from Sheet primitives. Do not disable these behaviors to imitate a screenshot.
- Port the requested AnythingLLM chat source into this repository while adapting it to these components and the existing Umwelt-OS thread/permissions/API contracts. AI calls use server-side OpenAI SDK via **Ommi Router**. There is no local-AI/Ollama requirement.

## 3. Semantic tokens and visual rules

Use CSS-variable theming from shadcn. Maintain values centrally for light (`:root`) and dark (`.dark` on `html`), with Tailwind v4 mappings. Do not pass literal colors through feature components.

| Token family | Use |
| --- | --- |
| background / foreground | App canvas and primary text |
| card / card-foreground | Gadget and content surfaces |
| popover / popover-foreground | Menus, tooltip/dialog surfaces |
| primary / primary-foreground | Main actions and selected controls |
| secondary, muted, accent with foreground pairs | Secondary surfaces/text and interactive states |
| destructive | Destructive action/error semantics |
| border / input / ring | Separation, editable controls and keyboard focus |
| sidebar tokens | Navigation surfaces, selection and borders |
| chart-1 … chart-5 | Stable series identities and legend swatches |
| market-positive / market-negative / market-neutral | Financial direction, always with sign/text/shape |
| radius | Common component radius; derive smaller/larger variants centrally |

Start with shadcn neutral surfaces and the established component sizing. Use compact editorial density, restrained borders, one main action per section, and a consistent spacing scale (4/8/12/16/24/32px). Chart colors remain series encodings, not decoration for every card. Use tabular numerals for prices/quantities. Supporting text must remain readable, touch controls about 44px, and focus rings visible.

Avoid broad global selectors such as `.button`, `button { ... }` or `.input` overriding generated component behavior. Typography/layout styles must not break portal content. Dialog, Sheet, Tooltip and Select rendered under `body` must inherit the active root tokens and language.

## 4. Theme contract

Persisted theme preference: **`light | dark | system`**, default `system`. Effective appearance is derived, not stored as a replacement for `system`.

- Follow the shadcn Next.js dark-mode recipe with next-themes and `attribute="class"`; the root `.dark` selector must be the single effective theme authority.
- Follow OS changes only while preference is `system`. Manually selected light/dark remains stable when the OS changes.
- Initialize theme before visible paint, handle server/client agreement, and do not show incorrect mounted-only icons. Use root hydration suppression only where the documented theme strategy requires it, not as a general way to hide hydration bugs.
- Apply `color-scheme` so native controls match. Chart axes/grid/tooltip, maps, generated component portals and chat source content must remain readable in both themes.
- Reuse current preference persistence if it exists. Current checked-in CSS uses `body[data-theme='dark']` and legacy token names; the implementation must migrate callers to one authority. Temporary aliases can bridge existing screens, but do not keep two independent theme states.

## 5. Internationalization contract

The owner requested two choices. Keep these app preference identifiers and normalize at the library boundary:

| App preference ID | Display name | Messages / `Intl` / HTML `lang` |
| --- | --- | --- |
| `en-us` | English (US) | `en-US` |
| `vi-vi` | Tiếng Việt | `vi-VN` |

`vi-vi` is an application alias, not the locale to pass through unchanged for Vietnamese/Vietnam formatting. Accept canonical/legacy input aliases at a single boundary if needed; emit the defined preference contract consistently. Do not invent a third language. Default to saved owner preference, then a supported browser language, otherwise `en-us`.

- Use next-intl catalogs with stable message keys, with English as missing-message fallback. Server-rendered and client-rendered UI must use the same effective locale. Set HTML `lang` accordingly.
- Translate navigation, buttons, labels, placeholders, validation/API error messages, dialogs, tooltips, chart legends, empty/loading/stale states, toast text and accessible names. Do not build sentences by concatenating translated fragments; use interpolation/plural messages.
- Translate UI chrome, not external news, proper names, tickers, URLs, source IDs or user messages. Source-content translation is a separate explicitly selected feature, with original content retained.
- Use `Intl.DateTimeFormat`, `Intl.NumberFormat` and `Intl.RelativeTimeFormat` with canonical locale. Locale changes formatting, not the underlying quote currency, numeric value or configured timezone. Example: formatting a USD quote in Vietnamese does not convert it to VND.
- Pass an explicit timezone for server/client timestamp rendering; preserve UTC instants and source timestamps. Display exchange/session timezone when relevant.
- Avoid arbitrary inline ternaries spread across components. Type the supported preference IDs and keep alias normalization in one place. No localized route duplication is required solely to switch these two UI languages.

## 6. User settings dialog

Entry point: user/account menu in the app header; the login screen may expose appearance/language before authentication. Do not duplicate account/appearance in Settings or add a fourth main navigation item.

```text
User settings                                      [Close]
Appearance      [ Light ] [ Dark ] [ System ]
Language        [ English (US) / Tiếng Việt      v ]

Changes are previewed until saved.
                                      [Cancel] [Save]
```

- Use shadcn Dialog with title/description, RadioGroup for appearance and Select for language. Mobile remains a responsive Dialog, with safe-area-aware margins/scrolling and large controls.
- Opening snapshots committed preferences. Theme and language changes preview throughout the visible UI, including the dialog itself, without losing form input, layout drafts or the active chat.
- Save persists both settings atomically to the existing owner-preferences mechanism (extend it if needed), updates the initial-render cookie/client cache, then closes only on success. Keep draft/preview and a translated retryable error if the save fails.
- Cancel, Escape and closing without Save restore the committed pair. While a Save is in flight, prevent duplicate submissions and ambiguous dismissal; preserve dialog focus behavior.
- Layout Save/Cancel and preference Save/Cancel are separate operations. Changing theme/language must not commit a dashboard layout draft.
- On reload, server preference is authoritative for authenticated sessions. Any browser cache is a bootstrap aid with a defined reconciliation path, not an unrelated competing store. Preferences may be non-secret, but APIs still authenticate and validate allowed values.

## 7. Financial charts with Recharts

- Default price presentation: LineChart/AreaChart; volume: BarChart; normalized comparison: clearly labeled percentage/index series. Use the shadcn Chart wrapper and a container with measurable width and a defined minimum height/aspect ratio; avoid nested responsive wrappers that yield zero size.
- Data should carry symbol, venue/provider, timestamp, timezone/session context, quote currency/unit, precision and freshness/delay status. Missing points remain gaps; never replace a missing quote with zero or interpolate across exchange closures without a declared rule.
- Tooltips show time, value, unit/currency and available series. Axes/legend/tooltips share locale formatters; short axis ticks must not obscure full values in the tooltip. A price delta needs both its sign and period.
- Preserve provider precision and formatting rules for very small coin prices, large quantities and decimal prices. Avoid floating-point calculations for monetary business logic; a chart's numeric display representation does not become the accounting value.
- Keep zoom/time range stable as live data arrives; coalesce visual updates and provide stale/offline states. Do not redraw the entire dashboard for each price tick.
- Expose an accessible text summary/table, keyboard-accessible controls, Recharts accessibility support appropriate to the installed version, and reduced-motion behavior. Colors alone must not encode gain/loss or distinguish series.
- Recharts does not supply an assumed `CandlestickChart` component. If OHLC/candlesticks are later explicitly required, build one narrowly scoped shared shape/composition with documented OHLC invariants; do not silently substitute invented components or a second chart library.

## 8. Viewport, layout and overlays

The application owns 100% width with no centered max-width around the entire shell. `html/body/app root` fill at least the viewport; dashboard content can extend vertically and scroll the document. Use `min-height: 100dvh` with fallback where needed, not a fixed height plus hidden overflow that clips the dashboard. Main columns use `minmax(0, 1fr)`/`min-width: 0` to prevent chart and table overflow.

Every dashboard content block is a gadget, including the globe/map, highlights, watch rules and personal context. They share the same grid, add/remove, move, resize and Save/Cancel lifecycle. App header/footer, navigation and layout controls remain shell elements. Map filters belong inside the map gadget.

Dashboard grid stays at most 20 square-unit columns; grid and handles appear only in Edit. Users drag a gadget title to move it and drag its corner to resize directly on the dashboard, with live size feedback, grid snapping and collision handling. Do not require a size dialog as the primary resize interaction. Support keyboard equivalents and canceling an in-progress pointer gesture. Desktop positions must not be overwritten by mobile reflow. On mobile, readable full-width gadgets and navigation collapse take precedence over shrinking desktop text. Dense tables may scroll inside their own wrapper; the overall page must not overflow horizontally.

Use built-in shadcn portal/overlay stacking. Settings Dialog and quick-chat Sheet must not create competing focus traps; open one modal surface at a time and restore the prior focus/context on close.

## 9. Implementation and delivery boundaries

Current source inspection: Next.js/React/TypeScript + Tailwind v4, React Hook Form, Zod are present; package manifest does not yet include shadcn-generated primitives, Recharts, next-themes or next-intl. `RootLayout` currently has `lang="en"`; existing CSS has fixed shell max-width and `body[data-theme]`. These are migration targets, not already completed work.

Implementation order: adopt minimum shadcn components/tokens → theme + i18n/preferences → shell and settings Dialog → gadget compositions → quick/full chat adaptation → Recharts financial gadgets. Inspect existing preference/API owners before creating alternatives. Keep existing data/auth/permission contracts intact.

Update `OSS_USED.md` and README at delivery with the actual versions, source/port locations, licenses and setup, including shadcn/Radix/Recharts/next-themes/next-intl and the relevant transitive inventory. Do not list a design-only dependency as already installed in production.

During the production code stage, follow repository policy: code + build only; no tests, lint or standalone typecheck. When the deferred test stage is authorized, cover theme boot/system changes, both locales and portal translations, Save/Cancel/failure behavior, reload persistence, mobile viewport/keyboard, chart units/gaps/freshness and chat continuity. Report build evidence separately from runtime behavior.

## Official references

- https://github.com/shadcn-ui/ui
- https://ui.shadcn.com/docs
- https://ui.shadcn.com/docs/theming
- https://ui.shadcn.com/docs/dark-mode/next
- https://ui.shadcn.com/docs/components/radix/dialog
- https://ui.shadcn.com/docs/components/radix/sheet
- https://ui.shadcn.com/docs/components/radix/chart
- https://recharts.github.io/
- https://recharts.github.io/en-US/api/ResponsiveContainer/
- https://next-intl.dev/docs/getting-started/app-router

The shadcn theming/dark-mode/Sheet/Chart and Recharts ResponsiveContainer pages were consulted for this design. Confirm generated imports and APIs against the versions actually installed when implementing.
