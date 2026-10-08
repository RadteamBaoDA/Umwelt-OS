import os
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


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://u:build-placeholder@postgres:5432/db"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], check=False, capture_output=True, text=True, env=env
    )


def test_single_head_is_provider_terms_quota() -> None:
    result = _alembic("heads")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["p14_provider_terms_quota", "(head)"]


def test_offline_base_to_head_renders_cleanup_authority_checks() -> None:
    result = _alembic("upgrade", "base:head", "--sql")
    assert result.returncode == 0, result.stderr
    assert "ck_document_cleanup_original_epoch" in result.stdout
    assert "ck_source_purge_operations_configuration_revision" in result.stdout


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
