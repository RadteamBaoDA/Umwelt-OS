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


def test_dismissed_migration_downgrade_sql_drops_dismissed_only_rows() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade",
         "r15_document_dismissed:r15_document_language_backfill", "--sql"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    delete = out.index("DELETE FROM document_interactions WHERE read_at IS NULL AND bookmarked_at IS NULL")
    assert delete < out.index("DROP COLUMN dismissed_at")
    assert "read_at IS NOT NULL OR bookmarked_at IS NOT NULL)" in out


def test_rule_delivery_migration_adds_and_drops_rule_last_notified() -> None:
    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade",
         "r15_document_dismissed:r15_highlight_rule_delivery", "--sql"],
        check=False, capture_output=True, text=True,
    )
    assert up.returncode == 0, up.stderr
    assert "ALTER TABLE gadget_highlight_progress ADD COLUMN rule_last_notified JSONB DEFAULT '{}' NOT NULL" in up.stdout
    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade",
         "r15_highlight_rule_delivery:r15_document_dismissed", "--sql"],
        check=False, capture_output=True, text=True,
    )
    assert down.returncode == 0, down.stderr
    assert "DROP COLUMN rule_last_notified" in down.stdout
