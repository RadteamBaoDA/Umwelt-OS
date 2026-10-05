# Checkbox

An 18px square, `radius-xs`, on `bg`; checked fills with `accent` and shows a 14px Lucide check in `bg`.

Source: `apps/web/src/components/ui/checkbox.tsx` — Radix `Checkbox.Root` + `Indicator`, class `.checkbox`.

- Consumer provides `checked` / `onCheckedChange` and a visible `<label>` beside it.
- Use for independent booleans; use RadioGroup for one-of-many.
