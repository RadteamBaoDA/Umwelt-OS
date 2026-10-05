# P04-T4 repair 3 — F9 narrow source repair

Date: 2026-10-02. Scope: close only the two residual F9 findings from `task-P04-T4-review-3.md` in `C:/Users/doana/.codex/worktrees/bbd-p04-entities/Umwelt-OS`. No backend revision protocol or other accepted group was changed.

## Numbered findings closed at source level

1. **Merge/split write failure feedback inside open confirmation dialogs.** Merge and split mutation failures now render within their respective active AlertDialogs as `role="alert"`, so Radix modal content does not hide the only actionable error from sighted or assistive users. The rendering includes a capped server error string and structured 409 conflict details capped to five conflicts, four rows per conflict, five IDs per field and bounded string lengths. The captured preview payload, consequences, reason, revision and pending-state Cancel/Confirm/Escape protections remain in place. The mutation error remains visible for retry while the same dialog stays open.
2. **Alias removal confirmation.** The Remove action now validates through the existing correction form before opening a controlled AlertDialog. It captures alias ID, alias name and trimmed validated reason; the dialog states the consequence and displays the captured alias and reason. Confirm submits only that captured payload. Cancel clears the snapshot, Escape and Cancel are blocked while pending, and mutation failure is rendered as an accessible alert inside the dialog. Success closes the dialog and invalidates entity data. No backend revision protocol was added.

The prior repair 2 report was corrected: its F9 summary now says alias removal had not yet been confirmed in repair 2 and was submitted immediately on the first click.

## Files changed for repair 3

- `apps/web/src/modules/knowledge/entity-detail.tsx`
- `apps/web/src/core/messages.ts` — localized dialog copy for English and Vietnamese.
- `docs/task-reports/task-P04-T4-repair-2.md` — corrected the inaccurate repair 2 alias-removal statement.
- `docs/task-reports/task-P04-T4-repair-3.md` — this report.

No shared dialog wrapper, API client, backend, graph, navigation or other production files were changed for repair 3. Existing unrelated working-tree changes were preserved.

## Impact and verification boundary

GitNexus upstream impact was attempted before editing for `EntityDetail`, `deleteEntityAlias` and the message catalog symbol; each target was unresolved and returned risk `UNKNOWN` with no resolved caller count. I supplemented that result with source tracing: the entity route page renders `EntityDetail`, whose alias Remove handler calls `deleteEntityAlias` from the accepted frontend API client. The new localized strings are consumed by `EntityDetail`. No HIGH/CRITICAL impact warning was returned; the unresolved index result is not treated as proof of zero callers.

Source was inspected after the edit and the repair 3 patch is frozen in this worktree. No tests, lint, standalone typecheck, runtime/browser check, build, service, migration, provider, commit or merge was run. The prescribed full build and independent review remain the next gates. Runtime accessibility, concurrency, migration/provider and 2-core/8-GB capacity acceptance remain deferred.

Repair 3 frozen working-tree SHA256 values:

- `apps/web/src/modules/knowledge/entity-detail.tsx`: `E1D9A2991A52B8D59D72360DA18C3B7E3CA8576B3532B1D77213FC2DDDBBFBE9`
- `apps/web/src/core/messages.ts`: `B08B4E7FD2CEE43EE3AD02A4A72A7298C14EADB0CF55B9459EC3866A22C66E1F`
