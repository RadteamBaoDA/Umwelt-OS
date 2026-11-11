---
name: umwelt-os-ui-system
description: Design, implement, or review Umwelt-OS frontend UI using its shadcn/ui design system, Recharts financial charts, theme preferences and English/Vietnamese localization. Use for application components, dashboard gadgets, chat surfaces and User settings; not for backend-only changes.
---

# Umwelt-OS UI System

Read [the design system](../../../docs/DESIGN_SYSTEM.md) before UI work. It owns component choices, token/theme/locale contracts and preference behavior. Then read [the design-system brand book](../../../docs/design-system/README.md) and use its [tokens](../../../docs/design-system/tokens.json) and per-component guidelines (`docs/design-system/components/<Name>/README.md`) for exact colors, type styles, spacing, radii, shadows, states and copy voice. Explicit user corrections govern older plans.

## Locate before building

- Read repository guidance and current worktree status. Inspect the current component owner, generated shadcn primitives, `components.json`, tokens, provider setup, preference persistence and lockfile for the affected feature.
- Reuse the responsible component or helper. Add only necessary shadcn components through the official registry/CLI when implementing; review generated diffs. Do not regenerate the whole UI or replace unrelated work.
- Preserve the selected Radix-based shadcn family and Tailwind v4 conventions. Components generated for another family/version can have different imports and composition contracts; consult the official page linked in the design system when needed.

## Non-negotiable UI contracts

- Values come from the design-system tokens: `bg`, `surface`, `text`, `muted`, `line`, `accent`, `on-accent`, `danger` (or their Tailwind aliases), radii `radius-sm/md/lg/card/dialog/panel`, spacing `space-1…space-6`, 44px `control-height`. `apps/web/src/app/globals.css` is the runtime source; keep `docs/design-system/tokens.json` in sync in the same change, and note that `muted` on `bg` (light) is 4.39:1, so prefer `muted` on `surface`.

- All ordinary UI components compose shadcn primitives. Tokens are semantic CSS variables with paired foreground values, including portals, charts and dark mode. Do not introduce Material UI or parallel bespoke buttons/forms/dialogs.
- Main navigation is Dashboard / Chat / Settings. User settings owns gadget definitions, connectors/MCP, rules and system preferences. Dashboard Edit only changes layout/instances; grid/handles are hidden in View and changes require Save or Cancel.
- Quick chat is a large right **Sheet** containing New chat, user/assistant messages, composer/send-stop and close. History and advanced controls belong on the full Chat page. Share thread/draft state across both. Ported AnythingLLM UI must obey these component rules; AI uses the server-side OpenAI SDK against configured providers (cloud or local).
- User preferences use **Dialog** with light/dark/system and the two locale choices. Preview is reversible; Save persists both settings, Cancel/close restores them. Reuse the existing preferences owner rather than creating disconnected theme/language stores.
- Keep application language IDs `en-us` and `vi-vi`; normalize centrally to `en-US` and `vi-VN` for next-intl/Intl/HTML lang. Translate visible and accessible UI text through catalogs, not inline language branches. Locale must not change quote currency/timezone or translate source/user content implicitly.
- Use next-themes root class behavior and next-intl App Router patterns; migrate legacy `body[data-theme]` callers coherently. Do not claim server/client/theme hydration works merely because a build passes.
- Recharts through shadcn Chart owns financial graphs. Preserve units, precision, source timestamps, freshness and missing-data gaps. Provide readable theme-aware axes/tooltips and a non-color-only accessible summary. Never invent a Recharts CandlestickChart API.
- Fill viewport width, keep the header, main nav and connection footer fixed (the shell is `100dvh` with `overflow: hidden`; the document never scrolls) so only the body content region (`.workspace-main`) scrolls, with mobile reflow, and preserve desktop layout when viewport changes. Keep shadcn focus management and portal semantics. Do not build duplicate modal traps.

## Delivery discipline

Follow repository GitNexus impact/change-scope requirements for production symbols and the Testing Guidelines and Mandatory Code Documentation sections of `AGENTS.md`.

Record actual dependencies/ported code and notices in the OSS inventory and README at delivery. Distinguish proposed components, interactive mockups, code/build completion and verified runtime behavior. A reference HTML mockup is not evidence that shadcn/Recharts or backend preference persistence is integrated.
