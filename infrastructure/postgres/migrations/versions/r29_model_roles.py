"""Replace model-alias routing with per-role defaults (chat, brief, background, embedding, reranker, vision).

Adds ``ai_settings.defaults`` and backfills it from the old alias data. The old columns stay until
``r30_drop_model_alias_columns`` so this step is lossless and reversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r29_model_roles"
down_revision: str | Sequence[str] | None = "r28_n8n_endpoint_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``defaults`` and derive each role from the previous alias mapping.

    chat = aliases[chat_alias]; brief = aliases[brief_alias]; background = aliases['reasoning-small'] (else the
    brief mapping); embedding, reranker and vision keep their same-named alias. Roles with no mapping are
    omitted. ``configuration_revision`` is bumped on every row so in-flight snapshots and capability evidence
    (which was keyed by alias) are discarded and re-probed under the role names.
    """
    op.add_column("ai_settings", sa.Column(
        "defaults", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.execute("""
        UPDATE ai_settings SET
            defaults = (
                SELECT COALESCE(jsonb_object_agg(r.k, r.v), '{}'::jsonb)
                FROM (VALUES
                    ('chat', aliases -> chat_alias),
                    ('brief', aliases -> brief_alias),
                    ('background', COALESCE(aliases -> 'reasoning-small', aliases -> brief_alias)),
                    ('embedding', aliases -> 'embedding'),
                    ('reranker', aliases -> 'reranker'),
                    ('vision', aliases -> 'vision')
                ) AS r(k, v)
                WHERE r.v IS NOT NULL AND jsonb_typeof(r.v) = 'object'
            ),
            configuration_revision = configuration_revision + 1
    """)


def downgrade() -> None:
    """Rebuild the alias columns from ``defaults`` and drop it.

    Slot choice (the role model has no equivalent of the old three LLM slots): chat -> ``reasoning-large``,
    brief -> ``reasoning-small``, background -> ``fast``; embedding, reranker and vision keep their names.
    ``chat_alias``/``brief_alias`` are reset to ``reasoning-large``/``reasoning-small`` (their column defaults).
    This assumes r30 was downgraded first, which re-creates those columns.
    """
    op.execute("""
        UPDATE ai_settings SET
            aliases = (
                SELECT COALESCE(jsonb_object_agg(s.k, s.v), '{}'::jsonb)
                FROM (VALUES
                    ('reasoning-large', defaults -> 'chat'),
                    ('reasoning-small', defaults -> 'brief'),
                    ('fast', defaults -> 'background'),
                    ('embedding', defaults -> 'embedding'),
                    ('reranker', defaults -> 'reranker'),
                    ('vision', defaults -> 'vision')
                ) AS s(k, v)
                WHERE s.v IS NOT NULL AND jsonb_typeof(s.v) = 'object'
            ),
            chat_alias = 'reasoning-large',
            brief_alias = 'reasoning-small',
            configuration_revision = configuration_revision + 1
    """)
    op.drop_column("ai_settings", "defaults")
