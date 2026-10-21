"""Per-source graph/RAG switches: contracts, indexing eligibility and builtin enable plumbing."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from modules.connectors.builtins import BuiltinEnableRequest
from modules.search.indexing import eligible_chunks
from modules.sources import public as sources
from modules.sources.schemas import SourceCreate, SourcePatch, SourceRead


def test_create_defaults_on_and_accepts_off() -> None:
    created = SourceCreate(type="rss", name="n")
    assert created.graph_enabled is True and created.rag_index_enabled is True
    off = SourceCreate(type="rss", name="n", graph_enabled=False, rag_index_enabled=False)
    assert (off.graph_enabled, off.rag_index_enabled) == (False, False)


def test_patch_flags_are_optional_and_strict() -> None:
    assert SourcePatch(graph_enabled=False).model_fields_set == {"graph_enabled"}
    with pytest.raises(ValidationError):
        SourcePatch(unknown=True)  # type: ignore[call-arg]


def test_source_read_and_connector_projection_carry_flags() -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid4(), workspace_id=uuid4(), type="rss", name="n", provider=None, status="active", generation=1,
        local_only=False, graph_enabled=False, rag_index_enabled=True, configuration={}, created_at=now,
        updated_at=now, last_sync_at=None, last_success_at=None, last_error_at=None, last_error_code=None,
        collected_at=None, indexed_at=None, collection_error_code=None, processing_error_code=None, retired_at=None)
    read = SourceRead.model_validate(row, from_attributes=True)
    assert (read.graph_enabled, read.rag_index_enabled) == (False, True)
    projected = sources._connector_source(row)  # type: ignore[arg-type]
    assert (projected.graph_enabled, projected.rag_index_enabled) == (False, True)


def test_rag_disabled_sources_are_excluded_from_indexing_candidates() -> None:
    sql = str(eligible_chunks(workspace_id=uuid4()).compile(dialect=postgresql.dialect()))
    assert "rag_index_enabled" in sql


def test_builtin_enable_request_flags_default_to_unset() -> None:
    request = BuiltinEnableRequest()
    assert request.graph_enabled is None and request.rag_index_enabled is None
    assert BuiltinEnableRequest(graph_enabled=False).graph_enabled is False
