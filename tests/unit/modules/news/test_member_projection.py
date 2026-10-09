"""Member story projection: grant-first SQL, zero-grant invisibility, translation revision and visibility hash."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import literal, select
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, GrantRef, WorkspaceContext
from modules.news import stories
from modules.news.models import NewsObservation
from modules.news.schemas import StoryEvidence, StoryFilter, StoryRead

WS = uuid4()
MEMBER = WorkspaceContext(user_id=7, workspace_id=WS, role="member", membership_revision=3)
FENCE = AccessFence(workspace_id=WS, user_id=7, membership_revision=3, configuration_revision=1)
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _grant_select(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
    return select(literal(uuid4()).label("id"))


def _evidence(title: str = "T", excerpt: str = "E") -> StoryEvidence:
    return StoryEvidence(
        document_id=uuid4(), document_version_id=uuid4(), chunk_id=uuid4(), source_id=uuid4(),
        source_name="s", source_type="rss", provider=None, url=None, title=title, excerpt=excerpt,
        observed_at=NOW, published_at=None,
    )


class FakeSession:
    """Queue canned results and record the statements the code under test builds."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return self.results.pop(0)


def _rows(rows: list[object]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    result.one.side_effect = lambda: rows[0]
    return result


def test_grant_subquery_precedes_limit_in_compiled_sql() -> None:
    with patch.object(stories.workspaces, "granted_resource_ids", _grant_select):
        sql = str(stories._member_obs(MEMBER, NOW, (), NewsObservation.story_id).limit(5).compile(
            dialect=postgresql.dialect()))
    assert "news_observations.document_id IN (SELECT" in sql
    assert sql.index("document_id IN (SELECT") < sql.index("LIMIT")


@pytest.mark.asyncio
async def test_zero_grant_story_is_none() -> None:
    session = FakeSession(_rows([SimpleNamespace(n=0, s=0, latest=None)]))
    with patch.object(stories.workspaces, "granted_resource_ids", _grant_select):
        assert await stories._member_get_story(session, uuid4(), (), MEMBER, FENCE, 100, None) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_member_list_uses_authorized_title_and_counts_only() -> None:
    story_id = uuid4()
    obs = SimpleNamespace(
        story_id=story_id, document_id=uuid4(), document_version_id=uuid4(), chunk_id=uuid4(), source_id=uuid4(),
        provider=None, canonical_url=None, title="Visible title", excerpt="visible", observed_at=NOW,
        published_at=None,
    )
    session = FakeSession(
        _rows([SimpleNamespace(story_id=story_id, latest=NOW)]),
        _rows([(obs, SimpleNamespace(name="feed", type="rss"))]),
        _rows([SimpleNamespace(story_id=story_id, evidence_count=1, source_count=1)]),
    )
    with patch.object(stories.workspaces, "granted_resource_ids", _grant_select):
        page = await stories._member_list_stories(
            session, StoryFilter(), MEMBER, FENCE, None, NOW)  # type: ignore[arg-type]
    item = page.items[0]
    assert (item.title, item.evidence_count, item.relevance, item.why_relevant) == ("Visible title", 1, None, [])
    assert item.translation_revision == stories._translation_revision(story_id, item.evidence[0])


@pytest.mark.asyncio
async def test_member_topic_filter_forbidden() -> None:
    with pytest.raises(HTTPException) as exc:
        await stories._member_list_stories(
            FakeSession(), StoryFilter(topic_id=uuid4()), MEMBER, FENCE, None, NOW)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


def test_translation_revision_tracks_version_and_text() -> None:
    story = uuid4()
    base = _evidence()
    revision = stories._translation_revision(story, base)
    assert len(revision) == 32
    assert revision == stories._translation_revision(story, base)
    assert revision != stories._translation_revision(story, base.model_copy(update={"document_version_id": uuid4()}))
    assert revision != stories._translation_revision(story, base.model_copy(update={"title": "other"}))


@pytest.mark.asyncio
async def test_visibility_hash_changes_with_grant_and_membership_revision() -> None:
    ev = _evidence()
    story = StoryRead(id=uuid4(), title="T", excerpt="E", observed_at=NOW, source_count=1, evidence_count=1,
                      evidence=[ev], translation_revision="r" * 32)
    row = SimpleNamespace(document_id=ev.document_id, document_version_id=ev.document_version_id,
                          source_id=ev.source_id, source_generation=1, local_only=False)

    async def run(grant_revision: int, scope: WorkspaceContext) -> str:
        grant = GrantRef(resource_type="document", resource_id=ev.document_id,
                         share_revision=grant_revision, resource_revision=1)
        detail = SimpleNamespace(story=story, evidence_cursor=None)
        with patch.object(stories, "get_story", AsyncMock(return_value=detail)), \
                patch.object(stories.workspaces, "read_resource_grants", AsyncMock(return_value=(grant,))):
            value = await stories.read_story_translation_input(
                FakeSession(_rows([row])), scope=scope, story_id=story.id,  # type: ignore[arg-type]
                multi_workspace_enabled=False)
        assert value is not None and value.fields == {"title": "T", "excerpt": "E"}
        return value.visibility_hash

    base = await run(1, MEMBER)
    assert base == await run(1, MEMBER)
    assert base != await run(2, MEMBER)
    assert base != await run(1, WorkspaceContext(user_id=7, workspace_id=WS, role="member", membership_revision=4))
