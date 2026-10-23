import os
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

BASE = "0001_init"
SECOND = "0002_observations_provider_ids"
HEAD = "0003_model_test_results"


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://u:build-placeholder@postgres:5432/db"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], check=False, capture_output=True, text=True, env=env
    )


def _upgrade_sql() -> str:
    result = _alembic("upgrade", "base:head", "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_single_head_follows_the_init_baseline() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [HEAD]
    assert script.get_revision(BASE).down_revision is None  # type: ignore[union-attr]
    assert script.get_revision(SECOND).down_revision == BASE  # type: ignore[union-attr]
    assert script.get_revision(HEAD).down_revision == SECOND  # type: ignore[union-attr]
    result = _alembic("heads")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [HEAD, "(head)"]


def test_offline_upgrade_renders_schema_extensions_and_wide_version_column() -> None:
    sql = _upgrade_sql()
    assert "version_num VARCHAR(255)" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in sql
    # pg_stat_statements stays behind the shared_preload_libraries guard.
    assert "shared_preload_libraries" in sql and "pg_stat_statements" in sql
    for fragment in (
        "CREATE TABLE owner", "CREATE TABLE auth_session", "CREATE TABLE workspaces", "CREATE TABLE sources",
        "CREATE TABLE chat_todos", "CREATE TABLE chat_templates", "CREATE TABLE account_deletion_operations",
        "CREATE TABLE connector_provider_terms", "CREATE TABLE content_translations",
        "CREATE UNIQUE INDEX uq_owner_email", "CREATE INDEX ix_w2_documents_scope",
        "ck_document_cleanup_original_epoch", "ck_source_purge_operations_configuration_revision",
        "CREATE TRIGGER trg_agent_runs_purge_checkpoints",
    ):
        assert fragment in sql, fragment


def test_offline_upgrade_final_state_has_no_retired_objects_or_v4_dependencies() -> None:
    sql = _upgrade_sql()
    assert "p12_backup" not in sql
    for column in ("chat_alias", "brief_alias"):
        assert column not in sql  # ai_settings alias columns were replaced by ai_settings.defaults
    assert "aliases JSONB" not in sql
    assert "graph_enabled BOOLEAN DEFAULT true NOT NULL" in sql
    assert "rag_index_enabled BOOLEAN DEFAULT true NOT NULL" in sql
    assert "DEFAULT gen_random_uuid()" not in sql  # ids come from the application (UUIDv7)
    assert "CONCURRENTLY" not in sql  # the baseline runs in one transaction
    assert "CREATE FUNCTION uuid_generate_v7()" in sql


def test_offline_upgrade_inserts_required_seed_rows() -> None:
    sql = _upgrade_sql()
    assert "INSERT INTO connector_admission_slots (slot_id) VALUES (1), (2)" in sql
    assert "INSERT INTO translation_admission_slots (id, translation_id, fencing_token, expires_at) VALUES (1, NULL, 0, NULL)" in sql
    assert "INSERT INTO github_webhook_capacity (id, digest_count, pending_count) VALUES (1, 0, 0)" in sql
    assert "INSERT INTO checkpoint_migrations (v) VALUES (8)" in sql


def test_downgrade_is_destructive_and_documented() -> None:
    result = _alembic("downgrade", f"{BASE}:base", "--sql")
    assert result.returncode == 0, result.stderr
    assert "DROP SCHEMA public CASCADE" in result.stdout
    assert "CREATE SCHEMA public" in result.stdout
    doc = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(BASE).module.downgrade.__doc__  # type: ignore[union-attr]
    assert doc is not None and "DESTRUCTIVE" in doc


def test_observations_provider_ids_match_application_constant() -> None:
    from modules.knowledge.documents.schemas import MEASUREMENT_PROVIDER_IDS

    module = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(SECOND).module  # type: ignore[union-attr]
    assert list(module._PROVIDER_IDS) == sorted(MEASUREMENT_PROVIDER_IDS)
    assert "ck_observations_provider CHECK (provider IN ('alpha_vantage', 'alternative_me'" in _upgrade_sql()
    doc = module.downgrade.__doc__
    assert doc is not None and "raises" in doc
