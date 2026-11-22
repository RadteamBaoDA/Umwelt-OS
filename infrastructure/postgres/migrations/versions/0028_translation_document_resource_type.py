"""Allow ``document`` as a translatable ``resource_type`` on translation cache and batch items.

Widens the ``ck_content_translations_resource_type`` and ``ck_translation_batch_items_resource_type``
CHECK constraints using ``NOT VALID`` followed by ``VALIDATE CONSTRAINT`` so existing rows are
validated under a ``SHARE UPDATE EXCLUSIVE`` lock. Downgrade deletes derived ``document`` cache and
batch-item rows before restoring the original two-value constraints.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_translation_doc_type"
down_revision: str | Sequence[str] | None = "0027_brief_evidence_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = "'news_story', 'daily_brief'"
_NEW_TYPES = "'news_story', 'daily_brief', 'document'"


def _replace_check(table: str, name: str, expression: str) -> None:
    """Drop CHECK ``name`` on ``table`` and re-add it as ``expression`` (``NOT VALID`` then validated)."""
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    """Widen translation resource_type CHECK constraints to include ``document``."""
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
    """Delete derived document translations and restore the original resource_type CHECK constraints."""
    op.execute("DELETE FROM translation_batch_items WHERE resource_type = 'document'")
    op.execute("DELETE FROM content_translations WHERE resource_type = 'document'")
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
