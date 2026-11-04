import os
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

BASE = "0001_init"
SECOND = "0002_observations_provider_ids"
PROVIDERS_LATEST = "0010_observations_vn_stock"  # newest revision that rewrites ck_observations_provider
HEAD = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()  # derived: later revisions advance it
MERGE_REVISION = "0012_merge_live_and_capability"  # the only historical merge point (two parents)
CHAT_SQL_READER = "0013_chat_sql_reader_role"


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
    # Walking head -> base must be one contiguous linear chain that visits SECOND and ends at BASE.
    chain = [revision.revision for revision in script.walk_revisions()]
    assert chain[0] == HEAD and chain[-1] == BASE and SECOND in chain
    assert len(chain) == len(set(chain)) == len(list(script.walk_revisions("base", "heads")))
    for revision in script.walk_revisions():
        if revision.revision == MERGE_REVISION:
            assert isinstance(revision.down_revision, tuple) and len(revision.down_revision) == 2
        elif revision.down_revision is not None and revision.revision not in {"0011_merge_live_map", "0011_model_capability_evidence"}:
            assert isinstance(revision.down_revision, str)  # every other post-baseline revision has one parent
    # Everything after the merge is strictly linear and the chat SQL reader role migration follows it.
    assert script.get_revision(CHAT_SQL_READER).down_revision == MERGE_REVISION  # type: ignore[union-attr]
    assert chain.index(CHAT_SQL_READER) < chain.index(MERGE_REVISION)
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

    module = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(PROVIDERS_LATEST).module  # type: ignore[union-attr]
    assert list(module._PROVIDER_IDS) == sorted(MEASUREMENT_PROVIDER_IDS)
    sql = _upgrade_sql()
    final_check = sql.rsplit("ADD CONSTRAINT ck_observations_provider ", 1)[1].split(";", 1)[0]
    assert "'vn_stock'" in final_check
    doc = module.downgrade.__doc__
    assert doc is not None and "raises" in doc


def test_model_capability_evidence_extends_test_results_key() -> None:
    sql = _upgrade_sql()
    assert "ADD COLUMN capability VARCHAR(16) NOT NULL DEFAULT 'chat'" in sql
    assert "ADD COLUMN model_identity VARCHAR(64)" in sql
    assert "ADD PRIMARY KEY (workspace_id, provider_id, model_id, capability)" in sql


def test_chat_sql_reader_role_migration_is_least_privilege_and_guarded() -> None:
    sql = _upgrade_sql()
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'umwelt_chat_sql_reader')" in sql
    assert "CREATE ROLE umwelt_chat_sql_reader NOLOGIN NOSUPERUSER NOINHERIT" in sql
    assert "GRANT umwelt_chat_sql_reader TO CURRENT_USER" in sql
    assert "GRANT SELECT" not in sql.split("umwelt_chat_sql_reader", 1)[1]  # the role gets no table privileges
    down = _alembic("downgrade", f"{CHAT_SQL_READER}:{MERGE_REVISION}", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP ROLE IF EXISTS umwelt_chat_sql_reader" in down.stdout
