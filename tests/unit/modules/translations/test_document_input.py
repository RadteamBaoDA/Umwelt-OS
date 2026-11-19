"""Document translation input: scope fences, grant gating, visibility hash and bounded excerpt."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.workspaces import public as workspaces
from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.documents import _translation as tr
from modules.translations import inputs

WS, DOC, VER, SRC = uuid4(), uuid4(), uuid4(), uuid4()
OWNER = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=2, workspace_id=WS, role="member", membership_revision=1)


def _session(row):
    result = MagicMock(first=MagicMock(return_value=row))
    return MagicMock(execute=AsyncMock(return_value=result))


def _row(local_only=False, head="  hello  "):
    doc = SimpleNamespace(id=DOC, title="T")
    source = SimpleNamespace(id=SRC, generation=1, local_only=local_only)
    return (doc, VER, head, source)


@pytest.fixture(autouse=True)
def _admitted(monkeypatch):
    monkeypatch.setattr(tr, "_admit_document_scope", AsyncMock())


def _grants(monkeypatch, revision):
    grants = [SimpleNamespace(share_revision=revision)] if revision is not None else []
    monkeypatch.setattr(workspaces, "read_resource_grants", AsyncMock(return_value=grants))


async def _read(session, scope):
    return await tr.read_document_translation_input(
        session, scope=scope, document_id=DOC, multi_workspace_enabled=False
    )


async def test_owner_ready_row():
    got = await _read(_session(_row(local_only=True)), OWNER)
    assert got is not None
    assert got.resource_type == "document" and got.resource_revision == str(VER)
    assert got.local_only is True and got.fields == {"title": "T", "excerpt": "hello"}


async def test_missing_row_is_none():
    assert await _read(_session(None), OWNER) is None


async def test_member_without_grant_is_none(monkeypatch):
    _grants(monkeypatch, None)
    assert await _read(_session(_row()), MEMBER) is None


async def test_member_hash_differs_and_tracks_share_revision(monkeypatch):
    owner = await _read(_session(_row()), OWNER)
    _grants(monkeypatch, 1)
    m1 = await _read(_session(_row()), MEMBER)
    _grants(monkeypatch, 2)
    m2 = await _read(_session(_row()), MEMBER)
    assert owner and m1 and m2
    assert len({owner.visibility_hash, m1.visibility_hash, m2.visibility_hash}) == 3


async def test_local_only_flip_changes_hash():
    a = await _read(_session(_row(local_only=False)), OWNER)
    b = await _read(_session(_row(local_only=True)), OWNER)
    assert a and b and a.visibility_hash != b.visibility_hash


async def test_excerpt_is_stripped_and_bounded():
    got = await _read(_session(_row(head="x" * 5000)), OWNER)
    assert got is not None and len(got.fields["excerpt"]) == 2000
    empty = await _read(_session(_row(head=None)), OWNER)
    assert empty is not None and empty.fields["excerpt"] == ""


def test_registry_maps_document_to_existing_reader():
    from importlib import import_module

    module, reader, kw = inputs._OWNERS["document"]
    assert callable(getattr(import_module(module), reader)) and kw == "document_id"
