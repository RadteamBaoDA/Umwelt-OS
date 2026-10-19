import os
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

# Derive the head so later additive revisions (provider terms/quota, translation) never hardcode here.
# While r22 and r24 are unchained siblings there are two heads; the integrator re-chains them, so tolerate that here.
_HEADS = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
HEAD = _HEADS[0] if len(_HEADS) == 1 else "|".join(sorted(_HEADS))


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
    result = _alembic("downgrade", "r20_search_tsv_and_trgm_indexes:base", "--sql")
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


def test_dismissed_migration_downgrade_sql_drops_dismissed_only_rows() -> None:
    result = _alembic("downgrade", "r15_document_dismissed:r15_document_language_backfill", "--sql")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    delete = out.index("DELETE FROM document_interactions WHERE read_at IS NULL AND bookmarked_at IS NULL")
    assert delete < out.index("DROP COLUMN dismissed_at")
    assert "read_at IS NOT NULL OR bookmarked_at IS NOT NULL)" in out


def test_rule_delivery_migration_adds_and_drops_rule_last_notified() -> None:
    up = _alembic("upgrade", "r15_document_dismissed:r15_highlight_rule_delivery", "--sql")
    assert up.returncode == 0, up.stderr
    assert "ALTER TABLE gadget_highlight_progress ADD COLUMN rule_last_notified JSONB DEFAULT '{}' NOT NULL" in up.stdout
    down = _alembic("downgrade", "r15_highlight_rule_delivery:r15_document_dismissed", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP COLUMN rule_last_notified" in down.stdout


def test_language_index_is_workspace_first_and_drops_legacy_name() -> None:
    up = _alembic("upgrade", "p14_backend_activation:r15_document_language_backfill", "--sql")
    assert up.returncode == 0, up.stderr
    assert "ix_documents_workspace_language_created_at_id ON documents (workspace_id, language, created_at, id)" in up.stdout
    assert "DROP INDEX CONCURRENTLY IF EXISTS ix_documents_language_created_at_id" in up.stdout


def test_workspace_shares_migration_renders_and_guards_downgrade() -> None:
    up = _alembic("upgrade", "r15_highlight_rule_delivery:p14_workspace_shares", "--sql")
    assert up.returncode == 0, up.stderr
    for fragment in ("CREATE TABLE workspace_shares", "fk_workspace_shares_member", "fk_workspace_shares_matching_owner",
                     "ck_workspace_shares_not_self", "ix_workspace_shares_member_active", "WHERE revoked_at IS NULL"):
        assert fragment in up.stdout
    down = _alembic("downgrade", "p14_workspace_shares:r15_highlight_rule_delivery", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP TABLE workspace_shares" in down.stdout


def test_translation_runtime_migration_renders_slot_and_purge_index() -> None:
    up = _alembic("upgrade", "p14_workspace_shares:p14_translation_runtime", "--sql")
    assert up.returncode == 0, up.stderr
    for fragment in ("CREATE TABLE translation_admission_slots", "ck_translation_admission_slots_singleton",
                     "(translation_id IS NULL) = (expires_at IS NULL)", "VALUES (1, NULL, 0, NULL)",
                     "ix_content_translations_resource"):
        assert fragment in up.stdout
    assert "REFERENCES content_translations" not in up.stdout.split("CREATE TABLE translation_admission_slots")[1]
    down = _alembic("downgrade", "p14_translation_runtime:p14_workspace_shares", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP TABLE translation_admission_slots" in down.stdout


def test_r20_search_tsv_and_trgm_indexes_migration() -> None:
    """Verify r20 migration emits concurrent GIN FTS and trigram indexes and drops them cleanly on downgrade."""
    up = _alembic("upgrade", "r19_db_indexes_and_tuning:r20_search_tsv_and_trgm_indexes", "--sql")
    assert up.returncode == 0, up.stderr
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in up.stdout
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_document_chunks_content_tsv ON document_chunks USING gin (to_tsvector('simple', content))" in up.stdout
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entities_canonical_name_trgm ON entities USING gin (canonical_name gin_trgm_ops)" in up.stdout
    down = _alembic("downgrade", "r20_search_tsv_and_trgm_indexes:r19_db_indexes_and_tuning", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP INDEX CONCURRENTLY IF EXISTS ix_entities_canonical_name_trgm" in down.stdout
    assert "DROP INDEX CONCURRENTLY IF EXISTS ix_document_chunks_content_tsv" in down.stdout



def test_r21_drop_backup_control_migration() -> None:
    """Verify r21 drops the removed backup tables in FK order and refuses downgrade."""
    up = _alembic("upgrade", "r20_search_tsv_and_trgm_indexes:r21_drop_backup_control", "--sql")
    assert up.returncode == 0, up.stderr
    for table in ("p12_backup_activity", "p12_backup_control", "p12_backup_operations"):
        assert f"DROP TABLE IF EXISTS {table}" in up.stdout
    assert up.stdout.index("DROP TABLE IF EXISTS p12_backup_control") < up.stdout.index("DROP TABLE IF EXISTS p12_backup_operations")
    assert _alembic("downgrade", "r21_drop_backup_control:r20_search_tsv_and_trgm_indexes", "--sql").returncode != 0


def test_r24_viz_router_settings_migration() -> None:
    """Verify r24 adds and drops the viz_router JSONB column and is the head."""
    up = _alembic("upgrade", "r21_drop_backup_control:r24_viz_router_settings", "--sql")
    assert up.returncode == 0, up.stderr
    assert "ALTER TABLE ai_settings ADD COLUMN viz_router JSONB" in up.stdout
    down = _alembic("downgrade", "r24_viz_router_settings:r21_drop_backup_control", "--sql")
    assert down.returncode == 0, down.stderr
    assert "DROP COLUMN viz_router" in down.stdout
    assert HEAD == "r24_viz_router_settings"


def test_r22_chat_todos_migration() -> None:
    """Verify r22 creates chat_todos with checks and a cascading conversation FK, and drops it on downgrade."""
    up = _alembic("upgrade", "r21_drop_backup_control:r22_chat_todos", "--sql")
    assert up.returncode == 0, up.stderr
    for fragment in ("CREATE TABLE chat_todos", "ck_chat_todos_status", "fk_chat_todos_conversation",
                     "ON DELETE CASCADE", "ix_chat_todos_conversation_position"):
        assert fragment in up.stdout
    assert "DROP TABLE chat_todos" in _alembic("downgrade", "r22_chat_todos:r21_drop_backup_control", "--sql").stdout
