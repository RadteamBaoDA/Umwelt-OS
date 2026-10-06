from pathlib import Path
import secrets


def main() -> None:
    """Create a private local .env with generated secrets only when no existing file is present."""
    env_path = Path(".env")
    if env_path.exists():
        print("Kept existing .env unchanged.")
        return

    db_password = secrets.token_urlsafe(48)
    n8n_key = secrets.token_urlsafe(48)
    values = {
        "PUBLIC_ORIGIN": "http://localhost:3000",
        "SECURE_COOKIES": "false",
        "SETUP_TOKEN": secrets.token_urlsafe(48),
        "CSRF_SIGNING_SECRET": secrets.token_urlsafe(48),
        "SESSION_LIFETIME_HOURS": "24",
        "POSTGRES_PASSWORD": db_password,
        "DATABASE_URL": f"postgresql+asyncpg://bbd:{db_password}@postgres:5432/bbd",
        "REDIS_URL": "redis://redis:6379/0",
        "DATA_DIR": "/data",
        "WEB_PORT": "3000",
        "N8N_ENCRYPTION_KEY": n8n_key,
        "BACKUP_AGE_RECIPIENT": "",
        "BACKUP_AGE_IDENTITY_PATH": "",
        "OMNIROUTE_BASE_URL": "",
        "OMNIROUTE_API_KEY": "",
    }
    env_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    print("Created local .env with generated secrets. Keep it private.")


if __name__ == "__main__":
    main()
