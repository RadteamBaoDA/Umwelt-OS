"""Declare the always-available backup maintenance control surface."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BackupDescriptor:
    """Expose owner backup control independently of ordinary feature availability."""

    id: str = "backup"
    name: str = "Backup and Restore"
    version: str = "1.0.0"
    description: str = "Durable backup admission, encrypted host snapshots, and isolated restore control."
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    scheduled_jobs: tuple[str, ...] = ()
    provides: tuple[str, ...] = ("backup_control", "backup_restore")
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ("/api/v1/backups",)
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None


descriptor = BackupDescriptor()
