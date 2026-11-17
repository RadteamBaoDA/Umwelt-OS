# Button

The one action control: a 44px, 9px-radius button that is solid `accent` for the primary action, outlined for secondary, and a bare text button for inline links.

Source: `apps/web/src/components/ui/button.tsx` — a thin `<button>` wrapper adding the global `.button` class; props are native `ButtonHTMLAttributes`.

- **Primary** (default): `accent` fill, `on-accent` text, weight 700. One per section.
- **Secondary**: add `className="secondary"` — transparent, `text`, 1px `line` border. Use for Cancel, Retry, Close.
- **Text button**: `.text-button` on a plain `<button>` — `accent` text, no padding; for "Load more", "Refresh".
- **Disabled/pending**: `disabled` → opacity .65, `progress` cursor. Switch to a translated "Saving…" while pending.
- **Pending but focusable**: when the control must keep focus (blocked reason, dialog trigger), use `aria-disabled` plus an `onClick` guard (`if (pending) return`) instead of `disabled`. It gets the same .65 opacity with a `not-allowed` cursor and stays in the tab order. Do not set it for a confirmed end state that should keep full contrast (e.g. "Following").
- Consumer provides: a translated label (sentence case), `type` (`submit` inside forms), `onClick`.
- Don't add new color variants, icon-only buttons without an accessible name, or override `.button` globally.
