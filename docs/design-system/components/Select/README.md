# Select

A dropdown pick-list: the trigger reuses `.input`; content is a `surface` popover with `radius-md`, 4px padding and `shadow-popover`; items are 40px, `radius-sm`, highlighted on `bg`, checked in `accent` with a check.

Source: `apps/web/src/components/ui/select.tsx` — Radix `Select` (Root, Group, Value, Trigger, Content in a Portal, Item with ItemText + ItemIndicator).

- Consumer provides `value`/`onValueChange`, a `SelectValue` placeholder, and translated `SelectItem` labels. Language names display in their own language ("Tiếng Việt").
- Content portals under `body`; it must still inherit the root theme and `lang`.
