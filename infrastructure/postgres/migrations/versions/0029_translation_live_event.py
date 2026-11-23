"""Allow ``live_event`` as a translatable ``resource_type`` and store its inline source snapshot.

Adds nullable ``content_translations.source_text`` (JSONB title/summary supplied by the owner's
client for ``live_event`` only; removed with the row by the nightly 30-day expiry). Widens the
``ck_content_translations_resource_type`` and ``ck_translation_batch_items_resource_type`` CHECK
constraints using ``NOT VALID`` followed by ``VALIDATE CONSTRAINT`` so existing rows are validated
under a ``SHARE UPDATE EXCLUSIVE`` lock. Downgrade deletes derived ``live_event`` cache and
batch-item rows, drops the column and restores the three-value constraints.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_translation_live_event"
down_revision: str | Sequence[str] | None = "0028_translation_doc_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = "'news_story', 'daily_brief', 'document'"
_NEW_TYPES = "'news_story', 'daily_brief', 'document', 'live_event'"


def _replace_check(table: str, name: str, expression: str) -> None:
    """Drop CHECK ``name`` on ``table`` and re-add it as ``expression`` (``NOT VALID`` then validated)."""
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    """Add ``source_text`` and widen translation resource_type CHECK constraints to ``live_event``."""
    op.execute("ALTER TABLE content_translations ADD COLUMN IF NOT EXISTS source_text JSONB")
    _replace_check(
        "content_translations",
        "ck_content_translations_resource_type",
        f"resource_type IN ({_NEW_TYPES})",
    )
    _replace_check(
        "translation_batch_items",
        "ck_translation_batch_items_resource_type",
        f"resource_type IN ({_NEW_TYPES})",
    )


def downgrade() -> None:
    """Delete derived live_event rows, drop ``source_text`` and restore the three-value CHECKs."""
    op.execute("DELETE FROM translation_batch_items WHERE resource_type = 'live_event'")
    op.execute("DELETE FROM content_translations WHERE resource_type = 'live_event'")
    op.execute("ALTER TABLE content_translations DROP COLUMN IF EXISTS source_text")
    _replace_check(
        "content_translations",
        "ck_content_translations_resource_type",
        f"resource_type IN ({_OLD_TYPES})",
    )
    _replace_check(
        "translation_batch_items",
        "ck_translation_batch_items_resource_type",
        f"resource_type IN ({_OLD_TYPES})",
    )
