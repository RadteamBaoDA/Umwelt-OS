# DropdownMenu

A popover action list: `color-popover` surface, `radius-md`, `shadow-popover`; 14px items with 16px `muted` Lucide icons, focus on `color-accent`, destructive items in `color-destructive`.

Source: `apps/web/src/components/ui/dropdown-menu.tsx` — the full shadcn new-york set (Trigger, Content, Group, Label, Item with `variant="destructive"`, CheckboxItem, RadioGroup/RadioItem, Separator, Shortcut, Sub/SubTrigger/SubContent).

- The header user icon opens it: User settings, account actions, Sign out. Theme and language live here, never in Settings.
- Consumer provides a trigger with a translated accessible name ("User menu") and translated item labels.
