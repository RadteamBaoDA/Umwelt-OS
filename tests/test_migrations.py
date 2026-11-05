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


def _alembic(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://u:build-placeholder@postgres:5432/db", **(extra_env or {})}
    return subprocess.run(  # noqa: S603  # test-only subprocess with fixed arguments
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
    baseline = _alembic("upgrade", "base:0001_init", "--sql")
    assert baseline.returncode == 0, baseline.stderr
    assert "CONCURRENTLY" not in baseline.stdout  # the baseline runs in one transaction
    assert "CREATE FUNCTION uuid_generate_v7()" in sql


def test_offline_upgrade_inserts_required_seed_rows() -> None:
    sql = _upgrade_sql()
    assert "INSERT INTO connector_admission_slots (slot_id) VALUES (1), (2)" in sql
    assert "INSERT INTO translation_admission_slots (id, translation_id, fencing_token, expires_at) VALUES (1, NULL, 0, NULL)" in sql
    assert "INSERT INTO github_webhook_capacity (id, digest_count, pending_count) VALUES (1, 0, 0)" in sql
    assert "INSERT INTO checkpoint_migrations (v) VALUES (8)" in sql


def test_downgrade_is_destructive_guarded_and_documented() -> None:
    refused = _alembic("downgrade", f"{BASE}:base", "--sql")
    assert refused.returncode != 0 and "UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE" in refused.stderr
    assert "DROP SCHEMA public CASCADE" not in refused.stdout
    result = _alembic("downgrade", f"{BASE}:base", "--sql", extra_env={"UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE": "1"})
    assert result.returncode == 0, result.stderr
    assert "DROP SCHEMA public CASCADE" in result.stdout
    assert "CREATE SCHEMA public" in result.stdout
    doc = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(BASE).module.downgrade.__doc__  # type: ignore[union-attr]
    assert doc is not None and "DESTRUCTIVE" in doc and "UMWELT_ALLOW_DESTRUCTIVE_DOWNGRADE" in doc


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
    sql = _alembic("upgrade", f"{MERGE_REVISION}:{CHAT_SQL_READER}", "--sql").stdout
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'umwelt_chat_sql_reader')" in sql
    assert "CREATE ROLE umwelt_chat_sql_reader NOLOGIN NOSUPERUSER NOINHERIT" in sql
    assert "GRANT umwelt_chat_sql_reader TO CURRENT_USER" in sql
    assert "GRANT SELECT" not in sql.split("umwelt_chat_sql_reader", 1)[1]  # the role gets no table privileges
    down = _alembic("downgrade", f"{CHAT_SQL_READER}:{MERGE_REVISION}", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP ROLE IF EXISTS umwelt_chat_sql_reader" in down.stdout


def test_worker_dead_letters_revision_follows_perf_indexes_and_creates_the_table() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision("0015_worker_dead_letters").down_revision == "0014_perf_indexes_and_safety"  # type: ignore[union-attr]
    sql = _upgrade_sql()
    assert "CREATE TABLE worker_dead_letters" in sql and "uq_worker_dead_letters_queue_job" in sql


def test_workspace_storage_state_migration_makes_the_erasure_flag_permanent() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    revision = script.get_revision("0016_workspace_storage_state")
    assert revision is not None and revision.down_revision == "0015_worker_dead_letters"
    sql = _upgrade_sql()
    assert "CREATE TABLE workspace_storage_state" in sql
    assert "rows are permanent" in sql and "cannot be undone" in sql  # DELETE and un-delete are rejected by the trigger
    down = _alembic("downgrade", "0016_workspace_storage_state:0015_worker_dead_letters", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP TABLE workspace_storage_state" in down.stdout


def test_chat_sql_login_role_migration_is_isolated_and_passwordless() -> None:
    """0017 creates a LOGIN role with no membership/password, read-only defaults and grants only on the two views."""
    up = _alembic("upgrade", "0016_workspace_storage_state:0017_chat_sql_login_role", "--sql")
    assert up.returncode == 0, up.stderr
    sql = up.stdout
    assert "CREATE ROLE umwelt_chat_sql_login LOGIN NOSUPERUSER NOINHERIT" in sql
    assert "PASSWORD" not in sql.upper().replace("PASSWORDLESS", "")
    assert "GRANT umwelt_chat_sql_reader" not in sql and "TO CURRENT_USER" not in sql
    assert "ALTER ROLE umwelt_chat_sql_login SET default_transaction_read_only = on" in sql
    assert "GRANT SELECT ON chat_sql.scoped_observations, chat_sql.scoped_documents TO umwelt_chat_sql_login" in sql
    down = _alembic("downgrade", "0017_chat_sql_login_role:0016_workspace_storage_state", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP ROLE IF EXISTS umwelt_chat_sql_login" in down.stdout


def test_chat_sql_query_fence_migration_has_no_reader_grants_and_restores_views() -> None:
    """0018 creates the fence table without reader privileges, joins it by pid, and downgrade restores the GUC views."""
    up = _alembic("upgrade", "0017_chat_sql_login_role:0018_chat_sql_query_fence", "--sql")
    assert up.returncode == 0, up.stderr
    assert "CREATE TABLE chat_sql.query_fence" in up.stdout
    assert "REVOKE ALL ON chat_sql.query_fence FROM umwelt_chat_sql_login" in up.stdout
    assert "f.backend_pid = pg_backend_pid()" in up.stdout and "current_setting" not in up.stdout
    down = _alembic("downgrade", "0018_chat_sql_query_fence:0017_chat_sql_login_role", "--sql")
    assert down.returncode == 0, down.stderr
    assert "current_setting('umwelt.chat_sql_workspace'" in down.stdout and "DROP TABLE IF EXISTS chat_sql.query_fence" in down.stdout


def test_chat_sql_reader_no_public_writes_migration_revokes_and_restores() -> None:
    """0019 revokes PUBLIC TEMPORARY and large-object/notify/advisory EXECUTE; downgrade grants them back to PUBLIC."""
    up = _alembic("upgrade", "0018_chat_sql_query_fence:0019_chat_sql_reader_no_public_writes", "--sql")
    assert up.returncode == 0, up.stderr
    assert "REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC" in up.stdout
    assert "REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC" in up.stdout and "lo_from_bytea" in up.stdout
    assert "GRANT EXECUTE ON FUNCTION %s TO %I" in up.stdout
    down = _alembic("downgrade", "0019_chat_sql_reader_no_public_writes:0018_chat_sql_query_fence", "--sql")
    assert down.returncode == 0, down.stderr
    assert "GRANT TEMPORARY ON DATABASE %I TO PUBLIC" in down.stdout
    assert "GRANT EXECUTE ON FUNCTION %s TO PUBLIC" in down.stdout
