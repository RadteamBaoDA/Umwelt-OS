# Input

A 44px text field on `bg` with a 1px `line` border and `radius-md`, sitting one step below its `surface` panel.

Source: `apps/web/src/components/ui/input.tsx` — native `<input>` with the `.input` class; the same class styles `SelectTrigger` and textareas (`.input.text-area`, 220px min; `.compact` 100px).

- Always pair with a `Label` inside a `.field` (7px gap) — never placeholder-only.
- Validation errors go below in `.error` (`danger`, 14px), translated, telling the user how to fix it.
- Focus ring: 3px `accent` at 40%, 2px offset.
- Consumer provides: `id` matched to the label, value/handlers (React Hook Form + Zod), translated placeholder.
