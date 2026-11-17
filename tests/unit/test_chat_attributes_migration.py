"""Static checks of revision 0025 (no database): view column order, fence, secrets filter, index DDL."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "infrastructure" / "postgres" / "migrations" / "versions"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m25 = _load("0025_chat_attributes_unaccent")
m24 = _load("0024_chat_sql_scope_fence")


def test_revision_chain_and_downgrade_restores_0024_bodies() -> None:
    """0025 follows 0024 and its downgrade views equal the 0024 upgrade views (same columns and fence)."""
    assert m25.down_revision == m24.revision
    obs_0024 = (
        f"{m24._OBSERVATIONS_COLUMNS}\n{m24._FENCE_JOIN}\n{m24._SCOPE_JOIN.format(alias='o')}\n"
        "WHERE o.workspace_id = f.workspace_id AND o.source_id = ANY(f.source_ids) "
        "AND o.is_current AND o.provider_scope_discriminator IS NOT DISTINCT FROM fs.discriminator"
    )
    assert m25._observations_view("") == m24._view("scoped_observations", obs_0024)
    doc_0024 = (
        f"{m24._DOCUMENTS_COLUMNS}\n{m24._FENCE_JOIN}\n{m24._SCOPE_JOIN.format(alias='d')}\n"
        "WHERE d.workspace_id = f.workspace_id AND d.source_id = ANY(f.source_ids) "
        f"AND {m24._DOC_DISCRIMINATOR} IS NOT DISTINCT FROM fs.discriminator"
    )
    assert m25._documents_view("") == m24._view("scoped_documents", doc_0024)


def test_new_views_append_columns_and_strip_secrets() -> None:
    """New columns come last; attributes reads both provenance paths and drops credential-like keys."""
    obs = m25._observations_view(
        f", o.latitude, o.longitude, {m25._attributes('o.document_version_id')}"
    )
    head, tail = obs.split("FROM public.observations o")
    assert (
        head.index("o.document_version_id") < head.index("o.latitude") < head.index("AS attributes")
    )
    assert (
        "provider_record,source_fields" in obs
        and "provider_record,world_data,provider_fields" in obs
    )
    assert "!~* '(token|secret|password|key)'" in obs
    assert "chat_sql.query_fence_scope fs" in tail
    doc = m25._documents_view(f", {m25._attributes('dv.id')}")
    assert doc.index("dv.id AS document_version_id") < doc.index("AS attributes")


def test_downgrade_drops_views_before_recreate_and_regrants() -> None:
    """CREATE OR REPLACE cannot drop columns: downgrade must DROP VIEW, recreate, then re-grant."""
    from unittest.mock import patch

    stmts: list[str] = []
    with (
        patch.object(m25.op, "execute", side_effect=stmts.append),
        patch.object(m25, "drop_index_concurrently"),
    ):
        m25.downgrade()
    for name in ("scoped_observations", "scoped_documents"):
        drop = stmts.index(f"DROP VIEW chat_sql.{name}")
        create = next(
            i
            for i, s in enumerate(stmts)
            if s.startswith(f"CREATE OR REPLACE VIEW chat_sql.{name}")
        )
        grant = stmts.index(f"GRANT SELECT ON chat_sql.{name} TO umwelt_chat_sql_login")
        assert drop < create < grant
        assert f"REVOKE ALL ON chat_sql.{name} FROM PUBLIC" in stmts


def test_0026_history_view_replaces_is_current_and_downgrade_restores_0025() -> None:
    """0026 swaps ``o.is_current`` for a fenced newer-row anti-join and downgrade emits the 0025 view."""
    m26 = _load("0026_chat_sql_history_antijoin")
    assert m26.down_revision == m25.revision
    up = m26._history_view()
    assert "AND o.is_current" not in up and "o.is_current," in up  # column kept, predicate gone
    assert "NOT EXISTS" in up and "chat_sql.query_fence_scope ns" in up
    assert "n.source_id = ANY(f.source_ids)" in up and "n.observed_at = o.observed_at" in up
    assert "n.source_id <>" not in up and "n.is_current" not in up
    assert "chat_sql.query_fence f" in up and "chat_sql.query_fence_scope fs" in up
    expected = m25._observations_view(
        f", o.latitude, o.longitude, {m25._attributes('o.document_version_id')}"
    )
    assert m26._0025_view() == expected
