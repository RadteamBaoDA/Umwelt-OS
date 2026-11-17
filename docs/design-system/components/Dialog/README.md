# Dialog

A centred modal on `color-background`, `radius-lg`, 24px padding, `shadow-dialog`, over a 50% `text` overlay; max 32rem wide (100% − 2rem on mobile) with a top-right close icon.

Source: `apps/web/src/components/ui/dialog.tsx` — Radix Dialog (Root, Trigger, Portal, Overlay, Content with `showCloseButton` / `closeLabel`, Header, Footer, Title, Description, Close).

- Owns User settings (Appearance RadioGroup + Language Select). Opening snapshots preferences; changes preview live; Save persists both; Cancel/Escape restores both.
- Consumer provides a `DialogTitle` and `DialogDescription` (may be visually hidden), a translated `closeLabel`, and footer buttons: secondary Cancel, then primary Save.
- One modal at a time; never stack a Dialog over the chat Sheet.
