"""Bounded POSIX stdio transport for administrator-reviewed MCP deployments."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
from typing import Literal

import anyio
from anyio.abc import (
    ByteReceiveStream,
    ByteSendStream,
    ObjectReceiveStream,
    ObjectSendStream,
    Process,
)
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from modules.tools.mcp_transport import McpTransportError

if os.name == "posix":
    from mcp.os.posix.utilities import terminate_posix_process_tree
else:
    terminate_posix_process_tree = None

_OPERATION_SECONDS = 60.0
_CLEANUP_RESERVE_SECONDS = 8.0
_SPAWN_SECONDS = 5.0
_ORDINARY_FRAME_BYTES = 256 * 1024
_ORDINARY_TOTAL_BYTES = 512 * 1024
_ORDINARY_FRAMES = 16
_DISCOVERY_FRAME_BYTES = 1024 * 1024
_DISCOVERY_TOTAL_BYTES = 1024 * 1024
_DISCOVERY_FRAMES = 64
_OUTBOUND_FRAME_BYTES = 128 * 1024
_READ_CHUNK_BYTES = 16 * 1024
_ARTIFACT_BYTES_LIMIT = 128 * 1024 * 1024
_ARTIFACTS_BYTES_LIMIT = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StdioDeploymentProfile:
    """Hold the immutable, administrator-owned launch inputs and reviewed identity.

    ``profile_hash`` is the SHA-256 of canonical deployment policy verified by
    :func:`stdio_client_transport`, excluding the per-operation selector but
    including all quotas. ``reviewed_profile_hash`` is the separately persisted
    identity captured by the caller's current execution fence. The executable
    and, for Python or Node, the canonical entry script must appear in the
    immutable artifact manifest. Values are deployment configuration, never
    user input.
    """

    profile_id: str
    profile_hash: str
    reviewed_profile_hash: str | None
    enabled: bool
    platform: str
    executable: str
    argv: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    immutable_root: str
    artifact_sha256: Mapping[str, str]
    operation_kind: Literal["ordinary", "discovery"]
    runtime_kind: Literal["native", "python", "node"]
    entry_script: str | None


class _StdioReadPipe(ByteReceiveStream):
    """Own one nonblocking parent read FD and wake AnyIO waiters when closing.

    Reads are bounded by ``max_bytes`` and retry only readiness conditions;
    every successful read checkpoints before return so continuously ready
    stdout still yields to teardown. EOF is ``EndOfStream``. Closing notifies
    readiness waiters and closes the FD once; a blocked OS read is not
    interruptible by the async deadline.
    """

    def __init__(self, fd: int) -> None:
        """Take ownership of a nonblocking read descriptor."""
        self._fd = fd
        self._closed = False

    async def receive(self, max_bytes: int = 65536) -> bytes:
        """Read at most ``max_bytes``, waiting cooperatively when no bytes exist.

        Raises AnyIO ``EndOfStream`` on child EOF and ``ClosedResourceError``
        after local closure; other OS errors propagate to the framing worker.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self._closed:
            raise anyio.ClosedResourceError
        while True:
            if self._closed:
                raise anyio.ClosedResourceError
            try:
                data = os.read(self._fd, max_bytes)
            except BlockingIOError:
                await anyio.wait_readable(self._fd)
                await anyio.lowlevel.checkpoint()
                if self._closed:
                    raise anyio.ClosedResourceError
                continue
            except InterruptedError:
                await anyio.lowlevel.checkpoint()
                continue
            if not data:
                raise anyio.EndOfStream
            await anyio.lowlevel.checkpoint()
            if self._closed:
                raise anyio.ClosedResourceError
            return data

    async def aclose(self) -> None:
        """Notify readiness waiters and close the owned FD exactly once."""
        if self._closed:
            return
        self._closed = True
        try:
            anyio.notify_closing(self._fd)
        finally:
            os.close(self._fd)


class _StdioWritePipe(ByteSendStream):
    """Own one nonblocking parent write FD for complete bounded frame writes.

    Partial writes resume the same serialized frame; every resume checks the
    ownership flag before using the numeric FD, preventing a closed/reused
    descriptor from receiving frame bytes. Cancellation after a partial write
    leaves an uncertain external effect and must never be retried as success.
    Closing wakes any pending AnyIO readiness wait.
    """

    def __init__(self, fd: int) -> None:
        """Take ownership of a nonblocking write descriptor."""
        self._fd = fd
        self._closed = False

    async def send(self, item: bytes) -> None:
        """Write all bytes with partial-write handling and cooperative waits.

        Broken-pipe and other OS errors propagate so the transport owner can
        terminate the child and report delivery uncertainty.
        """
        if self._closed:
            raise anyio.ClosedResourceError
        offset = 0
        while offset < len(item):
            if self._closed:
                raise anyio.ClosedResourceError
            try:
                written = os.write(self._fd, item[offset:])
            except BlockingIOError:
                await anyio.wait_writable(self._fd)
                await anyio.lowlevel.checkpoint()
                if self._closed:
                    raise anyio.ClosedResourceError
                continue
            except InterruptedError:
                await anyio.lowlevel.checkpoint()
                continue
            if written <= 0:
                raise BrokenPipeError("MCP stdio pipe accepted no bytes")
            offset += written
            await anyio.lowlevel.checkpoint()

    async def aclose(self) -> None:
        """Notify readiness waiters and close the owned FD exactly once."""
        if self._closed:
            return
        self._closed = True
        try:
            anyio.notify_closing(self._fd)
        finally:
            os.close(self._fd)


class _StdioProcess(asyncio.SubprocessProtocol, Process):
    """Own a public asyncio subprocess transport without subprocess pipe wrappers.

    ``connection_made`` captures the PID before launch completion. ``abort``
    marks the owner first so a late callback kills the original process group
    and closes the public transport. The supplied stdin/stdout/stderr properties
    are ``None`` because parent pipe FDs have separate owners.
    """

    def __init__(self, *, spawn_deadline: float) -> None:
        """Create protocol state before launch so cancellation has an owner."""
        self._spawn_deadline = spawn_deadline
        self._transport: asyncio.SubprocessTransport | None = None
        self._pid: int | None = None
        self._aborted = False
        self._signal_failed = False
        self._exited = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Capture the public transport and kill it if launch ownership expired.

        asyncio invokes this callback before completing its subprocess creation
        future on the supported CPython 3.12 selector loop. A late/aborted child
        is synchronously signaled by its new-session PGID and its transport is
        closed; callback execution itself is loop-synchronous.
        """
        if not isinstance(transport, asyncio.SubprocessTransport):
            self._aborted = True
            transport.close()
            return
        self._transport = transport
        self._pid = transport.get_pid()
        if self._pid is None:
            self._signal_failed = True
        if self._aborted or self._pid is None or time.monotonic() >= self._spawn_deadline:
            self._aborted = True
            if self._pid is not None:
                try:
                    os.killpg(self._pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    self._signal_failed = True
            transport.close()

    def process_exited(self) -> None:
        """Publish leader exit after asyncio records the public return code."""
        self._exited.set()

    def connection_lost(self, exc: Exception | None) -> None:
        """Record transport closure without treating it as proof of group exit."""
        if exc is not None:
            self._signal_failed = True

    def abort(self) -> None:
        """Mark launch abandoned and immediately signal/close a captured child.

        Repeated calls are no-ops. If the protocol callback arrives later it
        performs the group kill; signal failures remain visible to teardown.
        """
        if self._aborted:
            return
        self._aborted = True
        if self._pid is not None:
            try:
                os.killpg(self._pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                self._signal_failed = True
        elif self._transport is not None:
            self._signal_failed = True
        if self._transport is not None:
            self._transport.close()

    async def wait(self) -> int:
        """Wait for the leader-exited callback, independent of inherited pipe EOF."""
        await self._exited.wait()
        code = self.returncode
        if code is None:
            raise RuntimeError("asyncio reported process exit without a return code")
        return code

    async def aclose(self) -> None:
        """Close the public process transport without waiting for pipe EOF or exit."""
        if self._transport is not None:
            self._transport.close()
        await anyio.lowlevel.checkpoint()

    def terminate(self) -> None:
        """Send SIGTERM through the public subprocess transport."""
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        """Send SIGKILL through the public subprocess transport."""
        self.send_signal(signal.SIGKILL)

    def send_signal(self, signal_number: int) -> None:
        """Send a signal to the leader through asyncio's public transport API."""
        if self._transport is None:
            raise ProcessLookupError("MCP stdio process has no captured transport")
        self._transport.send_signal(signal_number)

    @property
    def pid(self) -> int:
        """Return the captured leader PID, or fail when launch never connected."""
        if self._pid is None:
            raise ProcessLookupError("MCP stdio process PID was not captured")
        return self._pid

    @property
    def returncode(self) -> int | None:
        """Return the public transport's leader status, if it has exited."""
        return None if self._transport is None else self._transport.get_returncode()

    @property
    def stdin(self) -> None:
        """Expose no AnyIO stream because stdin is owned by ``_StdioWritePipe``."""
        return None

    @property
    def stdout(self) -> None:
        """Expose no AnyIO stream because stdout is owned by ``_StdioReadPipe``."""
        return None

    @property
    def stderr(self) -> None:
        """Expose no stderr stream because child stderr is redirected to DEVNULL."""
        return None


@asynccontextmanager
async def stdio_client_transport(
    profile: StdioDeploymentProfile,
    fence: Callable[[], Awaitable[bool]],
) -> AsyncIterator[tuple[
    ObjectReceiveStream[SessionMessage | Exception], ObjectSendStream[SessionMessage]
]]:
    """Yield bounded SDK streams for a reviewed POSIX subprocess.

    The fence checks current authority before launch, after launch, before each
    outbound frame and before result egress. Cooperative work uses one absolute
    deadline with reserved teardown time. Synchronous local filesystem calls
    and an individual OS read cannot be interrupted. Only CPython 3.12's default
    POSIX asyncio selector loop is source-reviewed; other backends fail closed.
    """
    deadline = time.monotonic() + _OPERATION_SECONDS
    work_deadline = deadline - _CLEANUP_RESERVE_SECONDS
    if os.name != "posix":
        raise McpTransportError("MCP stdio requires POSIX")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise McpTransportError("MCP stdio requires the supported asyncio loop") from exc
    if type(loop) is not asyncio.SelectorEventLoop or os.geteuid() == 0:
        raise McpTransportError("MCP stdio requires default selector loop and non-root service identity")

    try:
        argv, env, artifacts = tuple(profile.argv), dict(profile.environment), dict(profile.artifact_sha256)
    except Exception as exc:
        raise McpTransportError("MCP stdio profile is invalid") from exc
    if (
        not isinstance(profile.profile_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", profile.profile_id) is None
        or profile.enabled is not True or profile.platform != "posix"
        or profile.operation_kind not in ("ordinary", "discovery")
        or profile.runtime_kind not in ("native", "python", "node")
        or not isinstance(profile.profile_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", profile.profile_hash) is None
        or profile.reviewed_profile_hash != profile.profile_hash
    ):
        raise McpTransportError("MCP stdio profile or reviewed hash is invalid")
    if not (1 <= len(argv) <= 32) or any(
        not isinstance(v, str) or not v or len(v) > 2048 or "\x00" in v for v in argv
    ) or sum(map(len, argv)) > 16 * 1024:
        raise McpTransportError("MCP stdio arguments exceed reviewed bounds")
    if len(env) > 32 or any(
        k not in {"LANG", "LC_ALL", "TZ"} or not isinstance(v, str)
        or len(k) > 128 or len(v) > 2048 or "\x00" in k or "\x00" in v for k, v in env.items()
    ) or sum(len(k) + len(v) for k, v in env.items()) > 16 * 1024:
        raise McpTransportError("MCP stdio environment exceeds exact allowlist")
    if not (1 <= len(artifacts) <= 32):
        raise McpTransportError("MCP stdio artifact manifest is invalid")

    if not all(isinstance(p, str) for p in (profile.immutable_root, profile.cwd, profile.executable)):
        raise McpTransportError("MCP stdio launch paths must be strings")
    root_in, cwd_in, exe_in = map(Path, (profile.immutable_root, profile.cwd, profile.executable))
    if not all(p.is_absolute() for p in (root_in, cwd_in, exe_in)):
        raise McpTransportError("MCP stdio paths must be absolute")
    try:
        root, cwd, exe = root_in.resolve(strict=True), cwd_in.resolve(strict=True), exe_in.resolve(strict=True)
        cwd.relative_to(root)
        exe.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise McpTransportError("MCP stdio path is unavailable or outside immutable root") from exc
    if any(str(p) != str(q) for p, q in ((root, root_in), (cwd, cwd_in), (exe, exe_in))):
        raise McpTransportError("MCP stdio paths must be canonical and symlink-free")
    if exe.suffix.lower() in {".bat", ".cmd", ".ps1"} or exe.name.lower() in {
        "sh", "bash", "dash", "zsh", "fish", "npm", "npx", "cmd", "powershell", "pwsh",
    }:
        raise McpTransportError("MCP stdio shell and package-manager launchers are unsupported")
    if profile.runtime_kind == "native":
        if profile.entry_script is not None:
            raise McpTransportError("native profile cannot name an entry script")
    elif (
        not isinstance(profile.entry_script, str) or not Path(profile.entry_script).is_absolute()
        or str(Path(profile.entry_script)) != profile.entry_script or argv[0] != profile.entry_script
    ):
        raise McpTransportError("interpreter needs a canonical entry script as argv[0]")
    if any(
        arg in {"-c", "-m", "-e", "-r", "--eval", "--command", "--require", "--import", "--loader"}
        or arg.startswith(("-c", "-m", "-e", "-r", "--eval=", "--command=", "--require=", "--import=", "--loader="))
        for arg in argv
    ):
        raise McpTransportError("MCP stdio command-evaluation and interpreter-loader options are unsupported")

    # Stable identity covers all deployment inputs and quotas, excluding operation_kind.
    policy = {
        "policy": "bbd-os-mcp-stdio-v1", "profile_id": profile.profile_id,
        "platform": profile.platform, "enabled": profile.enabled,
        "runtime_kind": profile.runtime_kind, "entry_script": profile.entry_script,
        "executable": str(exe), "argv": argv, "cwd": str(cwd),
        "environment": dict(sorted(env.items())), "immutable_root": str(root),
        "artifact_sha256": dict(sorted(artifacts.items())),
        "limits": {
            "ordinary_frame": _ORDINARY_FRAME_BYTES, "ordinary_total": _ORDINARY_TOTAL_BYTES,
            "ordinary_frames": _ORDINARY_FRAMES, "discovery_frame": _DISCOVERY_FRAME_BYTES,
            "discovery_total": _DISCOVERY_TOTAL_BYTES, "discovery_frames": _DISCOVERY_FRAMES,
            "outbound_frame": _OUTBOUND_FRAME_BYTES,
        },
    }
    try:
        calculated = hashlib.sha256(json.dumps(
            policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise McpTransportError("MCP stdio policy is not canonical JSON") from exc
    if calculated != profile.profile_hash:
        raise McpTransportError("MCP stdio profile hash mismatch")

    paths: dict[str, str] = {}
    total = 0
    for raw_path, expected in artifacts.items():
        await anyio.lowlevel.checkpoint()
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise McpTransportError("MCP stdio artifact manifest is invalid")
        try:
            path = Path(raw_path).resolve(strict=True)
            path.relative_to(root)
            if str(path) != raw_path:
                raise ValueError("non-canonical")
            info = path.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise McpTransportError("MCP stdio artifact is unavailable or outside root") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size > _ARTIFACT_BYTES_LIMIT:
            raise McpTransportError("MCP stdio artifact is not a bounded regular file")
        if info.st_uid != 0 or info.st_mode & 0o022 or os.access(path, os.W_OK):
            raise McpTransportError("MCP stdio artifact is not root-owned and immutable")
        if not os.access(path, os.R_OK):
            raise McpTransportError("MCP stdio artifact is not readable")
        total += info.st_size
        if total > _ARTIFACTS_BYTES_LIMIT:
            raise McpTransportError("MCP stdio manifest exceeds aggregate bound")
        try:
            parts = path.relative_to(root).parts
            dirs = (root, *(root.joinpath(*parts[:i]) for i in range(1, len(parts))))
            cwd_dirs = tuple(reversed(cwd.parents[:-1])) + (cwd,)
            for directory in (*root.parents, *dirs, *cwd_dirs):
                await anyio.lowlevel.checkpoint()
                item = directory.stat()
                if not stat.S_ISDIR(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022 or os.access(directory, os.W_OK):
                    raise McpTransportError("MCP stdio path ancestry is not root-owned and immutable")
                if time.monotonic() >= work_deadline:
                    raise TimeoutError
            if not stat.S_ISDIR(cwd.stat().st_mode):
                raise McpTransportError("MCP stdio cwd is not a directory")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while True:
                    await anyio.lowlevel.checkpoint()
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    if time.monotonic() >= work_deadline:
                        raise TimeoutError
        except McpTransportError:
            raise
        except (OSError, TimeoutError) as exc:
            raise McpTransportError("MCP stdio artifact verification failed") from exc
        if digest.hexdigest() != expected:
            raise McpTransportError("MCP stdio artifact digest changed")
        paths[raw_path] = str(path)
    if str(exe) not in paths or not os.access(exe, os.X_OK):
        raise McpTransportError("MCP stdio executable is not a reviewed executable artifact")
    for argument in argv:
        if Path(argument).is_absolute() and argument not in paths:
            raise McpTransportError("MCP stdio absolute argument is outside the reviewed manifest")
    if profile.runtime_kind != "native":
        try:
            entry = Path(profile.entry_script).resolve(strict=True)
            entry.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise McpTransportError("MCP stdio entry script is unavailable") from exc
        if str(entry) != profile.entry_script or profile.entry_script not in paths:
            raise McpTransportError("MCP stdio entry script is not canonical and manifested")
    if time.monotonic() >= work_deadline:
        raise McpTransportError("MCP stdio verification exceeded work deadline")

    limits = (
        (_DISCOVERY_FRAME_BYTES, _DISCOVERY_TOTAL_BYTES, _DISCOVERY_FRAMES)
        if profile.operation_kind == "discovery"
        else (_ORDINARY_FRAME_BYTES, _ORDINARY_TOTAL_BYTES, _ORDINARY_FRAMES)
    )
    rs, rr = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    ws, wr = anyio.create_memory_object_stream[SessionMessage](0)
    shutdown, writer_done = anyio.Event(), anyio.Event()
    counters = [0, 0]  # Shared aggregate byte and complete-frame counters.
    process = _StdioProcess(spawn_deadline=min(work_deadline, time.monotonic() + _SPAWN_SECONDS))
    owned: list[int] = []
    stdin = stdout = None
    launched = finished = uncertain = False
    try:
        with anyio.fail_after(max(0.0, work_deadline - time.monotonic())):
            try:
                with anyio.fail_after(1.0):
                    if not await fence():
                        raise McpTransportError("MCP stdio fence is no longer current")
            except McpTransportError:
                raise
            except Exception as exc:
                raise McpTransportError("MCP stdio fence could not be verified") from exc
            # Ownership begins before the first pipe; child ends remain blocking.
            child_in, parent_in = os.pipe()
            owned.extend((child_in, parent_in))
            parent_out, child_out = os.pipe()
            owned.extend((parent_out, child_out))
            os.set_blocking(parent_in, False)
            os.set_blocking(parent_out, False)
            stdin, stdout = _StdioWritePipe(parent_in), _StdioReadPipe(parent_out)
            owned.remove(parent_in)
            owned.remove(parent_out)
            try:
                budget = min(_SPAWN_SECONDS, work_deadline - time.monotonic())
                if budget <= 0:
                    raise TimeoutError
                with anyio.fail_after(budget):
                    transport, _ = await loop.subprocess_exec(
                        lambda: process, str(exe), *argv,
                        stdin=child_in, stdout=child_out, stderr=subprocess.DEVNULL,
                        cwd=str(cwd), env=env, start_new_session=True,
                    )
                launched = True
                if process._transport is not transport or process._pid is None or process._aborted:
                    process.abort()
                    raise McpTransportError("MCP stdio process ownership was not captured")
            except TimeoutError as exc:
                process.abort()
                raise McpTransportError("MCP stdio spawn exceeded its deadline") from exc
            except BaseException:
                process.abort()
                raise
            finally:
                for fd in (child_in, child_out):
                    if fd in owned:
                        owned.remove(fd)
                        try:
                            os.close(fd)
                        except OSError:
                            uncertain = True
            try:
                with anyio.fail_after(1.0):
                    if not await fence():
                        raise McpTransportError("MCP stdio fence changed after spawn")
            except McpTransportError:
                raise
            except Exception as exc:
                raise McpTransportError("MCP stdio fence recheck failed") from exc
            async with anyio.create_task_group() as group:
                group.start_soon(_read_stdio_stdout, stdout, rs, fence, counters, *limits, shutdown)
                group.start_soon(_write_stdio_stdin, stdin, wr, fence, counters, limits[1], limits[2], writer_done)
                try:
                    yield rr, ws
                finally:
                    shutdown.set()
                    ws.close()
                    rr.close()
                    with anyio.CancelScope(shield=True):
                        with anyio.move_on_after(0.5, shield=True):
                            await writer_done.wait()
                        try:
                            await _finish_stdio_process(process, stdin, stdout, deadline=deadline)
                        except Exception:
                            uncertain = True
                        finally:
                            # A final synchronous owner close runs even if group
                            # probing or helper cleanup exits unexpectedly.
                            for pipe in (stdin, stdout):
                                try:
                                    await pipe.aclose()
                                except Exception:
                                    uncertain = True
                            try:
                                await process.aclose()
                            except Exception:
                                uncertain = True
                    group.cancel_scope.cancel()
                    finished = True
        finished = True
    except TimeoutError as exc:
        raise McpTransportError("MCP stdio work deadline expired") from exc
    finally:
        if not finished:
            shutdown.set()
            for stream in (ws, rr, wr, rs):
                stream.close()
            if not launched:
                process.abort()
            if stdin is not None and stdout is not None:
                with anyio.CancelScope(shield=True):
                    try:
                        await _finish_stdio_process(process, stdin, stdout, deadline=deadline)
                    except Exception:
                        uncertain = True
            else:
                uncertain = True
                try:
                    await process.aclose()
                except Exception:
                    uncertain = True
            for fd in tuple(owned):
                owned.remove(fd)
                try:
                    os.close(fd)
                except OSError:
                    uncertain = True
            for pipe in (stdin, stdout):
                if pipe is not None:
                    try:
                        await pipe.aclose()
                    except Exception:
                        uncertain = True
            for stream in (wr, rs):
                try:
                    await stream.aclose()
                except Exception:
                    uncertain = True
        if uncertain:
            raise McpTransportError("MCP stdio cleanup ownership is uncertain")


async def _finish_stdio_process(
    process: _StdioProcess,
    stdin: _StdioWritePipe,
    stdout: _StdioReadPipe,
    *,
    deadline: float,
) -> None:
    """Close owned FDs and verify leader/group cleanup under one deadline.

    Grace expiry selects normal escalation. Uncertainty means leader reap,
    original process group, mandatory signal, or resource closure is unverified.
    The helper TERM budget is shorter than its outer stage so its SIGKILL branch
    can execute; parent FDs close regardless of process-status uncertainty.
    """
    uncertain = process._signal_failed
    end = min(deadline, time.monotonic() + _CLEANUP_RESERVE_SECONDS)
    try:
        await stdin.aclose()
    except Exception:
        uncertain = True
    grace = min(end - 1.0, time.monotonic() + 1.0)
    if process.returncode is None and grace > time.monotonic():
        with anyio.move_on_after(grace - time.monotonic(), shield=True):
            try:
                await process.wait()
            except Exception:
                uncertain = True
    budget = min(1.5, max(0.0, end - time.monotonic() - 1.0))
    if process._pid is not None and terminate_posix_process_tree is not None and budget > 0:
        with anyio.move_on_after(budget, shield=True) as stage:
            try:
                await terminate_posix_process_tree(process, timeout_seconds=1.0)
            except Exception:
                uncertain = True
        uncertain |= stage.cancelled_caught
    else:
        uncertain = True

    # Probe the original group only; a setsid descendant leaves this cleanup scope.
    state_end = min(end - 0.5, time.monotonic() + 1.0)
    gone = False
    while process._pid is not None and time.monotonic() < state_end:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            gone = True
            break
        except OSError:
            uncertain = True
            break
        await anyio.sleep(min(0.05, state_end - time.monotonic()))
    if process.returncode is None and state_end > time.monotonic():
        with anyio.move_on_after(state_end - time.monotonic(), shield=True):
            try:
                await process.wait()
            except Exception:
                uncertain = True
    if process.returncode is None:
        uncertain = True
    if not gone and process._pid is not None:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            gone = True
        except OSError:
            uncertain = True
    if not gone:
        uncertain = True
    try:
        await stdout.aclose()
    except Exception:
        uncertain = True
    try:
        await process.aclose()
    except Exception:
        uncertain = True
    if uncertain:
        raise McpTransportError("MCP stdio cleanup is uncertain")


async def _read_stdio_stdout(
    stdout: ByteReceiveStream,
    messages: ObjectSendStream[SessionMessage | Exception],
    fence: Callable[[], Awaitable[bool]],
    counters: list[int],
    max_frame_bytes: int,
    max_total_bytes: int,
    max_frames: int,
    shutdown: anyio.Event,
) -> None:
    """Frame child stdout and deliver typed SDK messages after a fresh fence check.

    Reader and writer mutate shared counters synchronously before awaits, so
    combined byte/frame accounting stays serialized on the event loop without
    locks. Over-limit, invalid UTF-8, malformed, or truncated frames raise a
    sanitized error. SDK rendezvous can backpressure a valid frame; during owner
    shutdown this worker discards output instead of retaining child-controlled
    bytes. The pipe's successful-read checkpoint lets that discard loop yield
    on every bounded chunk, so teardown/cancellation can progress while stdout
    remains continuously ready; notified FD closure ends readiness waits.
    """
    pending = bytearray()
    discarding = False
    try:
        while True:
            chunk = await stdout.receive(_READ_CHUNK_BYTES)
            if not chunk:
                if shutdown.is_set():
                    return
                raise McpTransportError("MCP stdio server closed stdout unexpectedly")
            if shutdown.is_set() or discarding:
                discarding = True
                continue
            counters[0] += len(chunk)
            if counters[0] > max_total_bytes:
                raise McpTransportError("MCP stdio operation exceeded its aggregate byte limit")
            offset = 0
            while offset < len(chunk):
                newline = chunk.find(b"\n", offset)
                end = len(chunk) if newline < 0 else newline
                segment = chunk[offset:end]
                if len(pending) + len(segment) + (1 if newline >= 0 else 0) > max_frame_bytes:
                    raise McpTransportError("MCP stdio server frame exceeds its byte limit")
                pending.extend(segment)
                if newline < 0:
                    break
                frame = bytes(pending)
                pending.clear()
                offset = newline + 1
                counters[1] += 1
                if counters[1] > max_frames:
                    raise McpTransportError("MCP stdio operation exceeded its message limit")
                try:
                    text = frame.decode("utf-8", errors="strict")
                    message = jsonrpc_message_adapter.validate_json(text, by_name=False)
                except Exception as exc:
                    raise McpTransportError("MCP stdio server sent an invalid JSON-RPC frame") from exc
                try:
                    with anyio.fail_after(1.0):
                        if not await fence():
                            raise McpTransportError("MCP stdio execution fence changed before result delivery")
                except McpTransportError:
                    raise
                except Exception as exc:
                    raise McpTransportError("MCP stdio execution fence could not be rechecked") from exc
                await messages.send(SessionMessage(message))
    except anyio.EndOfStream:
        if shutdown.is_set():
            return
        if pending:
            raise McpTransportError("MCP stdio server ended with a truncated frame")
        raise McpTransportError("MCP stdio server closed stdout unexpectedly")
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        # SDK shutdown closes the receive end; keep consuming without retaining
        # child output so a cooperative process can still finish its shutdown.
        discarding = True
        with suppress(anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            while True:
                await stdout.receive(_READ_CHUNK_BYTES)


async def _write_stdio_stdin(
    stdin: ByteSendStream,
    messages: ObjectReceiveStream[SessionMessage],
    fence: Callable[[], Awaitable[bool]],
    counters: list[int],
    max_total_bytes: int,
    max_frames: int,
    writer_done: anyio.Event,
) -> None:
    """Serialize SDK messages into bounded frames and fence before pipe effects.

    It shares event-loop counters with the reader and updates them without an
    intervening await before writing. Serialization errors, revoked fences,
    broken pipes, and partial-write cancellation fail the worker; a partial
    external frame is uncertain and is never retried as successful. The done
    event always signals task completion for bounded teardown decisions.
    """
    try:
        async with messages:
            async for session_message in messages:
                try:
                    with anyio.fail_after(1.0):
                        if not await fence():
                            raise McpTransportError("MCP stdio execution fence changed before request delivery")
                except McpTransportError:
                    raise
                except Exception as exc:
                    raise McpTransportError("MCP stdio execution fence could not be rechecked") from exc
                try:
                    wire_message = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
                    frame = (wire_message + "\n").encode("utf-8", errors="strict")
                except Exception as exc:
                    raise McpTransportError("MCP stdio client message could not be serialized") from exc
                if len(frame) > _OUTBOUND_FRAME_BYTES:
                    raise McpTransportError("MCP stdio client frame exceeds its byte limit")
                counters[0] += len(frame)
                counters[1] += 1
                if counters[0] > max_total_bytes:
                    raise McpTransportError("MCP stdio operation exceeded its aggregate byte limit")
                if counters[1] > max_frames:
                    raise McpTransportError("MCP stdio operation exceeded its message limit")
                await stdin.send(frame)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError) as exc:
        # Raising through the task group wakes Client without waiting on an SDK
        # receive rendezvous that may no longer have a consumer.
        raise McpTransportError("MCP stdio server input pipe failed") from exc
    finally:
        writer_done.set()
