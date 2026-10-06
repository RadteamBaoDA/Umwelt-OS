"""Trusted host orchestration for quiesced age-encrypted backups."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from core.config import Settings
from modules.backup.manifest import BackupManifest
from modules.backup.service import build_manifest, extract_snapshot_tar, write_snapshot_tar


class BackupHostError(RuntimeError):
    """Raised when the trusted host runner cannot complete or safely resume a backup."""


def _workflow_schedule_stage(identifier: str, active: bool) -> str:
    """Return a stable bounded receipt key for one workflow schedule mutation direction."""
    action = "activate" if active else "deactivate"
    return f"n8n_{action}_{hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:32]}"


def _build_schedule_gate(
    compose: Compose, settings: Settings, operation_id: str, timeout_seconds: int,
) -> N8nScheduleGate:
    """Build n8n gate callbacks backed by the durable operation receipt journal."""
    def persist_workflows(workflows: list[dict[str, object]]) -> None:
        """Commit the complete original activation-state snapshot before n8n changes."""
        receipt = json.dumps(
            {"workflow_states": [
                {"workflow_id": workflow["id"], "original_active": workflow["active"]}
                for workflow in workflows
            ]},
            separators=(",", ":"),
        ).encode("utf-8")
        compose.coordinator(
            "receipt", "--operation-id", operation_id, "--expected", "draining",
            "--stage", "n8n_workflow_states", "--receipt-stdin", input_data=receipt,
        )

    def persist_effect(
        stage: str, receipt: dict[str, object], create: bool, expected: str,
    ) -> None:
        """Commit one bounded per-workflow attempt or outcome receipt."""
        action = "receipt" if create else "update-receipt"
        compose.coordinator(
            action, "--operation-id", operation_id,
            "--expected", expected, "--stage", stage,
            "--receipt", json.dumps(receipt, separators=(",", ":")),
        )

    return N8nScheduleGate(settings, timeout_seconds, operation_id, persist_workflows, persist_effect)


class Compose:
    """Run fixed project Compose files while suppressing credential-bearing diagnostics."""

    def __init__(
        self, root: Path, settings: Settings, project_name: str | None = None,
        *, include_connectors: bool = True, env_file: Path | None = None,
        extra_compose_file: Path | None = None,
    ) -> None:
        """Bind Compose operations to the fixed deployment project and file inventory."""
        self.root = root.resolve()
        self.settings = settings
        self.project_name = project_name
        self.env_file = env_file
        self.files = [
            "docker-compose.yml",
            "infrastructure/graph/compose.yml",
            "infrastructure/backup/compose.yml",
        ]
        if include_connectors:
            self.files.insert(1, "docker-compose.connectors.yml")
        if extra_compose_file is not None:
            self.files.append(str(extra_compose_file.resolve()))

    def command(self, *args: str) -> list[str]:
        """Build a command from the fixed Compose files and caller-supplied arguments."""
        prefix = ["docker", "compose"]
        if self.project_name is not None:
            prefix.extend(["--project-name", self.project_name])
        if self.env_file is not None:
            prefix.extend(["--env-file", str(self.env_file.resolve())])
        for compose_file in self.files:
            prefix.extend(["-f", compose_file])
        return prefix + list(args)

    def capture(
        self, *args: str, stdin: BinaryIO | None = None, input_data: bytes | None = None,
    ) -> bytes:
        """Capture bounded command output while suppressing potentially sensitive stderr."""
        if stdin is not None and input_data is not None:
            raise BackupHostError("Compose command received conflicting input streams")
        try:
            result = subprocess.run(
                self.command(*args), cwd=self.root,
                stdin=stdin,
                input=input_data,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
            )
        except OSError as exc:
            raise BackupHostError("Docker Compose is unavailable") from exc
        if result.returncode != 0:
            raise BackupHostError("Docker Compose maintenance command failed")
        if len(result.stdout) > 8 * 1024 * 1024:
            raise BackupHostError("Docker Compose returned an oversized maintenance receipt")
        return result.stdout

    def quiet(self, *args: str) -> None:
        """Run a maintenance command without exposing service diagnostics to the operator."""
        try:
            result = subprocess.run(
                self.command(*args), cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except OSError as exc:
            raise BackupHostError("Docker Compose is unavailable") from exc
        if result.returncode != 0:
            raise BackupHostError("Docker Compose maintenance command failed")

    def stream_to_file(self, destination: Path, *args: str) -> int:
        """Stream a bounded host-side helper output into a private staging file."""
        try:
            with destination.open("xb") as output:
                result = subprocess.run(
                    self.command(*args), cwd=self.root, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.DEVNULL, check=False,
                )
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise BackupHostError("Docker Compose snapshot helper failed") from exc
        if result.returncode != 0:
            destination.unlink(missing_ok=True)
            raise BackupHostError("Docker Compose snapshot helper failed")
        return destination.stat().st_size

    def service_names(self) -> set[str]:
        """Return the currently running service names from the fixed Compose project."""
        output = self.capture("ps", "--services", "--status", "running")
        return {line.strip() for line in output.decode("utf-8", "strict").splitlines() if line.strip()}

    def coordinator(
        self, *args: str, stdin: BinaryIO | None = None, input_data: bytes | None = None,
    ) -> dict[str, Any]:
        """Call one short-lived coordinator action and validate its public JSON result."""
        output = self.capture(
            "run", "--rm", "--no-deps", "-T", "api",
            "python", "-m", "modules.backup.coordinator", *args,
            stdin=stdin, input_data=input_data,
        )
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackupHostError("Backup coordinator returned an invalid receipt") from exc
        if not isinstance(payload, dict) or "error" in payload:
            raise BackupHostError("Backup coordinator rejected a state transition")
        return payload

    def export_volume(self, name: str, destination: Path) -> int:
        """Stream a named volume through the no-network backup helper into staging."""
        return self.stream_to_file(
            destination, "run", "--rm", "--no-deps", "-T", "backup-volume", "export", name,
        )

    def import_volume(self, name: str, source: BinaryIO) -> None:
        """Import one validated component tar into an empty project-scoped named volume."""
        try:
            result = subprocess.run(
                self.command(
                    "run", "--rm", "--no-deps", "-T", "backup-volume-restore", "restore", name,
                ), cwd=self.root, stdin=source,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except OSError as exc:
            raise BackupHostError("Docker Compose restore helper failed") from exc
        if result.returncode != 0:
            raise BackupHostError("Docker Compose restore helper failed")

    def feed_file(self, source: Path, *args: str) -> None:
        """Feed a protected staged file to a fixed Compose maintenance command."""
        try:
            with source.open("rb") as input_file:
                result = subprocess.run(
                    self.command(*args), cwd=self.root, stdin=input_file,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
        except OSError as exc:
            raise BackupHostError("Docker Compose restore command failed") from exc
        if result.returncode != 0:
            raise BackupHostError("Docker Compose restore command failed")


class N8nScheduleGate:
    """Pause exact active n8n workflows and wait until their running executions stop."""

    def __init__(
        self, settings: Settings, timeout_seconds: int,
        operation_id: str, persist_workflows: Any, persist_effect: Any,
    ) -> None:
        """Configure authenticated n8n control and the durable pre-effect receipt hook."""
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.operation_id = operation_id
        self.persist_workflows = persist_workflows
        self.persist_effect = persist_effect
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{settings.n8n_port}/api/v1",
            headers={"X-N8N-API-KEY": settings.n8n_api_key.get_secret_value()},
            timeout=5,
            trust_env=False,
        )
        self.workflows: list[dict[str, object]] = []

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue one authenticated API call and reject malformed or oversized receipts."""
        try:
            with self.client.stream(method, path, **kwargs) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length is not None and int(length) > 8 * 1024 * 1024:
                    raise BackupHostError("n8n returned an oversized schedule receipt")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        raise BackupHostError("n8n returned an oversized schedule receipt")
                    chunks.append(chunk)
                result = json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError) as exc:
            raise BackupHostError("n8n schedule control is unavailable") from exc
        if not isinstance(result, dict):
            raise BackupHostError("n8n returned an invalid schedule receipt")
        return result

    def _workflows(self) -> list[dict[str, object]]:
        """Read the bounded, paginated original workflow activation state projection."""
        cursor: str | None = None
        rows: list[dict[str, object]] = []
        while True:
            params: dict[str, object] = {"limit": 250}
            if cursor is not None:
                params["cursor"] = cursor
            result = self._json("GET", "/workflows", params=params)
            data = result.get("data")
            if not isinstance(data, list):
                raise BackupHostError("n8n workflow projection is invalid")
            for item in data:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or not isinstance(item.get("active"), bool)):
                    raise BackupHostError("n8n workflow identity is invalid")
                rows.append({"id": item["id"], "active": item["active"]})
                if len(rows) > 10_000:
                    raise BackupHostError("n8n workflow inventory exceeds its bound")
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return rows
            if not isinstance(next_cursor, str) or len(next_cursor) > 256 or next_cursor == cursor:
                raise BackupHostError("n8n workflow cursor is invalid")
            cursor = next_cursor

    def pause_and_drain(self) -> list[dict[str, object]]:
        """Persist every original workflow state, deactivate active workflows, and drain runs."""
        self.workflows = self._workflows()
        # Store state in the durable operation before any network mutation. A host
        # crash leaves enough information for explicit operator reconciliation.
        self.persist_workflows(self.workflows)
        failures: list[str] = []
        for workflow in self.workflows:
            if workflow["active"]:
                identifier = str(workflow["id"])
                try:
                    self._set_schedule(identifier, active=False, expected="draining", create=True)
                except BackupHostError:
                    failures.append(identifier)
        self._drain_executions()
        if failures:
            raise BackupHostError("One or more n8n workflows could not be paused with a known outcome")
        return self.workflows

    def pause_saved_and_drain(
        self, workflow_states: list[dict[str, object]], expected: str,
    ) -> None:
        """Re-pause original schedules from durable state before reopening a fenced API."""
        failures: list[str] = []
        for workflow in workflow_states:
            if workflow.get("original_active") is True:
                identifier = str(workflow["workflow_id"])
                try:
                    self._set_schedule(identifier, active=False, expected=expected, create=False)
                except BackupHostError:
                    failures.append(identifier)
        self._drain_executions()
        if failures:
            raise BackupHostError("n8n schedules remain uncertain and cannot be safely released")

    def _drain_executions(self) -> None:
        """Wait for every running n8n execution to finish within the configured bound."""
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            result = self._json("GET", "/executions", params={"status": "running", "limit": 100})
            executions = result.get("data")
            if not isinstance(executions, list):
                raise BackupHostError("n8n execution projection is invalid")
            if not executions:
                return self.workflows
            if time.monotonic() >= deadline:
                raise BackupHostError("n8n executions did not drain before the deadline")
            time.sleep(2)

    def _set_schedule(self, identifier: str, *, active: bool, expected: str, create: bool) -> None:
        """Journal an activation intent before its network call and its final effect outcome."""
        stage = _workflow_schedule_stage(identifier, active)
        intent = {
            "status": "incomplete", "workflow_id": identifier,
            "original_active": True, "effect_status": "attempted",
        }
        self._persist_effect(stage, intent, create=create, expected=expected)
        endpoint = "activate" if active else "deactivate"
        try:
            self._json("POST", f"/workflows/{quote(identifier, safe='')}/{endpoint}")
        except BackupHostError:
            uncertain = {**intent, "effect_status": "uncertain"}
            try:
                self._persist_effect(stage, uncertain, create=False, expected=expected)
            except BackupHostError:
                pass
            raise
        succeeded = {**intent, "status": "complete", "effect_status": "succeeded"}
        self._persist_effect(stage, succeeded, create=False, expected=expected)

    def resume(
        self, workflow_states: list[dict[str, object]], operation: dict[str, Any],
    ) -> list[str]:
        """Reconcile each originally active workflow using durable intent and outcome receipts."""
        if operation.get("id") != self.operation_id:
            raise BackupHostError("Backup operation identity changed during n8n recovery")
        receipts = operation.get("stage_receipts")
        expected = operation.get("status")
        if not isinstance(receipts, dict) or expected not in {"resuming", "failed_recovery_required"}:
            raise BackupHostError("Backup operation receipts are unavailable")
        failures: list[str] = []
        for workflow in reversed(workflow_states):
            if workflow.get("original_active") is not True:
                continue
            identifier = str(workflow["workflow_id"])
            stage = _workflow_schedule_stage(identifier, True)
            existing = receipts.get(stage)
            if isinstance(existing, dict) and existing.get("effect_status") == "succeeded":
                continue
            intent = {
                "status": "incomplete", "workflow_id": identifier,
                "original_active": True, "effect_status": "attempted",
            }
            try:
                self._set_schedule(
                    identifier, active=True, expected=expected, create=(existing is None),
                )
            except BackupHostError:
                failures.append(identifier)
                receipts[stage] = {**intent, "effect_status": "uncertain"}
                continue
            receipts[stage] = {**intent, "effect_status": "succeeded", "status": "complete"}
        return failures

    def _persist_effect(
        self, stage: str, receipt: dict[str, object], *, create: bool, expected: str,
    ) -> None:
        """Persist one per-workflow activation intent or terminal effect outcome."""
        self.persist_effect(stage, receipt, create, expected)

    def close(self) -> None:
        """Close the authenticated HTTP client without changing workflow state."""
        self.client.close()


def _identity_path(settings: Settings, root: Path | None = None) -> Path:
    """Resolve and validate the operator-protected age identity file."""
    if settings.backup_age_identity_path is None:
        raise BackupHostError("BACKUP_AGE_IDENTITY_PATH is required")
    path = settings.backup_age_identity_path.expanduser()
    if not path.is_absolute():
        path = (root or Path.cwd()) / path
    if path.is_symlink() or not path.is_file():
        raise BackupHostError("Age identity must be an existing regular file")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BackupHostError("Age identity permissions must exclude group and other access")
    return path.resolve(strict=True)


def _age_tools(settings: Settings, root: Path | None = None) -> tuple[str, str, Path]:
    """Validate standard age tooling and prove its identity matches the recipient."""
    recipient = settings.backup_age_recipient.strip()
    if not recipient.startswith("age1") or re.search(r"\s", recipient) or len(recipient) > 128:
        raise BackupHostError("BACKUP_AGE_RECIPIENT must be one age public recipient")
    age = shutil.which("age")
    age_keygen = shutil.which("age-keygen")
    if age is None or age_keygen is None:
        raise BackupHostError("Install the maintained age and age-keygen command line tools")
    identity = _identity_path(settings, root)
    try:
        result = subprocess.run(
            [age_keygen, "-y", str(identity)], cwd=root or Path.cwd(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False,
        )
    except OSError as exc:
        raise BackupHostError("Age key identity could not be inspected") from exc
    if result.returncode != 0 or result.stdout.decode("ascii", "replace").strip() != recipient:
        raise BackupHostError("Configured age recipient does not match the protected identity")
    return age, recipient, identity


def _copy_configuration(root: Path, stage: Path) -> None:
    """Copy deployment environment and fixed configuration while preserving paths."""
    source_env = root / ".env"
    if source_env.is_symlink() or not source_env.is_file():
        raise BackupHostError("The deployment .env file is required for a complete backup")
    directory = stage / "configuration"
    shutil.copyfile(source_env, directory / "runtime.env")
    for relative in (
        "docker-compose.yml", "docker-compose.connectors.yml",
        "infrastructure/graph/compose.yml", "infrastructure/backup/compose.yml",
        "infrastructure/docker/api.Dockerfile",
        "infrastructure/docker/n8n-connectors.Dockerfile",
        "infrastructure/docker/n8n-connectors-entrypoint.sh",
    ):
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise BackupHostError("Deployment configuration contains an unsupported entry")
        destination = directory / "deployment" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    workflow_dir = directory / "n8n-workflows"
    workflow_dir.mkdir(mode=0o700)
    for workflow in sorted((root / "infrastructure/n8n/workflows").glob("*.json")):
        if workflow.is_symlink() or not workflow.is_file():
            raise BackupHostError("Deployment workflow catalog contains an unsupported entry")
        shutil.copyfile(workflow, workflow_dir / workflow.name)


def _prepare_n8n_key(compose: Compose, settings: Settings, stage: Path) -> str:
    """Verify and stage the live n8n key inside the encrypted payload."""
    live_key = _verify_live_n8n_key(compose, settings)
    key_directory = stage / "n8n"
    key_file = key_directory / "encryption-key"
    key_file.write_text(live_key, encoding="utf-8")
    if os.name != "nt":
        key_file.chmod(0o600)
    return live_key


def _verify_live_n8n_key(compose: Compose, settings: Settings) -> str:
    """Prove the running n8n data key matches the configured service key without printing it."""
    configured_key = settings.n8n_encryption_key.get_secret_value()
    if not configured_key:
        raise BackupHostError("N8N_ENCRYPTION_KEY is required for configured n8n")
    output = compose.capture("exec", "-T", "n8n", "printenv", "N8N_ENCRYPTION_KEY")
    live_key = output.decode("utf-8", "strict").strip()
    if not live_key or live_key != configured_key:
        raise BackupHostError("Configured n8n encryption key does not match the live service")
    return live_key


def _record_receipt(compose: Compose, operation_id: str, expected: str,
                    stage: str, receipt: dict[str, object]) -> None:
    """Persist one sanitized operation stage receipt through the coordinator."""
    compose.coordinator(
        "receipt", "--operation-id", operation_id, "--expected", expected,
        "--stage", stage, "--receipt", json.dumps(receipt, separators=(",", ":")),
    )


def _publish_phase(compose: Compose, operation_id: str, expected: str, phase: str) -> None:
    """Compare-and-swap the durable control and operation phase."""
    compose.coordinator(
        "transition", "--operation-id", operation_id, "--expected", expected, "--phase", phase,
    )


def _load_operation(compose: Compose, operation_id: str) -> dict[str, Any]:
    """Load the durable public operation projection used for restart recovery."""
    return compose.coordinator("operation", "--operation-id", operation_id)


def _load_workflow_states(operation: dict[str, Any]) -> list[dict[str, object]]:
    """Extract the exact original workflow activation states from durable receipts."""
    receipts = operation.get("stage_receipts")
    snapshot = receipts.get("n8n_workflow_states") if isinstance(receipts, dict) else None
    states = snapshot.get("workflow_states") if isinstance(snapshot, dict) else None
    if not isinstance(states, list):
        raise BackupHostError("Durable n8n workflow-state snapshot is unavailable")
    return states


def _verify_services_running(compose: Compose, required: set[str]) -> None:
    """Require all core services and configured components to be running before release."""
    if not required.issubset(compose.service_names()):
        raise BackupHostError("Restored core services are not all running")


def _wait_for_api_ready(compose: Compose, timeout_seconds: int = 180) -> None:
    """Wait until the isolated API reports readiness without exposing its diagnostics."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            compose.quiet(
                "exec", "-T", "api", "python", "-c",
                "import urllib.request; opener=urllib.request.build_opener("
                "urllib.request.ProxyHandler({})); opener.open("
                "'http://127.0.0.1:8000/api/v1/system/ready', timeout=2)",
            )
            return
        except BackupHostError:
            time.sleep(2)
    raise BackupHostError("Isolated API did not become ready")


def _wait_for_worker_ready(compose: Compose, timeout_seconds: int = 180) -> None:
    """Wait for the ARQ worker heartbeat while its owner admission fence is closed."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            ttl = int(compose.capture(
                "exec", "-T", "redis", "redis-cli", "TTL", "bbd:worker:health",
            ).decode("ascii", "strict").strip())
            # ARQ .28 publishes the configured 15-second heartbeat with a 2x TTL.
            if 0 < ttl <= 30:
                return
        except (BackupHostError, UnicodeDecodeError):
            pass
        time.sleep(2)
    raise BackupHostError("Isolated worker did not publish its readiness heartbeat")


def _write_restore_override(path: Path, archived_env: Path) -> None:
    """Write archived settings and an internal-only network for isolated validation."""
    env_path = json.dumps(str(archived_env.resolve()).replace("\\", "/"))
    path.write_text(
        "services:\n"
        "  migrate:\n"
        "    env_file: !override\n"
        f"      - {env_path}\n"
        "  api:\n"
        "    env_file: !override\n"
        f"      - {env_path}\n"
        "  worker:\n"
        "    env_file: !override\n"
        f"      - {env_path}\n"
        "networks:\n"
        "  default:\n"
        "    internal: true\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)


def _retained_restore_directory(root: Path, project_name: str) -> Path:
    """Return a new private staging directory for one retained isolated project."""
    root = root.resolve(strict=True)
    if not re.fullmatch(r"bbd-restore-[0-9a-f]{16}", project_name):
        raise BackupHostError("Isolated restore project identity is invalid")
    base = root / ".backup-restores"
    if base.is_symlink():
        raise BackupHostError("Isolated restore staging directory cannot be a symbolic link")
    base.mkdir(mode=0o700, exist_ok=True)
    if not base.is_dir() or base.is_symlink():
        raise BackupHostError("Isolated restore staging directory is invalid")
    _secure_retained_path(base, directory=True)
    destination = base / project_name
    if destination.exists() or destination.is_symlink():
        raise BackupHostError("Isolated restore staging identity already exists")
    destination.mkdir(mode=0o700)
    try:
        _secure_retained_path(destination, directory=True)
    except BaseException:
        destination.rmdir()
        raise
    marker = destination / "project-id"
    try:
        marker.write_text(project_name, encoding="ascii")
        _secure_retained_path(marker, directory=False)
    except BaseException as exc:
        marker.unlink(missing_ok=True)
        destination.rmdir()
        if isinstance(exc, BackupHostError):
            raise
        raise BackupHostError("Isolated restore staging ownership could not be recorded") from exc
    return destination


def _secure_retained_path(path: Path, *, directory: bool) -> None:
    """Restrict retained restore settings to the current user on POSIX and Windows."""
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    try:
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
        if identity.returncode != 0 or len(identity.stdout) > 16 * 1024:
            raise BackupHostError("Windows restore settings owner could not be resolved")
        rows = list(csv.reader(identity.stdout.decode("mbcs", "strict").splitlines()))
        sid = rows[0][-1] if len(rows) == 1 and len(rows[0]) >= 2 else ""
        if not re.fullmatch(r"S-1-(?:[0-9]+-){1,14}[0-9]+", sid):
            raise BackupHostError("Windows restore settings owner is invalid")
        permissions = "(OI)(CI)F" if directory else "F"
        secured = subprocess.run(
            ["icacls", str(path.resolve()), "/inheritance:r", "/grant:r", f"*{sid}:{permissions}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as exc:
        raise BackupHostError("Windows restore settings permissions could not be secured") from exc
    if secured.returncode != 0:
        raise BackupHostError("Windows restore settings permissions could not be secured")


def _discard_retained_restore_directory(root: Path, project_name: str) -> None:
    """Remove only the three owned files inside one validated restore staging directory."""
    if not re.fullmatch(r"bbd-restore-[0-9a-f]{16}", project_name):
        raise BackupHostError("Isolated restore project identity is invalid")
    base = root.resolve(strict=True) / ".backup-restores"
    destination = base / project_name
    if base.is_symlink() or destination.is_symlink():
        raise BackupHostError("Isolated restore staging path cannot contain symbolic links")
    if not destination.exists():
        return
    resolved_base = base.resolve(strict=True)
    resolved_destination = destination.resolve(strict=True)
    if (resolved_base.parent != root.resolve(strict=True)
            or resolved_destination.parent != resolved_base or not resolved_destination.is_dir()):
        raise BackupHostError("Isolated restore staging path escaped its owner directory")
    marker = destination / "project-id"
    if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="ascii") != project_name:
        raise BackupHostError("Isolated restore staging ownership marker is invalid")
    for filename in ("compose.override.yml", "runtime.env", "project-id"):
        path = destination / filename
        if path.is_symlink():
            raise BackupHostError("Isolated restore staging contains an unexpected symbolic link")
        path.unlink(missing_ok=True)
    destination.rmdir()


def cleanup_isolated_restore(
    project_name: str, *, root: Path | None = None,
) -> dict[str, str]:
    """Stop and remove one retained isolated restore project and its protected settings."""
    root = (root or Path.cwd()).resolve(strict=True)
    if not re.fullmatch(r"bbd-restore-[0-9a-f]{16}", project_name):
        raise BackupHostError("Isolated restore project identity is invalid")
    staging = root / ".backup-restores" / project_name
    base = root / ".backup-restores"
    env_file = staging / "runtime.env"
    override = staging / "compose.override.yml"
    marker = staging / "project-id"
    if (base.is_symlink() or not base.is_dir()
            or staging.is_symlink() or env_file.is_symlink() or override.is_symlink()
            or marker.is_symlink() or not env_file.is_file() or not override.is_file()
            or not marker.is_file() or marker.read_text(encoding="ascii") != project_name):
        raise BackupHostError("Retained isolated restore configuration is unavailable")
    resolved_base = base.resolve(strict=True)
    resolved_staging = staging.resolve(strict=True)
    if (resolved_base.parent != root or resolved_staging.parent != resolved_base
            or env_file.resolve(strict=True).parent != resolved_staging
            or override.resolve(strict=True).parent != resolved_staging
            or marker.resolve(strict=True).parent != resolved_staging):
        raise BackupHostError("Retained isolated restore configuration escaped its owner directory")
    compose = Compose(
        root, Settings(_env_file=env_file), project_name=project_name, include_connectors=False,
        env_file=env_file, extra_compose_file=override,
    )
    compose.quiet("down", "--volumes", "--remove-orphans")
    _discard_retained_restore_directory(root, project_name)
    return {"project_name": project_name, "status": "removed"}


def _record_ready_services(compose: Compose, operation_id: str, expected: str,
                           required: set[str]) -> None:
    """Persist a small readiness receipt after Compose confirms every required service."""
    _verify_services_running(compose, required)
    _record_receipt(compose, operation_id, expected, "core_services", {
        "status": "complete", "components": {name: "complete" for name in sorted(required)},
    })


def _normalize_archived_control(compose: Compose) -> None:
    """Close the source snapshot's expected in-progress operation inside the isolated copy."""
    control = compose.coordinator("status")
    if control.get("phase") == "idle" and control.get("operation_id") is None:
        return
    operation_id = control.get("operation_id")
    if control.get("phase") != "snapshotting" or not isinstance(operation_id, str):
        raise BackupHostError("Archived maintenance control state is not safe to normalize")
    operation = compose.coordinator("operation", "--operation-id", operation_id)
    if operation.get("status") != "snapshotting":
        raise BackupHostError("Archived backup operation receipt is inconsistent")
    compose.coordinator(
        "transition", "--operation-id", operation_id,
        "--expected", "snapshotting", "--phase", "resuming",
    )
    compose.coordinator(
        "resume", "--operation-id", operation_id, "--outcome", "incomplete",
    )
    control = compose.coordinator("status")
    if control.get("phase") != "idle" or control.get("operation_id") is not None:
        raise BackupHostError("Archived operation could not be closed inside the isolated copy")


def _record_stage_failure(
    compose: Compose, operation_id: str, expected: str, stage: str, detail_code: str,
) -> None:
    """Record a bounded failure outcome without leaking host paths or subprocess output."""
    _record_receipt(compose, operation_id, expected, stage, {
        "status": "failed", "detail_code": detail_code,
    })


def _stop_service(compose: Compose, names: set[str], stopped: list[str], service: str) -> None:
    """Stop one running service and remember it only after Compose confirms success."""
    if service in names and service not in stopped:
        compose.quiet("stop", service)
        stopped.append(service)


def _start_services(compose: Compose, stopped: list[str]) -> None:
    """Restart previously stopped services in dependency-reverse order."""
    for service in reversed(stopped):
        compose.quiet("start", service)
    stopped.clear()


def _encrypt_age(
    age: str, recipient: str, destination: Path, stage: Path, manifest: object,
) -> None:
    """Stream the gzip tar directly through maintained age encryption to an atomic path."""
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.partial")
    try:
        with temporary.open("xb") as output:
            process = subprocess.Popen(
                [age, "--recipient", recipient], cwd=stage.parent,
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
            )
            if process.stdin is None:
                process.kill()
                raise BackupHostError("Age encryption pipe could not be opened")
            try:
                write_snapshot_tar(stage, process.stdin, manifest=manifest)
                process.stdin.close()
                result = process.wait(timeout=3600)
            except BaseException:
                process.stdin.close()
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            if result != 0:
                raise BackupHostError("Age encryption failed")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise BackupHostError("Backup archive already exists; choose a different path") from exc
    except OSError as exc:
        raise BackupHostError("Age could not publish the encrypted archive") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _decrypt_snapshot(age: str, identity: Path, archive_path: Path, stage: Path) -> BackupManifest:
    """Decrypt directly into a bounded safe extractor and require age to exit successfully."""
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [age, "--decrypt", "--identity", str(identity), str(archive_path)],
            cwd=archive_path.parent, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            process.kill()
            raise BackupHostError("Age decryption pipe could not be opened")
        manifest = extract_snapshot_tar(process.stdout, stage)
        result = process.wait(timeout=3600)
        if result != 0:
            raise BackupHostError("Age decryption failed")
        return manifest
    except BackupHostError:
        raise
    except BaseException as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise BackupHostError("Backup archive could not be decrypted and verified") from exc
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _verify_manifest_keys(manifest: BackupManifest, stage: Path,
                          recipient: str) -> dict[str, str]:
    """Match protected-key digests and n8n payload state before isolated restore."""
    references = {
        reference.purpose: reference.sha256
        for reference in manifest.required_key_references
    }
    env_path = stage / "configuration" / "runtime.env"
    if not env_path.is_file() or env_path.is_symlink():
        raise BackupHostError("Backup deployment configuration is unavailable")
    try:
        archived_env = dotenv_values(env_path, interpolate=False)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BackupHostError("Backup deployment configuration is invalid") from exc
    expected_recipient = archived_env.get("BACKUP_AGE_RECIPIENT")
    if (not expected_recipient or expected_recipient != recipient
            or references.get("AGE_RECIPIENT") != hashlib.sha256(expected_recipient.encode()).hexdigest()):
        raise BackupHostError("Backup age recipient does not match the protected configuration")
    expected_values = {
        "POSTGRES_PASSWORD": archived_env.get("POSTGRES_PASSWORD"),
        "AI_CREDENTIAL_ENCRYPTION_KEY": archived_env.get("AI_CREDENTIAL_ENCRYPTION_KEY"),
        "CONNECTOR_CREDENTIAL_ENCRYPTION_KEY": archived_env.get("CONNECTOR_CREDENTIAL_ENCRYPTION_KEY"),
        "GRAPH_PASSWORD": archived_env.get("GRAPH_PASSWORD"),
        "N8N_ENCRYPTION_KEY": archived_env.get("N8N_ENCRYPTION_KEY"),
    }
    for purpose in expected_values:
        expected = expected_values[purpose]
        if ((expected and purpose not in references)
                or (purpose in references and (
                    not isinstance(expected, str)
                    or references[purpose] != hashlib.sha256(expected.encode("utf-8")).hexdigest()
                ))):
            raise BackupHostError("Backup protected configuration key does not match its reference")
    components = {component.name: component for component in manifest.components}
    for required in ("postgres", "raw_files", "configuration"):
        if required not in components or components[required].status != "complete":
            raise BackupHostError("Backup is missing a required restore component")
    n8n = components.get("n8n")
    if n8n is not None and n8n.status == "complete":
        key_path = stage / "n8n" / "encryption-key"
        key = key_path.read_text(encoding="utf-8").strip()
        configured_key = archived_env.get("N8N_ENCRYPTION_KEY")
        if (not key or not configured_key or key != configured_key
                or references.get("N8N_ENCRYPTION_KEY") != hashlib.sha256(key.encode()).hexdigest()):
            raise BackupHostError("Backup n8n encryption key does not match the configured key")
        workflow_state_path = stage / "n8n" / "workflow_state.json"
        try:
            workflow_state = json.loads(workflow_state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupHostError("Backup n8n schedule state is invalid") from exc
        if not isinstance(workflow_state, list) or len(workflow_state) > 10_000:
            raise BackupHostError("Backup n8n schedule inventory exceeds its bound")
        workflow_ids: set[str] = set()
        for item in workflow_state:
            if (not isinstance(item, dict) or set(item) != {"id", "active"}
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or len(item["id"]) > 256
                    or any(ord(char) < 32 for char in item["id"])
                    or not isinstance(item.get("active"), bool)
                    or item["id"] in workflow_ids):
                raise BackupHostError("Backup n8n schedule inventory is invalid")
            workflow_ids.add(item["id"])
    return {component.name: component.status for component in manifest.components}


def _wait_for_postgres(compose: Compose, timeout_seconds: int = 180) -> None:
    """Wait a bounded interval for only the isolated restore database to accept connections."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            compose.quiet("exec", "-T", "postgres", "pg_isready", "-U", "bbd", "-d", "bbd")
            return
        except BackupHostError:
            time.sleep(2)
    raise BackupHostError("Isolated PostgreSQL did not become ready")


def _known_migrations(compose: Compose) -> set[str]:
    """Read migration revision IDs from the currently built application image."""
    output = compose.capture(
        "run", "--rm", "--no-deps", "-T", "api", "alembic", "history", "--verbose",
    ).decode("utf-8", "strict")
    return set(re.findall(r"(?m)^\s*Rev:\s+([A-Za-z0-9_-]+)", output))


def _database_migrations(compose: Compose) -> set[str]:
    """Read only migration identifiers from the isolated restored database."""
    output = compose.capture(
        "exec", "-T", "postgres", "psql", "-X", "-A", "-t", "-U", "bbd", "-d", "bbd",
        "-c", "SELECT version_num FROM alembic_version ORDER BY version_num",
    )
    return {line.strip() for line in output.decode("utf-8", "strict").splitlines() if line.strip()}


def _write_n8n_validation_env(destination: Path, archived_env: Path) -> None:
    """Materialize only the archived n8n key for a no-network credential decryption check."""
    values = dotenv_values(archived_env, interpolate=False)
    key = values.get("N8N_ENCRYPTION_KEY")
    if not isinstance(key, str) or not key or any(char in key for char in "\r\n\0"):
        raise BackupHostError("Archived n8n encryption key is invalid")
    destination.write_text(f"N8N_ENCRYPTION_KEY={key}\n", encoding="utf-8")
    if os.name != "nt":
        destination.chmod(0o600)


def _run_n8n_validation(
    project_name: str, env_file: Path, audit_directory: Path,
    workflow_states: list[dict[str, object]],
) -> int:
    """Use the pinned offline n8n CLI to decrypt credentials and verify paused workflow IDs."""
    volume_name = f"{project_name}_n8n-data"
    mounts = [
        "--mount", f"type=volume,source={volume_name},target=/home/node/.n8n",
        "--mount", f"type=bind,source={audit_directory.resolve()},target=/restore-audit",
    ]
    common = [
        "docker", "run", "--rm", "--network", "none", "--env-file", str(env_file.resolve()),
        *mounts, "--entrypoint", "n8n", "n8nio/n8n:2.5.2",
    ]
    for args in (
        ["export:credentials", "--all", "--decrypted", "--output=/dev/null"],
        ["export:workflow", "--all", "--output=/restore-audit/workflows.json"],
    ):
        try:
            result = subprocess.run(
                common + args, cwd=audit_directory, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupHostError("Pinned n8n validation runtime is unavailable or exceeded its deadline") from exc
        if result.returncode != 0:
            raise BackupHostError("Restored n8n credentials or workflows could not be validated")
    export_path = audit_directory / "workflows.json"
    try:
        if export_path.is_symlink() or not export_path.is_file() or export_path.stat().st_size > 16 * 1024 * 1024:
            raise BackupHostError("Restored n8n workflow projection exceeds its bound")
        exported = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupHostError("Restored n8n workflow projection is invalid") from exc
    if isinstance(exported, dict) and isinstance(exported.get("data"), list):
        exported = exported["data"]
    if not isinstance(exported, list) or len(exported) > 10_000:
        raise BackupHostError("Restored n8n workflow inventory exceeds its bound")
    observed: dict[str, bool] = {}
    for item in exported:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not isinstance(item.get("active"), bool) or item["id"] in observed):
            raise BackupHostError("Restored n8n workflow state is invalid")
        observed[item["id"]] = item["active"]
    expected = {
        str(item["id"]): bool(item["active"])
        for item in workflow_states
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and isinstance(item.get("active"), bool)
    }
    if len(expected) != len(workflow_states) or set(observed) != set(expected):
        raise BackupHostError("Restored n8n workflow IDs do not match the saved schedule receipt")
    if any(observed[identifier] for identifier in observed):
        raise BackupHostError("Restored n8n workflows must remain paused during isolated validation")
    return sum(expected.values())


def _verify_graph_volume(compose: Compose) -> None:
    """Open the restored FalkorDB volume with its pinned runtime and query its graph catalog."""
    compose.quiet("up", "-d", "graph")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            ping = compose.capture(
                "exec", "-T", "graph", "sh", "-c",
                'REDISCLI_AUTH="$GRAPH_PASSWORD" redis-cli ping',
            ).decode("ascii", "strict").strip()
            if ping == "PONG":
                compose.capture(
                    "exec", "-T", "graph", "sh", "-c",
                    'REDISCLI_AUTH="$GRAPH_PASSWORD" redis-cli --raw GRAPH.LIST',
                )
                return
        except (BackupHostError, UnicodeDecodeError):
            time.sleep(2)
    raise BackupHostError("Restored graph volume did not open under the pinned FalkorDB runtime")


def restore_backup(
    archive_path: Path, *, root: Path | None = None, keep_isolated: bool = False,
) -> dict[str, object]:
    """Restore and migrate a backup only inside a uniquely named Compose project."""
    root = (root or Path.cwd()).resolve()
    settings = Settings(_env_file=root / ".env")
    age, recipient, identity = _age_tools(settings, root)
    archive_path = archive_path.expanduser()
    if not archive_path.is_absolute():
        archive_path = root / archive_path
    archive_path = archive_path.resolve(strict=True)
    if archive_path.is_symlink() or not archive_path.is_file():
        raise BackupHostError("Backup archive must be an existing regular file")
    project_name = f"bbd-restore-{uuid4().hex[:16]}"
    succeeded = False
    retained_staging: Path | None = None
    with tempfile.TemporaryDirectory(prefix="bbd-os-restore-") as temporary:
        stage = Path(temporary) / "snapshot"
        stage.mkdir(mode=0o700)
        manifest = _decrypt_snapshot(age, identity, archive_path, stage)
        component_statuses = _verify_manifest_keys(manifest, stage, recipient)
        archived_env = stage / "configuration" / "runtime.env"
        archived_configuration = dotenv_values(archived_env, interpolate=False)
        archived_database_value = archived_configuration.get("DATABASE_URL")
        archived_postgres_password = archived_configuration.get("POSTGRES_PASSWORD")
        try:
            database_url = (
                make_url(archived_database_value)
                if isinstance(archived_database_value, str) else None
            )
        except (ValueError, TypeError) as exc:
            raise BackupHostError("Archived database configuration is invalid") from exc
        if (database_url is None or database_url.host != "postgres"
                or database_url.username != "bbd" or database_url.database != "bbd"
                or not archived_postgres_password
                or database_url.password != archived_postgres_password):
            raise BackupHostError("Archived database credentials do not match the fixed restore identity")
        if keep_isolated:
            retained_staging = _retained_restore_directory(root, project_name)
            retained_env = retained_staging / "runtime.env"
            retained_override = retained_staging / "compose.override.yml"
            try:
                shutil.copyfile(archived_env, retained_env)
                _secure_retained_path(retained_env, directory=False)
                _write_restore_override(retained_override, retained_env)
                _secure_retained_path(retained_override, directory=False)
            except BaseException:
                _discard_retained_restore_directory(root, project_name)
                raise
            compose_env = retained_env
            compose_override = retained_override
        else:
            compose_env = archived_env
            compose_override = Path(temporary) / "restore-compose.override.yml"
            _write_restore_override(compose_override, compose_env)
        compose = Compose(
            root, settings, project_name=project_name, include_connectors=False,
            env_file=compose_env, extra_compose_file=compose_override,
        )
        postgres_component = next(
            (component for component in manifest.components if component.name == "postgres"), None,
        )
        if postgres_component is None:
            if retained_staging is not None:
                _discard_retained_restore_directory(root, project_name)
            raise BackupHostError("Backup PostgreSQL schema manifest is unavailable")
        versions = set(postgres_component.schema_version.split(","))
        postgres_dump = stage / "postgres" / "database.dump"
        if not postgres_dump.is_file() or postgres_dump.is_symlink():
            if retained_staging is not None:
                _discard_retained_restore_directory(root, project_name)
            raise BackupHostError("Backup PostgreSQL dump is unavailable")
        try:
            compose.quiet("build", "api", "backup-volume-restore")
            if not versions or "" in versions or not versions.issubset(_known_migrations(compose)):
                raise BackupHostError("Backup database schema is unknown to this application revision")
            compose.quiet("up", "-d", "postgres")
            _wait_for_postgres(compose)
            compose.feed_file(
                postgres_dump, "exec", "-T", "postgres", "pg_restore",
                "--exit-on-error", "--clean", "--if-exists", "--no-owner", "--no-privileges",
                "--username", "bbd", "--dbname", "bbd",
            )
            if _database_migrations(compose) != versions:
                raise BackupHostError("Restored database revision does not match the manifest")
            compose.quiet(
                "run", "--rm", "--no-deps", "-T", "api", "alembic", "upgrade", "head",
            )
            heads = set(re.findall(
                r"(?m)^\s*([A-Za-z0-9_-]+)\s+\(head\)",
                compose.capture("run", "--rm", "--no-deps", "-T", "api", "alembic", "heads")
                .decode("utf-8", "strict"),
            ))
            if not heads or _database_migrations(compose) != heads:
                raise BackupHostError("Isolated database did not reach every application migration head")
            _normalize_archived_control(compose)
            # Fence all owner mutations before starting API and worker readiness checks.
            # The override's internal network also prevents outbound provider/effect calls.
            isolated_operation = compose.coordinator(
                "begin", "--coordinator-id", f"restore-validation-{project_name}",
            )
            isolated_operation_id = isolated_operation.get("id")
            if not isinstance(isolated_operation_id, str):
                raise BackupHostError("Isolated restore could not establish its validation fence")
            compose.coordinator(
                "transition", "--operation-id", isolated_operation_id,
                "--expected", "draining", "--phase", "quiesced",
            )
            for component, volume in (("raw_files", "raw_files"), ("graph", "graph"), ("n8n", "n8n")):
                if component_statuses.get(component) != "complete":
                    continue
                volume_archive = stage / component / "volume.tar.gz"
                if not volume_archive.is_file() or volume_archive.is_symlink():
                    raise BackupHostError("Backup volume component is incomplete")
                with volume_archive.open("rb") as source:
                    compose.import_volume(volume, source)
            verification: dict[str, str] = {
                "postgres": "migrated",
                "raw_files": "restored",
                "api": "not_started",
                "worker": "not_started",
                "graph": "not_configured",
                "n8n_credentials": "not_configured",
                "n8n_workflows": "not_configured",
                "n8n_schedules": "not_configured",
            }
            if component_statuses.get("graph") == "complete":
                _verify_graph_volume(compose)
                verification["graph"] = "opened_and_queried"
            if component_statuses.get("n8n") == "complete":
                workflow_state_path = stage / "n8n" / "workflow_state.json"
                workflow_states = json.loads(workflow_state_path.read_text(encoding="utf-8"))
                validation_env = Path(temporary) / "n8n-validation.env"
                _write_n8n_validation_env(validation_env, archived_env)
                expected_active = _run_n8n_validation(
                    project_name, validation_env, Path(temporary), workflow_states,
                )
                verification["n8n_credentials"] = "decryption_verified"
                verification["n8n_workflows"] = "inventory_verified"
                verification["n8n_schedules"] = f"{expected_active}_left_paused_for_review"
            compose.quiet("up", "-d", "api", "worker")
            _verify_services_running(compose, {"postgres", "redis", "api", "worker"})
            _wait_for_api_ready(compose)
            _wait_for_worker_ready(compose)
            verification["api"] = "ready"
            verification["worker"] = "fresh_heartbeat_under_quiesced_fence"
            succeeded = True
        finally:
            if not keep_isolated or not succeeded:
                try:
                    compose.quiet("down", "--volumes", "--remove-orphans")
                except BackupHostError:
                    raise
                if retained_staging is not None and not succeeded:
                    _discard_retained_restore_directory(root, project_name)
    return {
        "project_name": project_name,
        "components": component_statuses,
        "schema_versions": sorted(versions),
        "isolated_project_kept": keep_isolated,
        "retained_configuration": keep_isolated,
        "verification": verification,
        # Restore remains a fenced validation project with schedules left paused.
        # Report component results above; never label this operator-reviewed state complete.
        "fully_restored": False,
    }


def create_backup(output: Path, *, root: Path | None = None, drain_timeout: int = 600) -> dict[str, object]:
    """Pause configured n8n schedules, drain durable activity, snapshot fixed components, and resume."""
    root = (root or Path.cwd()).resolve()
    settings = Settings(_env_file=root / ".env")
    age, recipient, _identity = _age_tools(settings, root)
    output = output.expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    compose = Compose(root, settings)
    running = compose.service_names()
    required = {"postgres", "api", "worker"}
    if not required.issubset(running):
        raise BackupHostError("PostgreSQL, API, and worker must already be running")
    graph_enabled = settings.graph_enabled
    n8n_configured = bool(settings.n8n_api_key.get_secret_value())
    if graph_enabled and "graph" not in running:
        raise BackupHostError("Enabled FalkorDB profile is not running")
    if n8n_configured and "n8n" not in running:
        raise BackupHostError("Configured n8n connector service is not running")
    if "n8n" in running and not n8n_configured:
        raise BackupHostError("Running n8n cannot be drained without its configured API key")

    schema_versions = compose.coordinator("status").get("schema_versions")
    if not isinstance(schema_versions, list) or not schema_versions:
        raise BackupHostError("Installed database schema version is unavailable")
    schema_version = ",".join(str(item) for item in schema_versions)
    coordinator_id = f"host:{uuid4().hex}"
    operation = compose.coordinator("begin", "--coordinator-id", coordinator_id)
    operation_id = str(operation["id"])
    phase = "draining"
    stopped: list[str] = []
    schedule_gate: N8nScheduleGate | None = None
    schedule_state: list[dict[str, object]] = []
    services_recovered = True
    recovery_blocked = False
    admission_released = False
    operation_finished = False
    try:
        if n8n_configured:
            schedule_gate = _build_schedule_gate(compose, settings, operation_id, drain_timeout)
            schedule_state = schedule_gate.pause_and_drain()
            _record_receipt(compose, operation_id, phase, "n8n_drain", {
                "status": "complete", "files": len(schedule_state),
                "components": {"n8n_schedules": "complete"},
            })
        deadline = time.monotonic() + drain_timeout
        while True:
            status = compose.coordinator("status")
            active = int(status.get("active_activities", -1))
            if active == 0:
                break
            if time.monotonic() >= deadline:
                raise BackupHostError("Admitted owner activities did not drain before the deadline")
            time.sleep(3)
        _publish_phase(compose, operation_id, "draining", "quiesced")
        phase = "quiesced"
        _publish_phase(compose, operation_id, phase, "snapshotting")
        phase = "snapshotting"
        # Keep PostgreSQL and effect-bearing services available until their
        # respective snapshot boundary. API/worker are the durable-write fence:
        # admitted jobs have drained, so no new owner writes can begin.
        for service in ("worker", "api"):
            recovery_blocked = True
            try:
                _stop_service(compose, running, stopped, service)
            except BaseException:
                _record_stage_failure(
                    compose, operation_id, phase, f"{service}_stop", f"{service}_stop_failed",
                )
                raise
            else:
                _record_receipt(compose, operation_id, phase, f"{service}_stop", {
                    "status": "complete", "components": {service: "complete"},
                })

        with tempfile.TemporaryDirectory(prefix="bbd-os-backup-") as temporary:
            stage = Path(temporary)
            configured_components = ["postgres", "raw_files", "configuration"]
            if graph_enabled:
                configured_components.append("graph")
            if n8n_configured:
                configured_components.append("n8n")
            for component in configured_components:
                (stage / component).mkdir(mode=0o700)
            try:
                _copy_configuration(root, stage)
            except BaseException:
                _record_stage_failure(
                    compose, operation_id, phase, "configuration_copy", "configuration_copy_failed",
                )
                raise
            database_url = make_url(settings.database_url)
            dump_path = stage / "postgres" / "database.dump"
            try:
                compose.stream_to_file(
                    dump_path, "exec", "-T", "postgres", "pg_dump",
                    "--format=custom", "--no-owner", "--no-privileges",
                    "--username", database_url.username or "bbd",
                    "--dbname", database_url.database or "bbd",
                )
            except BaseException:
                _record_stage_failure(compose, operation_id, phase, "postgres_dump", "postgres_dump_failed")
                raise
            _record_receipt(compose, operation_id, phase, "postgres_dump", {
                "status": "complete", "bytes": dump_path.stat().st_size,
                "components": {"postgres": "complete"},
            })
            try:
                compose.export_volume("raw_files", stage / "raw_files" / "volume.tar.gz")
            except BaseException:
                _record_stage_failure(compose, operation_id, phase, "raw_files_copy", "raw_files_copy_failed")
                raise
            _record_receipt(compose, operation_id, phase, "raw_files_copy", {
                "status": "complete", "bytes": (stage / "raw_files" / "volume.tar.gz").stat().st_size,
                "components": {"raw_files": "complete"},
            })
            if graph_enabled:
                try:
                    result = compose.capture(
                        "exec", "-T", "graph", "sh", "-c",
                        'REDISCLI_AUTH="$GRAPH_PASSWORD" redis-cli SAVE',
                    ).decode("utf-8", "strict").strip()
                    if result != "OK":
                        raise BackupHostError("FalkorDB persistence snapshot failed")
                except BaseException:
                    _record_stage_failure(compose, operation_id, phase, "graph_save", "graph_save_failed")
                    raise
                _record_receipt(compose, operation_id, phase, "graph_save", {
                    "status": "complete", "components": {"graph": "complete"},
                })
                recovery_blocked = True
                try:
                    _stop_service(compose, running, stopped, "graph")
                except BaseException:
                    _record_stage_failure(compose, operation_id, phase, "graph_stop", "graph_stop_failed")
                    raise
                _record_receipt(compose, operation_id, phase, "graph_stop", {
                    "status": "complete", "components": {"graph": "complete"},
                })
                try:
                    compose.export_volume("graph", stage / "graph" / "volume.tar.gz")
                except BaseException:
                    _record_stage_failure(compose, operation_id, phase, "graph_copy", "graph_copy_failed")
                    raise
                _record_receipt(compose, operation_id, phase, "graph_copy", {
                    "status": "complete", "bytes": (stage / "graph" / "volume.tar.gz").stat().st_size,
                    "components": {"graph": "complete"},
                })
            if n8n_configured:
                try:
                    live_key = _prepare_n8n_key(compose, settings, stage)
                except BaseException:
                    _record_stage_failure(compose, operation_id, phase, "n8n_key", "n8n_key_unverified")
                    raise
                (stage / "n8n" / "workflow_state.json").write_text(
                    json.dumps(schedule_state, separators=(",", ":")), encoding="utf-8",
                )
                recovery_blocked = True
                try:
                    _stop_service(compose, running, stopped, "n8n")
                except BaseException:
                    _record_stage_failure(compose, operation_id, phase, "n8n_stop", "n8n_stop_failed")
                    raise
                _record_receipt(compose, operation_id, phase, "n8n_stop", {
                    "status": "complete", "components": {"n8n": "complete"},
                })
                try:
                    compose.export_volume("n8n", stage / "n8n" / "volume.tar.gz")
                except BaseException:
                    _record_stage_failure(compose, operation_id, phase, "n8n_copy", "n8n_copy_failed")
                    raise
                _record_receipt(compose, operation_id, phase, "n8n_copy", {
                    "status": "complete", "bytes": (stage / "n8n" / "volume.tar.gz").stat().st_size,
                    "components": {"n8n": "complete"},
                })
                required_keys = {"AGE_RECIPIENT": recipient, "N8N_ENCRYPTION_KEY": live_key}
            else:
            required_keys = {"AGE_RECIPIENT": recipient}
            archived_configuration = dotenv_values(stage / "configuration" / "runtime.env", interpolate=False)
            archive_postgres_password = archived_configuration.get("POSTGRES_PASSWORD")
            archive_database_url = archived_configuration.get("DATABASE_URL")
            try:
                archived_db = make_url(archive_database_url) if isinstance(archive_database_url, str) else None
            except (ValueError, TypeError) as exc:
                raise BackupHostError("Deployment database configuration is invalid") from exc
            if (not archive_postgres_password or archived_db is None
                    or archived_db.password != archive_postgres_password):
                raise BackupHostError("Deployment PostgreSQL credentials are inconsistent")
            required_keys["POSTGRES_PASSWORD"] = archive_postgres_password
            for purpose in (
                "AI_CREDENTIAL_ENCRYPTION_KEY", "CONNECTOR_CREDENTIAL_ENCRYPTION_KEY",
                "GRAPH_PASSWORD", "N8N_ENCRYPTION_KEY",
            ):
                value = archived_configuration.get(purpose)
                if value:
                    required_keys[purpose] = value
            if graph_enabled and not required_keys.get("GRAPH_PASSWORD"):
                raise BackupHostError("Enabled graph profile has no protected database key")
            if n8n_configured and required_keys.get("N8N_ENCRYPTION_KEY") != live_key:
                raise BackupHostError("Archived n8n key does not match the verified live key")
            component_specs = {
                "postgres": ("postgres", schema_version),
                "raw_files": ("raw_files", "raw-files-v1"),
                "graph": ("graph", "falkordb-rdb-volume-v1"),
                "n8n": ("n8n", "n8n-2.5.2"),
                "configuration": ("configuration", "compose-env-v1"),
            }
            manifest = build_manifest(
                stage, components=component_specs,
                required_key_references=required_keys,
                consistency_method="transactional-admission-barrier+service-stop+postgres-logical-dump+falkordb-save",
            )
            statuses = {item.name: item.status for item in manifest.components}
            if statuses.get("postgres") != "complete" or statuses.get("raw_files") != "complete":
                raise BackupHostError("Required PostgreSQL or raw-file component is missing")
            _record_receipt(compose, operation_id, phase, "snapshot", {
                "status": "complete", "files": sum(len(item.files) for item in manifest.components),
                "bytes": sum(file.size for item in manifest.components for file in item.files),
                "components": statuses,
            })
            try:
                _encrypt_age(age, recipient, output, stage, manifest)
            except BaseException:
                _record_stage_failure(compose, operation_id, phase, "archive", "archive_encryption_failed")
                raise
            archive_bytes = output.stat().st_size
            _record_receipt(compose, operation_id, phase, "archive", {
                "status": "complete", "bytes": archive_bytes, "schema_version": schema_version,
                "archive_name": output.name,
            })
        _publish_phase(compose, operation_id, phase, "resuming")
        phase = "resuming"
        _start_services(compose, stopped)
        if schedule_gate is not None:
            ready_services = required | ({"graph"} if graph_enabled else set()) | {"n8n"}
            _record_ready_services(
                compose, operation_id, phase, ready_services,
            )
            compose.coordinator("release", "--operation-id", operation_id)
            admission_released = True
            operation = _load_operation(compose, operation_id)
            activation_failures = schedule_gate.resume(_load_workflow_states(operation), operation)
            if activation_failures:
                compose.coordinator("recovery-required", "--operation-id", operation_id)
                operation_finished = True
                raise BackupHostError("n8n schedule recovery is incomplete; retry the stored operation")
        compose.coordinator(
            "resume", "--operation-id", operation_id, "--outcome", "completed",
            "--archive-name", output.name,
        )
        operation_finished = True
        return {
            "operation_id": operation_id,
            "archive_name": output.name,
            "archive_bytes": archive_bytes,
            "components": statuses,
        }
    except BaseException:
        if admission_released and not operation_finished:
            try:
                operation = _load_operation(compose, operation_id)
                if operation.get("status") in {"resuming", "failed_recovery_required"}:
                    compose.coordinator("recovery-required", "--operation-id", operation_id)
                operation_finished = True
            except BaseException:
                # Admission may already be open. Keep the durable operation ID so
                # a later operator can discover and reconcile it explicitly.
                pass
        elif not recovery_blocked:
            try:
                if schedule_gate is not None:
                    operation = _load_operation(compose, operation_id)
                    receipts = operation.get("stage_receipts")
                    snapshot_exists = isinstance(receipts, dict) and "n8n_workflow_states" in receipts
                    if snapshot_exists:
                        workflow_states = _load_workflow_states(operation)
                        schedule_gate.pause_saved_and_drain(workflow_states, str(operation["status"]))
                    if phase != "resuming":
                        _publish_phase(compose, operation_id, phase, "resuming")
                        phase = "resuming"
                    ready_services = required | ({"graph"} if graph_enabled else set()) | {"n8n"}
                    _record_ready_services(
                        compose, operation_id, phase, ready_services,
                    )
                    compose.coordinator("release", "--operation-id", operation_id)
                    admission_released = True
                    if snapshot_exists:
                        operation = _load_operation(compose, operation_id)
                        activation_failures = schedule_gate.resume(
                            _load_workflow_states(operation), operation,
                        )
                        if activation_failures:
                            compose.coordinator("recovery-required", "--operation-id", operation_id)
                            operation_finished = True
                            services_recovered = False
                else:
                    _start_services(compose, stopped)
                    if phase != "resuming":
                        _publish_phase(compose, operation_id, phase, "resuming")
                        phase = "resuming"
                    compose.coordinator(
                        "resume", "--operation-id", operation_id, "--outcome", "incomplete",
                    )
                    operation_finished = True
            except BaseException:
                services_recovered = False
        else:
            services_recovered = False
        if not operation_finished:
            try:
                if services_recovered:
                    if phase != "resuming":
                        _publish_phase(compose, operation_id, phase, "resuming")
                    compose.coordinator(
                        "resume", "--operation-id", operation_id, "--outcome", "incomplete",
                    )
                else:
                    _publish_phase(compose, operation_id, phase, "failed_recovery_required")
            except BaseException:
                services_recovered = False
        raise
    finally:
        if schedule_gate is not None:
            schedule_gate.close()


def recover_operation(
    operation_id: str, *, root: Path | None = None, archive_path: Path | None = None,
) -> dict[str, object]:
    """Explicitly restart services and reconcile one failed backup operation by its receipts."""
    root = (root or Path.cwd()).resolve()
    settings = Settings(_env_file=root / ".env")
    compose = Compose(root, settings)
    operation = _load_operation(compose, operation_id)
    if operation.get("status") != "failed_recovery_required":
        raise BackupHostError("Only a recovery-required operation can be resumed")
    receipts = operation.get("stage_receipts")
    if not isinstance(receipts, dict):
        raise BackupHostError("Backup operation receipts are unavailable")
    workflow_snapshot = receipts.get("n8n_workflow_states")
    workflow_states = _load_workflow_states(operation) if isinstance(workflow_snapshot, dict) else None
    if workflow_states is not None and not settings.n8n_api_key.get_secret_value():
        raise BackupHostError("Configure the original n8n API key before retrying schedule recovery")
    control = compose.coordinator("status")
    if control.get("operation_id") != operation_id:
        raise BackupHostError("Backup operation no longer owns maintenance state")

    archive_receipt = receipts.get("archive")
    archive_name = archive_receipt.get("archive_name") if isinstance(archive_receipt, dict) else None
    backup_complete = bool(
        isinstance(archive_receipt, dict) and archive_receipt.get("status") == "complete"
        and archive_path is not None and archive_path.expanduser().name == archive_name
        and archive_path.expanduser().is_file() and not archive_path.expanduser().is_symlink()
    )
    graph_enabled = settings.graph_enabled
    n8n_configured = bool(settings.n8n_api_key.get_secret_value())
    required = {"postgres", "api", "worker"}
    services = required | ({"graph"} if graph_enabled else set()) | ({"n8n"} if n8n_configured else set())
    if control.get("phase") == "failed_recovery_required":
        running = compose.service_names()
        for service in ("graph", "api", "worker", "n8n"):
            if service in services and service not in running:
                compose.quiet("start", service)
        _verify_services_running(compose, services)
        _wait_for_api_ready(compose)
        _wait_for_worker_ready(compose)
        if graph_enabled:
            _verify_graph_volume(compose)
        if workflow_states is not None:
            _verify_live_n8n_key(compose, settings)
            gate = _build_schedule_gate(compose, settings, operation_id, timeout_seconds=60)
            try:
                gate.pause_saved_and_drain(workflow_states, "failed_recovery_required")
            finally:
                gate.close()
        if "core_services" not in receipts:
            _record_ready_services(compose, operation_id, "failed_recovery_required", services)
        compose.coordinator(
            "transition", "--operation-id", operation_id,
            "--expected", "failed_recovery_required", "--phase", "resuming",
        )
        operation = _load_operation(compose, operation_id)
        control = compose.coordinator("status")
    elif control.get("phase") != "idle":
        raise BackupHostError("Backup operation is not in a resumable recovery state")

    if not services.issubset(compose.service_names()):
        raise BackupHostError("Core service readiness failed during recovery")
    _wait_for_api_ready(compose)
    _wait_for_worker_ready(compose)
    if graph_enabled and control.get("phase") == "idle":
        _verify_graph_volume(compose)
    if workflow_states is not None and control.get("phase") == "idle":
        _verify_live_n8n_key(compose, settings)
    if control.get("phase") == "resuming":
        compose.coordinator("release", "--operation-id", operation_id)
    operation = _load_operation(compose, operation_id)
    if workflow_states is not None:
        gate = _build_schedule_gate(compose, settings, operation_id, timeout_seconds=60)
        try:
            failures = gate.resume(_load_workflow_states(operation), operation)
        finally:
            gate.close()
        if failures:
            compose.coordinator("recovery-required", "--operation-id", operation_id)
            raise BackupHostError("Some n8n schedules remain uncertain; retry this operation")
    compose.coordinator(
        "resume", "--operation-id", operation_id,
        "--outcome", "completed" if backup_complete else "incomplete",
        *( ["--archive-name", str(archive_name)] if backup_complete else [] ),
    )
    return {
        "operation_id": operation_id,
        "outcome": "completed" if backup_complete else "incomplete",
        "archive_name": archive_name if backup_complete else None,
    }
