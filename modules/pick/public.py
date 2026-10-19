"""Pick-session store contract: unguessable ids, 10 minute TTL, single use, index-only phone input.

State machine per session id: ``waiting`` -> ``scanned`` -> ``picked`` -> consumed (deleted on the desktop read).
Every transition claims the key with GETDEL first, so concurrent phone requests cannot both succeed.
"""

import json
import re
import secrets
import socket
import time
from typing import Any, Literal, Protocol

PICK_TTL_SECONDS = 600
OPTION_COUNT = 3
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")  # token_urlsafe(16) = 128 random bits
# The phone can only ever choose one of these by index; the text never comes from the client.
PROMPT_POOL: tuple[str, ...] = (
    "Solar System", "DNA Helix", "Revenue Dashboard", "Microservices Architecture",
    "Ocean Waves", "Neural Network", "Force-Directed Graph", "Audio Visualizer",
    "3D Terrain", "Atom Model", "Fractal Tree", "Rocket Launch",
)
State = Literal["waiting", "scanned", "picked"]


class PickRedis(Protocol):
    """Subset of redis.asyncio.Redis used by the store (lets tests inject an in-memory fake)."""

    async def set(self, name: str, value: str, ex: int | None = ...) -> Any:
        """Store a value with an optional expiry in seconds."""

    async def get(self, name: str) -> str | None:
        """Read a value or None."""

    async def getdel(self, name: str) -> str | None:
        """Atomically read and delete a value."""


class PickUnavailable(Exception):
    """Session is unknown, expired, consumed, or in the wrong state (deliberately indistinguishable)."""


def _key(session_id: str) -> str:
    """Return the Redis key, rejecting any id that is not exactly a 128-bit urlsafe token."""
    if not SESSION_ID_RE.fullmatch(session_id):
        raise PickUnavailable
    return f"pick:{session_id}"


async def _claim(redis: PickRedis, session_id: str, expected: State) -> dict[str, Any]:
    """Atomically take the session (GETDEL) and return it only when in ``expected`` state and unexpired."""
    raw = await redis.getdel(_key(session_id))
    doc = json.loads(raw) if raw else None
    if not doc or doc.get("state") != expected or doc.get("expires_at", 0) <= time.time():
        raise PickUnavailable
    return dict(doc)


async def _store(redis: PickRedis, session_id: str, doc: dict[str, Any]) -> None:
    """Write the session back with the remaining (never extended) lifetime."""
    remaining = int(doc["expires_at"] - time.time())
    if remaining <= 0:
        raise PickUnavailable
    await redis.set(_key(session_id), json.dumps(doc), ex=remaining)


async def create_session(redis: PickRedis) -> str:
    """Create a ``waiting`` session for an already-authenticated desktop caller and return its id."""
    session_id = secrets.token_urlsafe(16)
    await _store(redis, session_id, {"state": "waiting", "expires_at": time.time() + PICK_TTL_SECONDS})
    return session_id


async def scan(redis: PickRedis, session_id: str) -> list[dict[str, Any]]:
    """Phone opened the link: move waiting -> scanned and return 3 random pool options as ``{index, label}``.

    A rescan is rejected, so the offered options are fixed once and a leaked link cannot be re-rolled.
    """
    doc = await _claim(redis, session_id, "waiting")
    doc["state"] = "scanned"
    doc["offered"] = secrets.SystemRandom().sample(range(len(PROMPT_POOL)), OPTION_COUNT)
    await _store(redis, session_id, doc)
    return [{"index": i, "label": PROMPT_POOL[i]} for i in doc["offered"]]


async def submit(redis: PickRedis, session_id: str, index: int) -> None:
    """Phone chose an option by pool index; accepted once, and only for an index that was offered."""
    doc = await _claim(redis, session_id, "scanned")
    if not isinstance(index, int) or isinstance(index, bool) or index not in doc["offered"]:
        raise PickUnavailable  # session is burned on tampering: GETDEL already removed it
    doc["state"] = "picked"
    doc["index"] = index
    await _store(redis, session_id, doc)


async def poll(redis: PickRedis, session_id: str) -> dict[str, Any]:
    """Desktop read: report the state; a ``picked`` session is returned once with its prompt, then deleted."""
    raw = await redis.get(_key(session_id))
    doc = json.loads(raw) if raw else None
    if not doc or doc.get("expires_at", 0) <= time.time():
        raise PickUnavailable
    if doc["state"] != "picked":
        return {"state": doc["state"], "prompt": None}
    doc = await _claim(redis, session_id, "picked")
    return {"state": "picked", "prompt": PROMPT_POOL[doc["index"]]}


def lan_ipv4() -> str | None:
    """Best-effort LAN IPv4 of this host (UDP connect sends no packet); None when only loopback exists."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None if ip.startswith("127.") else ip
