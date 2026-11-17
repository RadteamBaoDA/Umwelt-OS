# Sheet

A full-height side drawer on `color-background` with a 1px `color-border` edge, sliding from the right over a 40% `text` overlay with a 2px blur.

Source: `apps/web/src/components/ui/sheet.tsx` — Radix Dialog primitives; `SheetContent` takes `side` (`top|bottom|left|right`, default right), `showCloseButton`, `closeLabel`; plus Header, Footer, Title, Description.

- Owns quick chat: about 65vw capped at 56rem on desktop, full width on mobile. Shows only New chat, messages, composer (send/stop) and close. History and model controls belong on the full Chat page.
- Body scrolls on its own; the composer stays pinned in the footer. Focus trap, Escape and focus return come from Radix — keep them.
- Consumer provides a title (may be `sr-only`), a translated `closeLabel`, body and footer.
