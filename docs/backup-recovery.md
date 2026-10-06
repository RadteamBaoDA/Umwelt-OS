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
