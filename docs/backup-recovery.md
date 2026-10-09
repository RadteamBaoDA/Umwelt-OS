# Backup and isolated restore

Install Docker Compose v2.24 or newer and the maintained `age`/`age-keygen` CLI tools. Set `BACKUP_AGE_RECIPIENT` and `BACKUP_AGE_IDENTITY_PATH` in the protected deployment environment; the public recipient must match the local identity file. Keep the identity separately protected. Backup archives are published atomically and contain the deployment configuration inside the age-encrypted stream.

Create a new archive from the repository root:

```sh
make backup BACKUP=./private-backups/umwelt-2026-10-06.age DRAIN_TIMEOUT=600
```

Restore always creates a uniquely named Compose project with its own PostgreSQL and named volumes. It checks the archive checksums, archived database identity, known migration ancestry, migrations to current heads, and component-specific graph/n8n state. Validation API and worker run behind a quiesced backup fence on an internal-only network. n8n workflow activation remains paused for review. `fully_restored` stays false for this isolated validation result; it is not a claim that schedules or owner operations have been reconciled.

```sh
make restore BACKUP=./private-backups/umwelt-2026-10-06.age KEEP_ISOLATED=1
make backup-recover OPERATION_ID=00000000-0000-0000-0000-000000000000 BACKUP=./private-backups/umwelt-2026-10-06.age
make restore-cleanup PROJECT_ID=bbd-restore-0123456789abcdef
```

`restore` removes the isolated project after validation unless `KEEP_ISOLATED=1` is set. Retained projects keep their archived runtime settings and Compose override in the ignored, user-private `.backup-restores/<project-id>/` directory so Docker can restart them; use the printed project ID with `restore-cleanup` to remove the containers, volumes, and retained configuration together. Do not delete that directory manually while its project exists.

Windows PowerShell equivalents are `./scripts/dev.ps1 backup -BackupPath <path>`, `restore -BackupPath <path> [-KeepIsolated]`, `backup-recover -OperationId <uuid> [-BackupPath <archive>]`, and `restore-cleanup -ProjectId <project-id>`.

Portable owner downloads are available as JSON, Markdown, or CSV from Settings → Data export. They contain owner-approved chat history, documents and versions, source metadata, tasks, goals, live News topics, saved dashboards, and retained Memory records where each domain can verify its current evidence. Dataset metadata reports when privacy settings suppress a dataset or unsupported Memory provenance causes records to be omitted. These downloads do not contain raw file bytes, graph state, connector configuration, credentials, runtime state, or deployment identity; use the encrypted instance backup for recovery.

## Workspace data in backups

The `postgres` component is a full `pg_dump --format=custom` of the `bbd` database. Workspace data lives in that database, so it is included without a separate component: `workspaces`, `workspace_memberships`, `workspace_invitations` and `workspace_shares` (`core/workspaces/models.py`), together with the workspace-scoped rows of other modules. The backup and restore code has no workspace-specific logic. It neither exports nor filters workspaces, so a restored database contains the roles, memberships, pending invitations and shares that existed at snapshot time. Restore validation checks migration heads and archive integrity, not workspace contents.

`auth_session` rows are also part of the dump. A restored database therefore contains the sessions that existed at snapshot time. Invalidating them after a cutover is a separate manual step, described in `docs/workspace-scope-contracts.md` section 5.

## Native-collection state

Native collection state is ordinary PostgreSQL data (source provisioning, collection requests, admission slots and receipts) in the `postgres` component. There is no separate native-collection component.

- Backup: admitted activities drain (`active_activities == 0`) before the snapshot. The `worker` and `api` services are stopped at the snapshot boundary, so no native collection request is executing when the dump is taken.
- Restore: the archived in-progress maintenance operation is closed as `incomplete` inside the isolated copy (`_normalize_archived_control` in `modules/backup/host.py`). Restore does not mark any collection request complete. Expired admission slots are recovered by the scheduler's `_recover_expired_slots` when dispatch runs, not by restore.

## Restore pauses schedulers

Scheduling stops at two points. Neither point is a single global switch.

1. Fence in the worker. Every worker cron job is wrapped by `_gate_backup_job` (`apps/worker/main.py`). The wrapper calls `register_activity`, which calls `admit_write`. When the backup coordinator phase is not `idle`, that call raises `BackupAdmissionDenied`, and the wrapper returns without running the job. This includes `dispatch_due_collections`, the native collector scheduler that runs every 15 seconds.
2. Isolated restore. After migrations reach their heads, `make restore` starts a coordinator operation on the restored copy and moves it to `quiesced`. The isolated `worker` and `chat-worker` then start under that fence, so the same gate denies their cron jobs. The override also puts the restored services on an internal-only network. The restore does not change `COLLECTOR_SCHEDULER_ENABLED`. The isolated worker reads the archived `.env`, and `dispatch_due_collections` returns immediately when that flag is `false`. The fence is what pauses dispatch in the restored copy.
3. n8n schedules. Before a backup snapshot, `N8nScheduleGate` pauses active n8n workflows and waits for running executions to stop, then resumes them afterwards. During restore, validation fails if any restored workflow is active. The result reports the count as `<n>_left_paused_for_review`, and `fully_restored` stays `false`.

Manual step: nothing in `modules/backup` reopens the coordinator of a restored copy, re-enables n8n schedules, or sets `COLLECTOR_SCHEDULER_ENABLED`. Promoting a restored instance to live use is an operator action. Set `COLLECTOR_SCHEDULER_ENABLED=false` in that environment if native dispatch must stay off, as described in `docs/connectors/native-and-n8n.md`.
