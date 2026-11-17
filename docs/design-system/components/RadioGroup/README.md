# RadioGroup

One-of-many choice: 20px circles with a `muted` border; checked gets a 2px `accent` border and a 10px `accent` dot.

Source: `apps/web/src/components/ui/radio-group.tsx` — Radix `RadioGroup.Root` / `Item` / `Indicator` (Lucide `CircleIcon`).

- In User settings each item sits in a `.theme-option` card (44px, `radius-md`); the checked card gets an `accent` border and a 9% `accent` tint.
- Consumer provides `value`, `onValueChange`, and a translated label per item. Arrow-key navigation comes from Radix — keep it.
