# AlertDialog

The confirm-before-destroying modal: Dialog's frame with no close icon, so the user must choose Cancel or the action.

Source: `apps/web/src/components/ui/alert-dialog.tsx` — Radix AlertDialog; `AlertDialogCancel` renders a secondary `Button`, `AlertDialogAction` a primary `Button`.

- Title asks the question; description states the consequence and names the affected items and counts.
- The action label repeats the verb ("Delete source"), never "OK" or "Yes".
- For irreversible confirms pass `variant="destructive"` to `AlertDialogAction`; do not restyle with classes.
- Controlled dialogs without an `AlertDialogTrigger` get no focus return from Radix: store the opener element and focus it from `onCloseAutoFocus` (after `preventDefault()`).
- Consumer provides title, description, and the action's `onClick`.
