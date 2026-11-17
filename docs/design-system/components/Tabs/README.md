# Tabs

Segmented view switcher: a 36px track on `color-accent` (= `bg`) with 4px padding; the active trigger lifts onto `color-background` with a small shadow.

Source: `apps/web/src/components/ui/tabs.tsx` — Radix `Tabs` (Root, List, Trigger, Content), Tailwind classes.

- Use for sibling views inside one panel (gadget management, source detail). Not for main navigation.
- Consumer provides `value`/`defaultValue`, translated trigger labels, and one `TabsContent` per trigger.
