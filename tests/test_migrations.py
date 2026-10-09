import os
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

# Derive the head so later additive revisions (provider terms/quota, translation) never hardcode here.
HEAD = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


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


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://u:build-placeholder@postgres:5432/db"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], check=False, capture_output=True, text=True, env=env
    )


def test_single_head_matches_script_directory() -> None:
    result = _alembic("heads")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [HEAD, "(head)"]


def test_offline_upgrade_renders_collection_receipts() -> None:
    result = _alembic("upgrade", "p14_provider_terms_quota:p14_collection_receipts", "--sql")
    assert result.returncode == 0, result.stderr
    assert "ingestion_collection_receipts" in result.stdout
    assert "continuation_state" in result.stdout


def test_offline_base_to_head_renders_cleanup_authority_checks() -> None:
    result = _alembic("upgrade", "base:head", "--sql")
    assert result.returncode == 0, result.stderr
    assert "ck_document_cleanup_original_epoch" in result.stdout
    assert "ck_source_purge_operations_configuration_revision" in result.stdout


def test_offline_upgrade_removes_empty_replay_head_and_guards_populated_database() -> None:
    result = _alembic("upgrade", "base:head", "--sql")
    assert result.returncode == 0, result.stderr
    assert "DELETE FROM realtime_replay_head WHERE id = 1" in result.stdout
    assert "p14_workspace_scope offline script requires an empty database" in result.stdout


def test_offline_downgrade_refuses_cleanly() -> None:
    result = _alembic("downgrade", f"{HEAD}:base", "--sql")
    assert result.returncode != 0
    assert "run it online" in result.stderr
    assert "get_bind" not in result.stderr

def test_offline_head_renders_provider_terms_and_quota_ledger() -> None:
    result = _alembic("upgrade", "p14_cleanup_authority:head", "--sql")
    assert result.returncode == 0, result.stderr
    for fragment in (
        "CREATE TABLE connector_provider_terms", "ck_connector_provider_terms_review_fields",
        "CREATE TABLE connector_quota_windows", "CREATE TABLE connector_provider_sends",
        "uq_connector_provider_sends_sequence", "CREATE TABLE connector_quota_debits",
        "fk_connector_quota_debits_window",
    ):
        assert fragment in result.stdout
