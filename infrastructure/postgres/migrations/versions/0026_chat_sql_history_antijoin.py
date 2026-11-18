"""Show series history in ``chat_sql.scoped_observations`` instead of only ``is_current`` rows.

Ticker providers make every collection a new revision of ONE series point, so the 0024/0025 ``o.is_current``
predicate left a single tick per series and chat SQL could not average or list history. The view now hides a
row only when a LATER readable row of the same series point exists (same provider, metric, symbol, region,
unit, currency, latitude, longitude and ``observed_at``; ties by ``accepted_at`` then ``ingestion_observation_id``
text), the same rule as the dashboard history. Nullable key columns compare as ``coalesce(a, sentinel) =
coalesce(b, sentinel)`` (chr(31) / NaN never occur as values): a hashable equality, unlike ``IS NOT DISTINCT
FROM``, so the anti-join can hash on the whole key. Competing rows must pass the same fence (``query_fence`` and
``query_fence_scope``), so a hidden row never hides a visible one. Columns, joins and grants are unchanged and
the ``is_current`` column stays. Downgrade recreates the exact 0025 view.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0026_chat_sql_history"
down_revision: str | Sequence[str] | None = "0025_chat_attrs_unaccent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "umwelt_chat_sql_login"
_OLD_PREDICATE = (
    "AND o.is_current AND o.provider_scope_discriminator IS NOT DISTINCT FROM fs.discriminator"
)
_NOT_SUPERSEDED = """AND o.provider_scope_discriminator IS NOT DISTINCT FROM fs.discriminator
AND NOT EXISTS (
  SELECT 1 FROM public.observations n
  JOIN chat_sql.query_fence_scope ns ON ns.backend_pid = f.backend_pid AND ns.source_id = n.source_id
  WHERE n.workspace_id = f.workspace_id AND n.source_id = ANY(f.source_ids)
    AND n.provider_scope_discriminator IS NOT DISTINCT FROM ns.discriminator
    AND n.provider = o.provider AND n.metric = o.metric AND n.observed_at = o.observed_at
    AND n.unit = o.unit
    AND coalesce(n.symbol, chr(31)) = coalesce(o.symbol, chr(31))
    AND coalesce(n.region, chr(31)) = coalesce(o.region, chr(31))
    AND coalesce(n.currency, chr(31)) = coalesce(o.currency, chr(31))
    AND coalesce(n.latitude, 'NaN'::float8) = coalesce(o.latitude, 'NaN'::float8)
    AND coalesce(n.longitude, 'NaN'::float8) = coalesce(o.longitude, 'NaN'::float8)
    AND (n.accepted_at, n.ingestion_observation_id::text) > (o.accepted_at, o.ingestion_observation_id::text)
)"""


def _0025_view() -> str:
    """Return the exact 0025 ``scoped_observations`` DDL (sibling revision loaded by path)."""
    path = Path(__file__).with_name("0025_chat_attributes_unaccent.py")
    spec = importlib.util.spec_from_file_location("_rev_0025", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load revision 0025")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(
        module._observations_view(
            f", o.latitude, o.longitude, {module._attributes('o.document_version_id')}"
        )
    )


def _history_view() -> str:
    """Return the 0025 view DDL with ``is_current`` replaced by the newer-row anti-join (same columns)."""
    ddl = _0025_view()
    if _OLD_PREDICATE not in ddl:
        raise RuntimeError("0025 view body changed; update the 0026 predicate")
    return ddl.replace(_OLD_PREDICATE, _NOT_SUPERSEDED)


def upgrade() -> None:
    """Redefine the view in place; same columns, so ``CREATE OR REPLACE`` keeps the existing grants."""
    op.execute(_history_view())


def downgrade() -> None:
    """Restore the exact 0025 view and re-apply its grants (PUBLIC revoked, SELECT to the reader login)."""
    op.execute(_0025_view())
    op.execute("REVOKE ALL ON chat_sql.scoped_observations FROM PUBLIC")
    op.execute(f"GRANT SELECT ON chat_sql.scoped_observations TO {_ROLE}")
