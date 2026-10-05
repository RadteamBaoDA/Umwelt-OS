---
name: umwelt-os-ui-system
description: Design, implement, or review Umwelt-OS frontend UI using its shadcn/ui design system, Recharts financial charts, theme preferences and English/Vietnamese localization. Use for application components, dashboard gadgets, chat surfaces and User settings; not for backend-only changes.
---

# Umwelt-OS UI System

Read [the design system](../../../docs/DESIGN_SYSTEM.md) before UI work. It owns component choices, token/theme/locale contracts and preference behavior. Then read [the design-system brand book](../../../docs/design-system/README.md) and use its [tokens](../../../docs/design-system/tokens.json) and per-component guidelines (`docs/design-system/components/<Name>/README.md`) for exact colors, type styles, spacing, radii, shadows, states and copy voice. Read [the product proposal](../../../docs/ux-proposals/2026-09-26-life-dashboard-proposal.md) only for the feature being changed. Explicit user corrections govern older plans.

## Locate before building

- Read repository guidance and current worktree status. Inspect the current component owner, generated shadcn primitives, `components.json`, tokens, provider setup, preference persistence and lockfile for the affected feature.
- Reuse the responsible component or helper. Add only necessary shadcn components through the official registry/CLI when implementing; review generated diffs. Do not regenerate the whole UI or replace unrelated work.
- Preserve the selected Radix-based shadcn family and Tailwind v4 conventions. Components generated for another family/version can have different imports and composition contracts; consult the official page linked in the design system when needed.

## Non-negotiable UI contracts

- Values come from the design-system tokens: `bg`, `surface`, `text`, `muted`, `line`, `accent`, `on-accent`, `danger` (or their Tailwind aliases), radii `radius-sm/md/lg/card/dialog/panel`, spacing `space-1…space-6`, 44px `control-height`. `apps/web/src/app/globals.css` is the runtime source; keep `docs/design-system/tokens.json` in sync in the same change, and note that `muted` on `bg` (light) is 4.39:1, so prefer `muted` on `surface`.

- All ordinary UI components compose shadcn primitives. Tokens are semantic CSS variables with paired foreground values, including portals, charts and dark mode. Do not introduce Material UI or parallel bespoke buttons/forms/dialogs.
- Main navigation is Dashboard / Chat / Settings. User settings owns gadget definitions, connectors/MCP, rules and system preferences. Dashboard Edit only changes layout/instances; grid/handles are hidden in View and changes require Save or Cancel.
- Quick chat is a large right **Sheet** containing New chat, user/assistant messages, composer/send-stop and close. History and advanced controls belong on the full Chat page. Share thread/draft state across both. Ported AnythingLLM UI must obey these component rules; AI uses server-side OpenAI SDK via Ommi Router, without local AI.
- User preferences use **Dialog** with light/dark/system and the two locale choices. Preview is reversible; Save persists both settings, Cancel/close restores them. Reuse the existing preferences owner rather than creating disconnected theme/language stores.
- Keep application language IDs `en-us` and `vi-vi`; normalize centrally to `en-US` and `vi-VN` for next-intl/Intl/HTML lang. Translate visible and accessible UI text through catalogs, not inline language branches. Locale must not change quote currency/timezone or translate source/user content implicitly.
- Use next-themes root class behavior and next-intl App Router patterns; migrate legacy `body[data-theme]` callers coherently. Do not claim server/client/theme hydration works merely because a build passes.
- Recharts through shadcn Chart owns financial graphs. Preserve units, precision, source timestamps, freshness and missing-data gaps. Provide readable theme-aware axes/tooltips and a non-color-only accessible summary. Never invent a Recharts CandlestickChart API.
- Fill viewport width, allow dashboard document scrolling and mobile reflow, and preserve desktop layout when viewport changes. Keep shadcn focus management and portal semantics. Do not build duplicate modal traps.

## Delivery discipline

Follow repository GitNexus impact/change-scope requirements for production symbols. During the authorized implementation stage run builds only: do not add/run tests, lint or standalone typecheck; deferred behavioral validation belongs to the later test stage.

Record actual dependencies/ported code and notices in the OSS inventory and README at delivery. Distinguish proposed components, interactive mockups, code/build completion and verified runtime behavior. A reference HTML mockup is not evidence that shadcn/Recharts or backend preference persistence is integrated.

## Code documentation required by the owner

Before handing off generated or changed production code, ensure every named function/method/component/hook has JSDoc (JavaScript/TypeScript) or a Python docstring describing its real purpose and contract. Add inline comments for non-obvious invariants, authorization, transaction/lock ordering, retry/idempotency/deletion behavior and UI state transitions. Preserve accurate existing comments and OSS notices; never fabricate safety guarantees or narrate obvious lines. Follow AGENTS.md's Mandatory Code Documentation section for details and existing-code coverage.

Review documentation alongside source behavior. Keep changes limited to comments/docstrings during a documentation task; preserve signatures and application logic. Tests remain deferred under the owner's code/build-only stage. Update checkpoint files continuously and batch their commits with a completed large task or phase instead of committing every status update.

For other authored production languages, use their native named-function documentation format; keep test-only helpers deferred with the test stage.
