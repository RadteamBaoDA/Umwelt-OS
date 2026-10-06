# Umwelt-OS release checklist

Status: preparation in progress, 2026-10-06. This checklist records release gates; it does not certify deployment or product acceptance.

## Current integration boundary

Local develop `00ddd32` includes P01–P11, Collector/MCP, R07, R12, R14 and R15, plus bounded owner-export contracts (`3215939`) and serialized Chat history privacy (`bd79c06`). These integrated tasks passed their prescribed builds and independent source reviews. Additional accepted milestones include Entities/Relationships export (`781c0eb`), onboarding API/UI (`de132d8`) Chat/Memory deletion (`94c7c1b`), retained Timeline/Observations export (`72d72c6`) bounded Chat expiry (`32a6ccc`), demo/named workspace source (`655331b`), visible branding/EN–VI labels (`44cf45a`) and individual-Document raw cleanup (`3d2c51e`). Backup/admission/recovery and the bounded portable-export aggregator are integrated at `5212def`; complete saved GadgetDefinition/eligible retained DailyBrief/schedule coverage is integrated at `bce670b`, and bounded exact Chat copied-evidence cleanup at `f6da2e2`. Document deletion receipt UI (`4416843`), Source canonical/linked-child cleanup (`fe601bc`) and mobile/full Dashboard reconciliation (`98365a1`) are integrated. Document-derived Memory cleanup is integrated at `84c25df` from accepted `adf52e1`, Document-derived Agent evidence cleanup at `2c06b07`, and copied Notifications/Automations/saved-brief Documents cleanup stages at `b4c52b1`. Source purge success is gated on Source historical Documents receipts and Source-local Memory coverage at `00ddd32`. Behavior change: whole-Source purge operations previously reported `succeeded` are reopened to `running` by `p12_source_coverage` until coverage re-settles (reversible on downgrade). All of this is build/review evidence only; runtime, physical-erasure, restore, capacity, provider and test acceptance remain PENDING, as do final release work and runtime mobile/accessibility acceptance. Demo/named workspace seed/reset source is integrated at `655331b`; runtime isolation acceptance remains deferred. Recheck the execution ledger and actual Git revision before producing a release artifact.

## Required evidence

| Gate | Evidence required | Current state |
| --- | --- | --- |
| Production scope | Every original phase and R01–R16 task has an accepted code/build/review receipt and integrated revision | Incomplete |
| Final composed build | Prescribed `./scripts/dev.ps1 build` or `make build`, exact source revision, output and exit code | Pending final composition |
| Clean install | Disposable clean database migrations, owner bootstrap/login, approved seed, authenticated dashboard/chat/settings | Deferred test stage |
| Upgrade | Preserved Phase0 deployment data and secrets, migration chain, upgrade and recovery evidence | Deferred test stage |
| Realtime/proxy | Authenticated SSE replay/reset, long-lived proxy response and client status | Runtime pending |
| Privacy/deletion | Consent, immediate access revocation, exact evidence cleanup and durable retry receipts | P12 implementation incomplete |
| Backup/restore | Encrypted archive, protected keys, isolated restore, component readiness and explicit recovery | Source integrated; restore acceptance deferred |
| Provider integration | Explicit live Google, connector, OmniRoute, graph/n8n/browser receipts for enabled capabilities | Pending |
| Mobile/accessibility | Viewport, keyboard/focus, screen-reader labels, edit-grid and drawer/full-chat behavior | Deferred test stage |
| Capacity | Measured 2-core/8GiB workload, configuration, concurrency, memory/CPU/latency and OOM/disk evidence | Target host unverified |
| OSS | Exact dependencies/copied source revisions, licenses, modifications and notices | Final inventory reconciliation pending |

## Operator preparation

- Follow [deployment.md](deployment.md) and [README.md](../README.md) for the supported installation commands. Validate them against the final integrated source before release.
- Keep PostgreSQL, Redis, API internals and Docker socket private; expose the web origin through HTTPS with secure cookies and exact origin configuration.
- Apply the authenticated realtime proxy settings documented in deployment.md. Check Chat streaming paths too during final proxy acceptance.
- Bootstrap the local owner before linking Google sign-in. Login consent and Gmail collection consent are separate operations.
- Configure approved collectors, MCP servers and remote AI through the native Settings UI. Account, appearance and language remain in the user menu.
- Use explicit fictional seeding only; preserve stable demo identities and owner edits/deletions. Named development workspace reset is implemented and source-reviewed at `655331b`; actual isolation/reset acceptance remains deferred, and it must not be executed against owner data during code work.
- Use the implemented commands in [backup-recovery.md](backup-recovery.md) for encrypted backup, isolated restore, same-operation recovery and retained-project cleanup. Their source is integrated at `5212def`; container builds and isolated validation do not establish successful production recovery.

## Release procedures (documented, not executed)

Every command below exists in `scripts/dev.ps1`, `Makefile`, `scripts/backup.py` or `scripts/restore.py`; none has been run for this document. Record each actual run in the acceptance record only after the deferred test stage.

Expected Alembic head: `p12_evidence_version_index`. P12 lineage: `p12_memory_document_cleanup` → `p12_agent_document_cleanup` → `p12_copied_stage_cleanup` → `p12_source_coverage` → `p12_evidence_version_index` (index on cleanup evidence `document_version_id`; each `down_revision` verified against `infrastructure/postgres/migrations/versions/`; the chain continues from `p12_source_cleanup_children`). Re-verify against the final integrated chain before release.

1. **Clean install.** Use a disposable checkout and empty Docker volumes. `./scripts/dev.ps1 setup`, then `./scripts/dev.ps1 dev` (or `make setup`, `make dev`). Confirm the migration service reaches the expected head, complete owner setup with `SETUP_TOKEN`, optionally run `./scripts/dev.ps1 seed` (`make seed`), and exercise authenticated Dashboard, Chat and Settings.
2. **Phase 0 upgrade.** Start from a preserved Phase 0 database and `.env` (keep the existing secrets; `setup` never replaces `.env`). Back up first (step 5), stop old writers, run `./scripts/dev.ps1 migrate` (`make migrate`), then start the final API/worker. Follow the migration-specific quiescence notes in [deployment.md](deployment.md) (Agent Chat lifecycle, whole-Source cleanup). Structural downgrade functions are not an operational rollback.
3. **Protected proxy paths.** Behind the HTTPS reverse proxy verify `/api/v1/realtime/events` (no buffering/caching, `Last-Event-ID` preserved, read timeout of at least 75 seconds) and the Chat stream `/api/v1/responses/{response_id}/events`. Keep API, Redis, PostgreSQL and the Docker socket private. Collector bearer routes, OAuth callbacks, GitHub webhooks and inbound MCP keep their own credential checks.
4. **Module enable/disable.** Owner-authenticated, CSRF-protected `GET`/`PATCH /api/v1/settings/modules` (Settings, advanced operations). Verify transitive dependency availability, that worker dispatch and native tool calls honor persisted disables, and that deletion, cancellation, approval-decision, privacy-purge and revocation paths remain available (see [operations.md](operations.md)).
5. **Backup and restore.** `make backup BACKUP=<path>.age DRAIN_TIMEOUT=600`; `make restore BACKUP=<path>.age KEEP_ISOLATED=1`; `make backup-recover OPERATION_ID=<uuid> BACKUP=<path>.age`; `make restore-cleanup PROJECT_ID=<project-id>`. PowerShell: `./scripts/dev.ps1 backup|restore|backup-recover|restore-cleanup` with `-BackupPath`, `-KeepIsolated`, `-OperationId`, `-ProjectId`. Details and host `age` requirements are in [backup-recovery.md](backup-recovery.md). `restore_verified` stays `false` until an isolated restore is actually recorded.
6. **Build gate.** `./scripts/dev.ps1 build` (or `make build`) on the final composed revision; deferred `lint`, `typecheck` and `test` run only in the deferred validation stage.

## Acceptance record

```json
{
  "functional_acceptance": "pending",
  "live_integrations": "pending",
  "target_capacity": "pending",
  "restore_verified": false
}
```

Run tests only after all original and supplemental production code/build/review closes. Record actual command, source revision, environment, outcome and unresolved gate for each result. Historical Phase0 evidence does not certify current auth, UI, provider or recovery behavior. Release, push and deployment require their own authorized action.


