import base64
import re
import secrets
from collections.abc import Callable
from pathlib import Path

# A value is "unset" when absent, empty, or still an .env.example placeholder.
_PLACEHOLDER = re.compile(r"replace-(with|this)-")


def _token() -> str:
    """Return a 48-byte URL-safe secret (safe inside URLs and shell words)."""
    return secrets.token_urlsafe(48)


def _fernet_key() -> str:
    """Return a Fernet-compatible key (32 random bytes, url-safe base64) without importing cryptography."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _parse(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines, ignoring comments and blank lines."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), value.strip())
    return found


def ensure(env_path: Path) -> list[str]:
    """Create or complete env_path; return the keys written.

    Existing real values are never changed. Missing, empty or placeholder secrets are generated, and
    DATABASE_URL / REDIS_URL / NEO4J_AUTH are derived from the final password so they always agree.
    """
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    current = _parse(text)

    def unset(key: str) -> bool:
        value = current.get(key, "")
        return not value or bool(_PLACEHOLDER.search(value))

    updates: dict[str, str] = {}

    def want(key: str, make: str | Callable[[], str]) -> str:
        if unset(key):
            updates[key] = make() if callable(make) else make
            current[key] = updates[key]
        return current[key]

    want("PUBLIC_ORIGIN", "http://localhost:3000")
    want("SECURE_COOKIES", "false")  # set true behind HTTPS (see docs/deployment.md)
    want("SETUP_TOKEN", _token)
    want("CSRF_SIGNING_SECRET", _token)
    want("SESSION_LIFETIME_HOURS", "24")
    want("POSTGRES_USER", "bbd")
    want("POSTGRES_DB", "bbd")
    db_password = want("POSTGRES_PASSWORD", _token)
    # Derived URLs are only generated when unset, from the (possibly pre-existing) password.
    if unset("DATABASE_URL"):
        updates["DATABASE_URL"] = f"postgresql+asyncpg://{current['POSTGRES_USER']}:{db_password}@postgres:5432/{current['POSTGRES_DB']}"
    redis_password = want("REDISCLI_AUTH", _token)
    if unset("REDIS_URL"):
        updates["REDIS_URL"] = f"redis://:{redis_password}@redis:6379/0"
    graph_password = want("GRAPH_PASSWORD", _token)
    want("GRAPH_USERNAME", "neo4j")
    if unset("NEO4J_AUTH"):
        updates["NEO4J_AUTH"] = f"{current['GRAPH_USERNAME']}/{graph_password}"
    want("BROWSER_SHARED_TOKEN", _token)
    want("N8N_ENCRYPTION_KEY", _token)
    want("N8N_WEBHOOK_TOKEN", _token)
    want("CONNECTOR_CREDENTIAL_ENCRYPTION_KEY", _fernet_key)
    want("AI_CREDENTIAL_ENCRYPTION_KEY", _fernet_key)
    want("DATA_DIR", "/data")
    want("WEB_PORT", "3000")

    if not updates:
        return []
    # Replace an existing empty/placeholder line in place, else append.
    lines = text.splitlines()
    for key, value in list(updates.items()):
        for index, line in enumerate(lines):
            if line.partition("=")[0].strip() == key and not line.startswith("#"):
                lines[index] = f"{key}={value}"
                del updates[key]
                break
    lines.extend(f"{key}={value}" for key, value in updates.items())
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(_parse("\n".join(lines)))


def main() -> None:
    """Generate any missing secrets in ./.env without altering values the owner already set."""
    env_path = Path(".env")
    existed = env_path.exists()
    before = _parse(env_path.read_text(encoding="utf-8")) if existed else {}
    ensure(env_path)
    after = _parse(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
    added = sorted(set(after) - set(before))
    if not existed:
        print("Created local .env with generated secrets. Keep it private.")
    elif added:
        print(f"Kept existing values; generated missing: {', '.join(added)}.")
    else:
        print("Kept existing .env unchanged.")


if __name__ == "__main__":
    main()
