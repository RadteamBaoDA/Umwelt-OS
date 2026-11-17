# Privacy, service inventory and account deletion

Approved scope: self-hosted offering; English/Vietnamese draft Privacy, Terms and
Refunds pages backed by canonical Markdown; evidence-linked third-party inventory;
account deletion with durable owner-scoped erasure and status receipts; frontend
credential review. Legal name/contact/jurisdiction remain explicit placeholders.

## Acceptance

- Public policies work without a session and ship in standalone/Docker builds.
- Deletion requires current password, typed DELETE, Origin/CSRF, and no members or
  live invitations in the owned workspace. Acceptance atomically disables the account
  and revokes sessions. Cleanup never admits ordinary disabled-account access.
- Status receipt exists before acceptance, survives a lost response, reveals no
  content, and lasts seven days after completion. Host recovery handles lost receipts.
- Cleanup covers all persistence owners, originals, derived/copy data, external
  connection state, browser caches and other tabs; other accounts' data survives.
- Required-stage failure is visible and cannot become successful erasure. Account 1
  leaves a non-personal disabled bootstrap tombstone. Backups, host secrets and
  external provider retention are disclosed separately.
- No actual account deletion, live migration/provider call, Docker integration/E2E,
  commit, push, deploy or key rotation is authorized by implementation alone.

## Work ownership and interfaces

1. Policy worker: docs/legal, inventory, public pages/renderer, next.config packaging,
   Docker web packaging and login links. Root owns session-gate/account-shell links.
2. Auth worker: core/auth/deletion_* (except deletion_cleanup), migration, API/worker
   registration, host recovery and auth tests. Owns authorize_account_erasure.
3. Cleanup worker: core/auth/deletion_cleanup.py and module-owned erasure contracts,
   persistence coverage inventory and cleanup tests. Root owns UI/browser clearing.

Shared contracts: core/auth/erasure_contracts.py. Auth exports
authorize_account_erasure(session, scope) from core/auth/deletion_service.py;
cleanup exports erase_account_data(ctx, scope, phase, cursor) -> ErasureProgress
from core/auth/deletion_cleanup.py. Neither holds DB locks over external I/O.

## Execution rulings

- Existing clean feature/fix-ui checkout is the assigned workspace; do not switch
  branches, create worktrees, commit or merge. Workers have disjoint file ownership.
- This explicit approved plan authorizes focused tests/typecheck/lint, superseding
  historical skill instructions deferring original phase validation.
- Existing get_active_account is CRITICAL (17 direct callers/20 flows); preserve its
  admission contract and use separate validated operation-only cleanup authority.
- Preserve other workspaces' content; remove/anonymize the departing identity's
  references through owning modules rather than blanket foreign-workspace deletion.
