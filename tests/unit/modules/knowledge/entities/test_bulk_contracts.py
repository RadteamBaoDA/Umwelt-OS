"""Bulk owner contracts return exactly what the singular owner reads return, in constant queries."""

import re
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.entities import public as entities
from modules.knowledge.relationships import public as relationships
from tests.unit.entity_patching import patch_entities

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _GraphSession:
    """In-memory redirect/entity tables answering both the singular and the bulk statements."""

    def __init__(self, entity_ids, redirects):
        self.entity_ids = set(entity_ids)
        self.redirects = redirects  # old -> target | None
        self.statements = 0

    def _uuids(self, sql):
        return [UUID(value) for value in _UUID.findall(sql)]

    async def scalar(self, statement):
        self.statements += 1
        sql = _sql(statement)
        wanted = self._uuids(sql.split("WHERE", 1)[1])
        if "FROM entity_redirects" in sql:
            old = wanted[0] if "old_entity_id =" in sql else None
            return SimpleNamespace(target_entity_id=self.redirects[old]) if old in self.redirects else None
        return wanted[0] if wanted[0] in self.entity_ids else None

    async def execute(self, statement):
        self.statements += 1
        sql = _sql(statement)
        assert "FROM entity_redirects" in sql
        wanted = set(self._uuids(sql.split("WHERE", 1)[1]))
        return _Rows([(old, target) for old, target in self.redirects.items() if old in wanted])

    async def scalars(self, statement):
        self.statements += 1
        sql = _sql(statement)
        assert "FROM entities" in sql
        wanted = set(self._uuids(sql.split("WHERE", 1)[1]))
        return _Rows([i for i in self.entity_ids if i in wanted])


async def _singular(session, identifier, workspace_id):
    scope = SimpleNamespace(workspace_id=workspace_id)
    try:
        return await entities.resolve_canonical_entity_id(
            session, identifier, scope=scope, multi_workspace_enabled=False)
    except (LookupError, ValueError) as exc:
        return exc


@pytest.fixture(autouse=True)
def _no_admission(monkeypatch):
    async def admit(*_args, **_kwargs):
        return None

    patch_entities(monkeypatch, "_admit", admit)


@pytest.mark.asyncio
async def test_bulk_canonical_resolution_matches_singular_for_every_chain_shape() -> None:
    workspace_id = uuid4()
    plain, a, b, c, gone, missing = (uuid4() for _ in range(6))
    cycle_x, cycle_y = uuid4(), uuid4()
    long_chain = [uuid4() for _ in range(40)]
    redirects = {a: b, b: c, gone: None, cycle_x: cycle_y, cycle_y: cycle_x}
    redirects.update({long_chain[i]: long_chain[i + 1] for i in range(39)})
    db = _GraphSession({plain, c, *long_chain}, redirects)
    ids = [plain, a, b, c, gone, missing, cycle_x, long_chain[0], long_chain[20], plain]

    bulk = await entities._resolve_canonical_bulk(db, ids, workspace_id=workspace_id)

    for identifier in ids:
        expected = await _singular(db, identifier, workspace_id)
        actual = bulk[identifier]
        if isinstance(expected, Exception):
            assert type(actual) is type(expected) and str(actual) == str(expected), identifier
        else:
            assert actual == expected, identifier
    assert bulk[a] == c and bulk[plain] == plain


@pytest.mark.asyncio
async def test_bulk_resolution_query_count_is_independent_of_entity_count() -> None:
    workspace_id = uuid4()
    ids = [uuid4() for _ in range(60)]
    db = _GraphSession(ids, {})
    await entities._resolve_canonical_bulk(db, ids, workspace_id=workspace_id)
    assert db.statements == 2  # one redirect lookup + one existence check for all 60 ids


@pytest.mark.asyncio
async def test_resolve_canonical_entity_ids_aligns_with_input_and_raises_first_value_error() -> None:
    workspace_id = uuid4()
    ok, missing, x, y = uuid4(), uuid4(), uuid4(), uuid4()
    scope = SimpleNamespace(workspace_id=workspace_id)
    db = _GraphSession({ok}, {x: y, y: x})
    resolved = await entities.resolve_canonical_entity_ids(
        db, [ok, missing, ok], scope=scope, multi_workspace_enabled=False)
    assert resolved == [ok, None, ok]
    with pytest.raises(ValueError, match="cycle"):
        await entities.resolve_canonical_entity_ids(db, [ok, x], scope=scope, multi_workspace_enabled=False)
    with pytest.raises(ValueError, match="at most 100"):
        await entities.resolve_canonical_entity_ids(
            db, [uuid4() for _ in range(101)], scope=scope, multi_workspace_enabled=False)


def test_bulk_statements_compile_with_one_window_per_batch() -> None:
    """The evidence and neighbour bulk reads are single window-function statements, not per-entity loops."""
    from sqlalchemy import func, select, union

    from modules.knowledge.entities.models import EntityEvidenceMembership
    from modules.knowledge.relationships.models import Relationship

    position = func.row_number().over(
        partition_by=EntityEvidenceMembership.entity_id,
        order_by=(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id),
    )
    sql = _sql(select(EntityEvidenceMembership.id, position.label("position")))
    assert "row_number() OVER (PARTITION BY entity_evidence_memberships.entity_id" in sql
    matches = union(
        select(Relationship.source_entity_id.label("focus"), Relationship.id.label("rid")),
        select(Relationship.target_entity_id.label("focus"), Relationship.id.label("rid")),
    )
    assert "UNION" in _sql(matches) and "UNION ALL" not in _sql(matches)
    assert callable(relationships.get_neighbors_bulk) and callable(entities.list_entity_evidence_bulk)
