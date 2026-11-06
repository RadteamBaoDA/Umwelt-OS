"""Lexical search matches and ranks through the stored ``content_tsv`` column, not an inline expression."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.documents.models import DocumentChunk
from modules.search import public
from modules.search.schemas import SearchRequest


class _Scalars:
    def all(self) -> list[Any]:
        return []


class _Recorder:
    """Capture the statement `_lexical_ids` executes instead of talking to a database."""

    def __init__(self) -> None:
        self.statement: Any = None

    async def scalars(self, statement: Any) -> _Scalars:
        self.statement = statement
        return _Scalars()


@pytest.mark.asyncio
async def test_lexical_statement_uses_generated_column_for_match_and_rank() -> None:
    recorder = _Recorder()
    await public._lexical_ids(
        recorder, SearchRequest(query="alpha beta", mode="lexical"), workspace_id=uuid4()
    )  # type: ignore[arg-type]
    sql = str(
        recorder.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "document_chunks.content_tsv @@ websearch_to_tsquery('simple', 'alpha beta')" in sql
    assert (
        "ts_rank_cd(document_chunks.content_tsv, websearch_to_tsquery('simple', 'alpha beta')) DESC"
        in sql
    )
    assert "to_tsvector" not in sql  # no per-row re-parse of the content
    assert "LIMIT" in sql


def test_generated_column_expression_equals_the_replaced_inline_expression() -> None:
    column = DocumentChunk.__table__.c.content_tsv
    assert column.computed is not None and column.computed.persisted is True
    assert str(column.computed.sqltext) == "to_tsvector('simple', content)"
    gin = {index.name: index for index in DocumentChunk.__table__.indexes}[
        "ix_document_chunks_content_tsv_gin"
    ]
    assert gin.dialect_options["postgresql"]["using"] == "gin"
    assert [c.name for c in gin.columns] == ["content_tsv"]
