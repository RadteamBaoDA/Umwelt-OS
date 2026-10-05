# P04-T4 repair 2 source report

Date: 2026-10-02. Scope: repair the four residual source findings from `task-P04-T4-review-2.md` (F5, F6, F7, F9). Worktree: `C:/Users/doana/.codex/worktrees/bbd-p04-entities/Umwelt-OS`. This report records the current repair 2 patch; production source was not changed while writing it.

## Findings closed at source level

- **F5 — merge target selection:** Search text and the selected target DTO are separate state. Editing the query clears selection and preview. The selected target is retained across pages, and preview/confirmation is bound to its captured id and revision; a changed target revision invalidates the preview rather than silently adopting a newer revision.
- **F6 — metadata provenance:** The document owner’s `metadata_is_version_snapshot` flag now flows through entity evidence and review DTOs and frontend API types. Entity detail, relationship evidence, and review evidence render localized labels distinguishing version-snapshot metadata from current-metadata fallback.
- **F7 — graph pagination:** Initial neighbor request now uses total-node limit 50. Subsequent requests translate remaining row/node/edge capacity into the owner API’s total-node limit by adding the focus node, keeping requests within its supported 2–100 range. Localized entity type labels are reused in graph fallback and nodes.
- **F9 — correction validation and confirmation (repair 2 scope):** Merge/split previews and writes plus suppression use validated form handlers and trimmed bounded reasons. Correction field errors are rendered inline. Suppression, merge and split use AlertDialog with captured revision/reason and consequence details; pending operations disable unsafe dismissal/actions. Localized entity-type labels and Knowledge branding are consumed from the message catalog. **Correction from review 3:** alias removal did not have a confirmation dialog in repair 2 and its first click submitted the delete request with the validated reason. Repair 3 addresses that remaining seam.

The earlier review’s F1–F4 and F8 source closures remain unchanged. This report makes no new runtime or integration claim for those findings.

## Repair 2 files

- `apps/web/src/core/messages.ts`
- `apps/web/src/modules/knowledge/api.ts`
- `apps/web/src/modules/knowledge/entity-detail.tsx`
- `apps/web/src/modules/knowledge/entity-graph.tsx`
- `apps/web/src/modules/knowledge/entity-list.tsx`
- `apps/web/src/modules/knowledge/entity-review-card.tsx`
- `apps/web/src/modules/search/search-page.tsx`
- `modules/knowledge/entities/public.py`
- `modules/knowledge/entities/schemas.py`
- `apps/web/src/components/ui/alert-dialog.tsx` (new)

The separately integrated graph repair is included in this frozen repair 2 source state; it was not edited during this reporting step. Existing repair 1 changes and unrelated pre-existing instruction/skill modifications were preserved.

## Unresolved acceptance gates

Source closure is not runtime acceptance. The prescribed full build remains blocked because Docker Desktop’s Linux engine pipe was unavailable in the prior build attempt; API, worker, web and migration Docker images therefore remain unverified for this repaired tree. Restore Docker and run the prescribed full production build before accepting T4/P04. Tests, lint, standalone typecheck, services, migrations, provider calls, browser/accessibility/history flows, concurrency and retained-evidence deletion behavior, and 2-core/8-GB capacity acceptance remain deferred by the task instructions.

No tests, lint, typecheck, build, service, migration, provider, commit or merge was run for this repair 2 reporting step. The repair 2 production source is frozen in this worktree; only this report was added.
