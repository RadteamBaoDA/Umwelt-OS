import subprocess
import sys


def test_empty_database_migration_emits_single_owner_and_session_schema() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE owner" in result.stdout
    assert "CONSTRAINT ck_owner_singleton CHECK (id = 1)" in result.stdout
    assert "CREATE TABLE auth_session" in result.stdout
    assert "ix_auth_session_expires_at" in result.stdout
    assert "version_num VARCHAR(255)" in result.stdout
    assert "VARCHAR(32)" not in result.stdout.split("CREATE TABLE owner")[0]
