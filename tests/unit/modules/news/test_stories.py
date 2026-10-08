"""Unit tests for news story clustering, topic grouping, confidence thresholds, and deduplication.

Covers:
- URL canonicalization (_canonical_url): host normalization, default port stripping, fragment removal, query preservation, and credential rejection.
- Identity key generation (_identity_keys): deterministic URL and content hash keying for exact deduplication.
- Lexical tokenization (_tokens) and initial signal provenance structure (_initial_signal_provenance).
- Keyset cursor encoding/decoding (_encode_cursor, _decode_cursor) and filter hash binding.
- Story clustering (cluster_observation): replay idempotency, exact identity matching, and new story creation.
- Conservative fuzzy clustering thresholds (_fuzzy_candidate): 72-hour window limit, entity overlap requirement, local_only exclusion, and 0.92 cosine similarity threshold.
"""

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.news.models import (
    NewsObservation,
    NewsStory,
    NewsStoryIdentity,
)
from modules.news.schemas import (
    StoryCursor,
    StoryFilter,
)
from modules.news.stories import (
    ALGORITHM_VERSION,
    _canonical_url,
    _decode_cursor,
    _decode_detail_cursor,
    _encode_cursor,
    _encode_detail_cursor,
    _filter_hash,
    _fuzzy_candidate,
    _identity_keys,
    _initial_signal_provenance,
    _tokens,
    cluster_observation,
)

FENCE = AccessFence(workspace_id=uuid4(), user_id=1, membership_revision=2, configuration_revision=3)
SCOPE = InternalJobScope(workspace_id=FENCE.workspace_id, actor_user_id=1, membership_revision=2)
KW = {"scope": SCOPE, "multi_workspace_enabled": True}


class TestUrlCanonicalizationAndIdentityKeys:
    """Tests for canonical URL normalization, scheme filtering, and deduplication keys."""

    def test_canonical_url_strips_default_ports(self) -> None:
        """_canonical_url strips port 80 for http and port 443 for https."""
        assert _canonical_url("http://example.com:80/news") == "http://example.com/news"
        assert _canonical_url("https://example.com:443/article") == "https://example.com/article"

    def test_canonical_url_preserves_custom_port_and_query(self) -> None:
        """_canonical_url retains custom port, lowercase hostname, and query parameters while stripping fragments."""
        url = "HTTPS://Example.COM:8080/path?key=value#section"
        expected = "https://example.com:8080/path?key=value"
        assert _canonical_url(url) == expected

    def test_canonical_url_rejects_credentials_and_bad_schemes(self) -> None:
        """_canonical_url rejects URLs with embedded user/password and non-http schemes."""
        assert _canonical_url("https://user:pass@example.com/leak") is None
        assert _canonical_url("ftp://example.com/file") is None
        assert _canonical_url("javascript:alert(1)") is None
        assert _canonical_url(None) is None

    def test_identity_keys_deterministic(self) -> None:
        """_identity_keys generates deterministic sha256 keys for URL and content hash."""
        url = "https://example.com/news/1"
        content_hash = "abcdef123456"
        keys = _identity_keys(url, content_hash)
        assert len(keys) == 2
        assert keys[0][0] == "url"
        assert keys[1][0] == "hash"

        # Re-generating with identical inputs returns exact same hash keys
        keys_repeat = _identity_keys(url, content_hash)
        assert keys == keys_repeat

    def test_identity_keys_without_url(self) -> None:
        """_identity_keys generates only the content hash key when URL is absent or invalid."""
        keys = _identity_keys(None, "hash123")
        assert len(keys) == 1
        assert keys[0][0] == "hash"


class TestTokensAndSignalProvenance:
    """Tests for word token extraction and initial signal provenance."""

    def test_tokens_extraction(self) -> None:
        """_tokens extracts lowercase words between 2 and 64 characters, filtering single-char tokens."""
        text = "AI Agents & Autonomous Systems 2026: Revolutionizing Tech!"
        tokens = _tokens(text)
        assert "ai" in tokens
        assert "agents" in tokens
        assert "autonomous" in tokens
        assert "systems" in tokens
        assert "2026" in tokens
        assert "&" not in tokens

    def test_initial_signal_provenance_structure(self) -> None:
        """_initial_signal_provenance initializes all 7 signals with equal weights of 1/7."""
        provenance = _initial_signal_provenance()
        assert provenance["formula_version"] == 1
        weights: dict[str, float] = provenance["weights"]  # type: ignore[assignment]
        assert len(weights) == 7
        for name in ("topic", "entity", "goal", "project", "recency", "importance", "novelty"):
            assert name in weights
            assert abs(weights[name] - (1.0 / 7.0)) < 1e-6
            signal = provenance["signals"][name]  # type: ignore[index]
            assert signal["available"] is False
            assert signal["method"] == "not_evaluated_at_ingest"


class TestStoryCursorCodec:
    """Tests for story keyset cursor encoding, decoding, and filter tampering detection."""

    def test_story_cursor_roundtrip(self) -> None:
        """Valid story cursor encodes and decodes cleanly with matching owner and filter hash."""
        now = datetime.now(UTC)
        s_id = uuid4()
        filters = StoryFilter(source_ids=[s_id])
        fh = _filter_hash(filters)
        cursor = StoryCursor(
            domain="news_stories", workspace_id=FENCE.workspace_id, actor_user_id=1,
            membership_revision=2, configuration_revision=3,
            sort="observed_at_desc_story_id_desc",
            filter_hash=fh,
            as_of=now,
            after_observed_at=now,
            after_story_id=uuid4(),
            resolved_source_ids=[s_id],
            source_selection_incomplete=False,
        )
        encoded = _encode_cursor(cursor)
        decoded = _decode_cursor(encoded, FENCE, filters)
        assert decoded.actor_user_id == cursor.actor_user_id
        assert decoded.filter_hash == cursor.filter_hash
        assert decoded.after_story_id == cursor.after_story_id

    def test_story_cursor_owner_mismatch_raises_422(self) -> None:
        """_decode_cursor raises HTTP 422 if the membership revision is stale."""
        now = datetime.now(UTC)
        filters = StoryFilter()
        fh = _filter_hash(filters)
        cursor = StoryCursor(
            domain="news_stories", workspace_id=FENCE.workspace_id, actor_user_id=1,
            membership_revision=2, configuration_revision=3,
            sort="observed_at_desc_story_id_desc",
            filter_hash=fh,
            as_of=now,
            after_observed_at=now,
            after_story_id=uuid4(),
            resolved_source_ids=[],
            source_selection_incomplete=False,
        )
        encoded = _encode_cursor(cursor)
        with pytest.raises(HTTPException) as exc_info:
            _decode_cursor(encoded, AccessFence(workspace_id=FENCE.workspace_id, user_id=1, membership_revision=9,
                                                configuration_revision=3), filters)  # stale revision
        assert exc_info.value.status_code == 422
        assert "Invalid story cursor" in exc_info.value.detail


class TestStoryClustering:
    """Tests for cluster_observation idempotency, exact matching, and fuzzy thresholds."""

    def _sample_projection(self, doc_id: UUID | None = None, local_only: bool = False) -> SimpleNamespace:
        """Build a mock news document projection."""
        chunk = SimpleNamespace(id=uuid4(), content="First paragraph of the news article")
        now = datetime.now(UTC)
        return SimpleNamespace(
            document_id=doc_id or uuid4(),
            document_version_id=uuid4(),
            version_number=1,
            source_id=uuid4(),
            current_source_generation=1,
            source_name="Sample Source",
            source_type="rss",
            provider="rss",
            local_only=local_only,
            canonical_url="https://example.com/news/article-1",
            content_hash="abc123hash",
            provider_item_id="item-1",
            scope_discriminator=None,
            title="Tech Breakthrough Announced",
            published_at=now,
            observed_at=now,
            created_at=now,
            chunks=(chunk,),
            metadata_is_version_snapshot=False,
            accepted_record_hash="record_hash",
            normalization_version=1,
            chunk_count=1,
            chunks_truncated=False,
        )

    @pytest.mark.asyncio
    async def test_cluster_observation_projection_missing_returns_none(self) -> None:
        """cluster_observation returns None when get_news_document_projection returns None."""
        session = AsyncMock()
        with patch("modules.knowledge.documents.public.get_news_document_projection", return_value=None):
            result = await cluster_observation(session, document_id=uuid4(), expected_source_generation=1, **KW)
            assert result is None

    @pytest.mark.asyncio
    async def test_cluster_observation_idempotent_replay(self) -> None:
        """cluster_observation returns existing story_id without creating duplicate observations."""
        session = AsyncMock()
        proj = self._sample_projection()
        existing_story_id = uuid4()
        prior_obs = NewsObservation(
            story_id=existing_story_id,
            document_version_id=proj.document_version_id,
            source_generation=proj.current_source_generation,
            algorithm_version=ALGORITHM_VERSION,
        )
        session.scalar.return_value = prior_obs

        with patch("modules.knowledge.documents.public.get_news_document_projection", return_value=proj):
            result = await cluster_observation(session, document_id=proj.document_id, expected_source_generation=1, **KW)
            assert result == existing_story_id

    @pytest.mark.asyncio
    async def test_cluster_observation_exact_identity_match(self) -> None:
        """cluster_observation joins existing story when an exact URL identity matches."""
        session = AsyncMock()
        session.add = MagicMock()
        proj = self._sample_projection()
        existing_story_id = uuid4()

        existing_story = NewsStory(id=existing_story_id, algorithm_version=ALGORITHM_VERSION)
        existing_identity = NewsStoryIdentity(story_id=existing_story_id, identity_key="url_key", algorithm_version=ALGORITHM_VERSION)

        # 1st scalar: prior observation (None), 2nd scalar: existing identity, 3rd scalar: existing story
        session.scalar.side_effect = [
            None,               # prior NewsObservation
            existing_identity,  # NewsStoryIdentity
            existing_story,     # NewsStory
            None,               # identity check url
            None,               # identity check hash
        ]

        with patch("modules.knowledge.documents.public.get_news_document_projection", return_value=proj), \
             patch("modules.knowledge.entities.public.list_version_membership_refs", return_value=[]):
            result = await cluster_observation(session, document_id=proj.document_id, expected_source_generation=1, **KW)
            assert result == existing_story_id

    @pytest.mark.asyncio
    async def test_fuzzy_candidate_excludes_local_only(self) -> None:
        """_fuzzy_candidate returns None for local_only projections to prevent private data leakage."""
        session = AsyncMock()
        proj = self._sample_projection(local_only=True)
        res = await _fuzzy_candidate(
            session, projection=proj, current_entity_ids={"ent-1"}, observed_at=datetime.now(UTC), **KW  # type: ignore[arg-type]
        )
        assert res is None

    @pytest.mark.asyncio
    async def test_fuzzy_candidate_requires_entity_overlap(self) -> None:
        """_fuzzy_candidate returns None when there are no candidate entity IDs."""
        session = AsyncMock()
        proj = self._sample_projection(local_only=False)
        res = await _fuzzy_candidate(
            session, projection=proj, current_entity_ids=set(), observed_at=datetime.now(UTC), **KW  # type: ignore[arg-type]
        )
        assert res is None

    @pytest.mark.asyncio
    async def test_fuzzy_candidate_threshold_rejection(self) -> None:
        """_fuzzy_candidate rejects candidate when cosine similarity < FUZZY_THRESHOLD (0.92)."""
        session = AsyncMock()
        proj = self._sample_projection(local_only=False)
        now = datetime.now(UTC)

        other_obs = NewsObservation(
            id=uuid4(),
            story_id=uuid4(),
            document_id=uuid4(),
            document_version_id=uuid4(),
            chunk_id=uuid4(),
            source_id=uuid4(),
            source_generation=1,
            observed_at=now,
            local_only=False,
        )
        other_story = NewsStory(id=other_obs.story_id)

        mock_result = MagicMock()
        mock_result.all.return_value = [(other_obs, other_story)]
        session.execute.return_value = mock_result

        # Candidate projection mock
        candidate_proj = SimpleNamespace(
            document_version_id=other_obs.document_version_id,
            chunks=[SimpleNamespace(id=other_obs.chunk_id)],
        )

        entity_ref = SimpleNamespace(entity_id=UUID("00000000-0000-0000-0000-000000000001"))

        # Similarity score < 0.92 (e.g. 0.85)
        sim_item = SimpleNamespace(
            right_chunk_id=other_obs.chunk_id,
            cosine_similarity=0.85,  # Below threshold!
            generation_id=uuid4(),
            model_id="embed-v1",
            model_version="1",
            response_model_id="embed-v1",
            dimensions=1536,
            gateway_identity="gate",
        )
        similarity_res = SimpleNamespace(items=[sim_item], capability="search")

        with patch("modules.knowledge.documents.public.get_news_document_projection", return_value=candidate_proj), \
             patch("modules.knowledge.entities.public.list_version_membership_refs", return_value=[entity_ref]), \
             patch("modules.search.public.compare_news_evidence_embeddings", return_value=similarity_res):
            res = await _fuzzy_candidate(
                session,
                projection=proj,  # type: ignore[arg-type]
                current_entity_ids={"00000000-0000-0000-0000-000000000001"},
                observed_at=now, **KW,
            )
            assert res is None  # Below 0.92 threshold -> rejected!


class TestStoryDetailCursor:
    """N-F7: the Story detail evidence token binds workspace, actor, revisions, story and snapshot."""

    def _token(self):  # type: ignore[no-untyped-def]
        story_id = uuid4()
        sources = (uuid4(),)
        after = (uuid4(), uuid4(), uuid4())
        as_of = datetime.now(UTC)
        return story_id, sources, after, as_of, _encode_detail_cursor(FENCE, story_id, sources, as_of, after, True)

    def test_roundtrip(self) -> None:
        """A current-fence token decodes to the pinned sources, snapshot, position and flag."""
        story_id, sources, after, as_of, token = self._token()
        assert _decode_detail_cursor(token, FENCE, story_id, ()) == (sources, as_of, after, True)

    @pytest.mark.parametrize("field,value", [
        ("membership_revision", 9), ("configuration_revision", 9), ("user_id", 7), ("workspace_id", uuid4()),
    ])
    def test_stale_or_foreign_fence_rejected(self, field: str, value: object) -> None:
        """Revision, actor or workspace drift rejects the token before any position is applied."""
        story_id, _s, _a, _t, token = self._token()
        other = dataclasses.replace(FENCE, **{field: value})
        with pytest.raises(HTTPException) as exc:
            _decode_detail_cursor(token, other, story_id, ())
        assert exc.value.status_code == 422

    def test_other_story_and_source_mismatch_rejected(self) -> None:
        """A token cannot be replayed on another story or a different explicit source selection."""
        story_id, _s, _a, _t, token = self._token()
        for sid, srcs in ((uuid4(), ()), (story_id, (uuid4(),))):
            with pytest.raises(HTTPException):
                _decode_detail_cursor(token, FENCE, sid, srcs)

    def test_legacy_six_field_token_rejected(self) -> None:
        """The pre-cutover owner-only token format is no longer accepted."""
        import base64
        import json
        story_id = uuid4()
        raw = json.dumps({
            "owner_id": 1, "story_id": str(story_id), "source_ids": [], "as_of": datetime.now(UTC).isoformat(),
            "after": [str(uuid4())] * 3, "source_selection_incomplete": False,
        }, sort_keys=True, separators=(",", ":"))
        token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        with pytest.raises(HTTPException) as exc:
            _decode_detail_cursor(token, FENCE, story_id, ())
        assert exc.value.status_code == 422
