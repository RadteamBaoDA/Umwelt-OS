"""Operate explicitly named, disposable local Compose workspaces with a confirmed reset fence."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import quote

if os.name == "nt":
    import msvcrt
else:
    import fcntl

REPOSITORY = Path(__file__).resolve().parents[1]
WORKSPACE_PATTERN = re.compile(r"^bbd-os-ws-[0-9a-f]{12}$")


def _state_directory() -> Path:
    """Return the current user's private local state directory across supported host OSes."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise RuntimeError("LOCALAPPDATA is required to register a reset-safe workspace")
        return Path(root) / "Umwelt-OS" / "workspaces"
    root = os.environ.get("XDG_STATE_HOME")
    return Path(root) / "umwelt-os" / "workspaces" if root else Path.home() / ".local" / "state" / "umwelt-os" / "workspaces"


def _project_name(value: str) -> str:
    """Validate the exact unique project namespace that reset is allowed to remove."""
    if not WORKSPACE_PATTERN.fullmatch(value):
        raise ValueError("workspace must be bbd-os-ws- followed by exactly 12 lowercase hexadecimal characters")
    return value


def _compose(project: str, *arguments: str, development: bool = True) -> list[str]:
    """Build an argument vector for this checkout's fixed Compose files and project name."""
    command = ["docker", "compose", "-p", project, "-f", "docker-compose.yml"]
    if development:
        command.extend(("-f", "docker-compose.dev.yml"))
    return command + list(arguments)


def _fingerprint(project: str, web_port: int) -> str:
    """Hash the resolved configuration in memory without writing or displaying its environment values."""
    try:
        environment = _workspace_environment(project, web_port)
        result = subprocess.run(
            _compose(project, "config", "--format", "json"),
            cwd=REPOSITORY, check=True, capture_output=True, text=True, env=environment,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("could not resolve the named workspace Compose configuration") from exc
    material = f"{REPOSITORY}\n{project}\n{result.stdout}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _workspace_environment(project: str, web_port: int) -> dict[str, str]:
    """Resolve the local Postgres password and force application state URLs onto this Compose project."""
    environment = os.environ.copy()
    environment["WEB_PORT"] = str(web_port)
    environment.pop("DATABASE_URL", None)
    environment.pop("REDIS_URL", None)
    try:
        result = subprocess.run(
            _compose(project, "config", "--format", "json"), cwd=REPOSITORY,
            check=True, capture_output=True, text=True, env=environment,
        )
        config = json.loads(result.stdout)
        postgres_environment = config["services"]["postgres"]["environment"]
        password = postgres_environment["POSTGRES_PASSWORD"]
        if not isinstance(password, str) or not password:
            raise ValueError("missing local Postgres password")
    except (OSError, subprocess.CalledProcessError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not resolve named workspace's local database configuration") from exc
    environment["DATABASE_URL"] = f"postgresql+asyncpg://bbd:{quote(password, safe='')}@postgres:5432/bbd"
    environment["REDIS_URL"] = "redis://redis:6379/0"
    return environment


def _daemon_identity() -> str:
    """Return the Docker daemon ID that fences a registration from other contexts or daemon replacements."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ID}}"], cwd=REPOSITORY,
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("could not identify Docker daemon; refusing workspace operation") from exc
    identity = result.stdout.strip()
    if not identity:
        raise RuntimeError("Docker daemon identity is unavailable; refusing workspace operation")
    return identity


def _existing_project_resources(project: str) -> bool:
    """Detect project-labeled containers, volumes, or networks before creating local ownership."""
    queries = (
        (["docker", "ps", "-a", "--format", "{{json .}}"], f"{project}-"),
        (["docker", "volume", "ls", "--format", "{{json .}}"], f"{project}_"),
        (["docker", "network", "ls", "--format", "{{json .}}"], f"{project}_"),
    )
    for command, name_prefix in queries:
        try:
            result = subprocess.run(command, cwd=REPOSITORY, check=True, capture_output=True, text=True)
            resources = [json.loads(line) for line in result.stdout.splitlines()]
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("could not check existing Docker resources; refusing workspace registration") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("could not parse existing Docker resources; refusing workspace registration") from exc
        for resource in resources:
            name = resource.get("Names", resource.get("Name", ""))
            labels = resource.get("Labels", "")
            if isinstance(name, str) and name.startswith(name_prefix):
                return True
            if isinstance(labels, str) and f"com.docker.compose.project={project}" in labels.split(","):
                return True
    return False


def _marker_path(project: str) -> Path:
    """Return the exact per-user registration file for a validated Compose project."""
    return _state_directory() / f"{project}.json"


@contextmanager
def _workspace_lock(project: str):
    """Serialize each named workspace command across processes until all marker and Compose work is finished."""
    directory = _state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        lock_file = (directory / f"{project}.lock").open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
    except OSError as exc:
        raise RuntimeError("could not prepare named workspace operation lock") from exc
    acquired = False
    try:
        while not acquired:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    lock_file.close()
                    raise RuntimeError("could not lock named workspace operation") from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _read_registration(project: str) -> dict[str, object]:
    """Read and validate this checkout's registration before verifying its live configuration."""
    marker = _marker_path(project)
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("workspace is not registered by this checkout; refusing operation") from exc
    if not isinstance(value, dict):
        raise RuntimeError("workspace registration is invalid; refusing operation")
    if value.get("project") != project or value.get("repository") != str(REPOSITORY):
        raise RuntimeError("workspace registration does not belong to this checkout; refusing operation")
    if value.get("daemon_id") != _daemon_identity():
        raise RuntimeError("workspace registration belongs to a different Docker daemon; refusing operation")
    port = value.get("web_port")
    if type(port) is not int or not 1024 <= port <= 65535 or value.get("fingerprint") != _fingerprint(project, port):
        raise RuntimeError("workspace registration or Compose fingerprint changed; refusing operation")
    value["marker_path"] = marker
    return value


def _free_web_port() -> int:
    """Choose an available loopback port so separate workspace web services do not collide."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _register(project: str) -> dict[str, object]:
    """Exclusively register an unused project after fencing its daemon and checking for existing resources."""
    directory = _state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    marker = _marker_path(project)
    if marker.exists():
        return _read_registration(project)
    port = _free_web_port()
    fingerprint = _fingerprint(project, port)
    expected = {"project": project, "repository": str(REPOSITORY), "web_port": port, "fingerprint": fingerprint}
    expected["daemon_id"] = _daemon_identity()
    if _existing_project_resources(project):
        raise RuntimeError("Docker project resources already exist without this checkout's registration; refusing adoption")
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(expected, stream, separators=(",", ":"))
    except FileExistsError:
        return _read_registration(project)
    except OSError as exc:
        raise RuntimeError("could not create exclusive workspace registration") from exc
    try:
        if _existing_project_resources(project):
            raise RuntimeError("Docker project resources appeared during registration; refusing adoption")
    except RuntimeError:
        marker.unlink(missing_ok=True)
        raise
    expected["marker_path"] = marker
    return expected


def _run(project: str, arguments: list[str], web_port: int) -> None:
    """Run one Compose operation with application state URLs pinned to the named project's services."""
    try:
        environment = _workspace_environment(project, web_port)
        subprocess.run(arguments, cwd=REPOSITORY, check=True, env=environment)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Compose operation failed: {arguments[2] if len(arguments) > 2 else 'docker'}") from exc


def _verify_target(project: str, web_port: int) -> None:
    """Verify target containers, state volumes, and default network belong to this registered checkout."""
    command = _compose(project, "ps", "--all", "-q")
    try:
        environment = _workspace_environment(project, web_port)
        result = subprocess.run(command, cwd=REPOSITORY, check=True, capture_output=True, text=True, env=environment)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("could not inspect named workspace; refusing reset") from exc
    expected_configs = {
        str((REPOSITORY / "docker-compose.yml").resolve()).casefold(),
        str((REPOSITORY / "docker-compose.dev.yml").resolve()).casefold(),
    }
    for container_id in result.stdout.splitlines():
        try:
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
                check=True, capture_output=True, text=True,
            )
            labels = json.loads(inspected.stdout)
            config_label = labels.get("com.docker.compose.project.config_files") if isinstance(labels, dict) else None
            if not isinstance(config_label, str):
                raise ValueError("Compose config label is unavailable")
            config_files = {
                str(Path(value).resolve()).casefold()
                for value in config_label.split(",")
            }
            working_dir = str(Path(labels["com.docker.compose.project.working_dir"]).resolve())
        except (OSError, subprocess.CalledProcessError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("could not verify named workspace container labels; refusing reset") from exc
        if (
            labels.get("com.docker.compose.project") != project
            or working_dir.casefold() != str(REPOSITORY).casefold()
            or config_files != expected_configs
        ):
            raise RuntimeError("container ownership does not match the registered Compose workspace; refusing reset")
    try:
        volumes = subprocess.run(
            ["docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Name}}"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("could not inspect named workspace volumes; refusing reset") from exc
    expected_volumes = {"app-data", "postgres-data", "redis-data", "web-node-modules", "web-app-node-modules"}
    for name in volumes.stdout.splitlines():
        if not name.startswith(f"{project}_") or name[len(project) + 1:] not in expected_volumes:
            raise RuntimeError("named project contains an unexpected volume; refusing reset")
        try:
            inspected = subprocess.run(
                ["docker", "volume", "inspect", "--format", "{{json .Labels}}", name],
                check=True, capture_output=True, text=True,
            )
            labels = json.loads(inspected.stdout)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise RuntimeError("could not verify named workspace volume labels; refusing reset") from exc
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project:
            raise RuntimeError("volume ownership does not match the registered Compose workspace; refusing reset")
    if not result.stdout.splitlines() and not volumes.stdout.splitlines():
        raise RuntimeError("named workspace has no Compose resources; refusing a successful no-op reset")
    try:
        networks = subprocess.run(
            ["docker", "network", "ls", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Name}}"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("could not inspect named workspace networks; refusing reset") from exc
    if not networks.stdout.splitlines() or any(name != f"{project}_default" for name in networks.stdout.splitlines()):
        raise RuntimeError("named project contains an unexpected or missing network; refusing reset")
    for name in networks.stdout.splitlines():
        try:
            inspected = subprocess.run(
                ["docker", "network", "inspect", "--format", "{{json .Labels}}", name],
                check=True, capture_output=True, text=True,
            )
            labels = json.loads(inspected.stdout)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise RuntimeError("could not verify named workspace network labels; refusing reset") from exc
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project:
            raise RuntimeError("network ownership does not match the registered Compose workspace; refusing reset")


def _workspace_up(project: str) -> None:
    """Register and start one uniquely named development workspace using this checkout's Compose files."""
    registration = _register(project)
    port = int(registration["web_port"])
    _run(project, _compose(project, "up", "-d", "--build"), port)
    print(f"Workspace {project} is available at http://localhost:{port}")


def _workspace_stop(project: str) -> None:
    """Stop a registered workspace without deleting its containers or named volumes."""
    registration = _read_registration(project)
    _run(project, _compose(project, "stop"), int(registration["web_port"]))


def _workspace_seed(project: str) -> None:
    """Run the explicit fictional seed inside the registered workspace's own database project."""
    registration = _read_registration(project)
    _run(
        project,
        _compose(project, "run", "--rm", "--build", "api", "python", "-m", "modules.knowledge.documents.seed", development=False),
        int(registration["web_port"]),
    )


def _reset_preview(project: str) -> None:
    """Print the exact current fingerprint required by the destructive reset command."""
    registration = _read_registration(project)
    print(f"Workspace: {project}")
    print(f"Web port: {registration['web_port']}")
    print(f"Confirmation fingerprint: {registration['fingerprint']}")
    print("Review this named development workspace before confirming its volume reset.")


def _reset(project: str, confirmation: str) -> None:
    """Delete only registered workspace resources after exact fingerprint confirmation."""
    registration = _read_registration(project)
    if confirmation != registration["fingerprint"]:
        raise RuntimeError("confirmation fingerprint does not match; refusing reset")
    port = int(registration["web_port"])
    _verify_target(project, port)
    # Named project, local registration, unchanged config, verified labels, and exact token fence this volume deletion.
    _run(project, _compose(project, "down", "--volumes", "--remove-orphans"), port)
    Path(registration["marker_path"]).unlink()


def main() -> int:
    """Parse a bounded workspace command, serialize it by project, and report failures without exposing config."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "stop", "seed", "reset-preview", "reset"))
    parser.add_argument("--name", required=True, help="unique project name bbd-os-ws-<12 lowercase hex>")
    parser.add_argument("--confirm", help="exact fingerprint printed by reset-preview; required by reset")
    args = parser.parse_args()
    try:
        project = _project_name(args.name)
        with _workspace_lock(project):
            if args.action == "up":
                _workspace_up(project)
            elif args.action == "stop":
                _workspace_stop(project)
            elif args.action == "seed":
                _workspace_seed(project)
            elif args.action == "reset-preview":
                _reset_preview(project)
            else:
                if not args.confirm:
                    raise ValueError("reset requires --confirm with the exact reset-preview fingerprint")
                _reset(project, args.confirm)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
