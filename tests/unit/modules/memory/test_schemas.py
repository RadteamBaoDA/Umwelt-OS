"""Unit tests for memory module schemas, memory types, and importance score calculations.

Covers:
- Memory schemas (MemoryCreate, MemoryUpdate, MemoryRead, MemoryCandidateCreate, MemoryCandidateRead)
- MemoryType enumeration and validation ('fact', 'preference', 'instruction', 'decision', 'procedural')
- Privacy configurations and purge requests (MemoryPrivacyConfig, MemoryPurgeRequest)
- Importance score calculations (evaluate_candidate, novelty, usefulness, confidence)
- Tokenization, Jaccard similarity, and candidate deduplication
- Automatic acceptance gating and candidate extraction from text
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.memory.schemas import (
    MAX_MEMORY_CONTENT_LENGTH,
    MAX_REASON_LENGTH,
    MemoryCandidateCreate,
    MemoryCandidateRead,
    MemoryCandidateRejectRequest,
    MemoryCreate,
    MemoryForgetRequest,
    MemoryInvalidateRequest,
    MemoryPrivacyConfig,
    MemoryPrivacyUpdate,
    MemoryProvenance,
    MemoryPurgeRequest,
    MemoryPurgeResponse,
    MemoryRead,
    MemorySupersedeRequest,
    MemoryType,
    MemoryUpdate,
)
from modules.memory.selection import (
    CandidateEvaluation,
    _jaccard_similarity,
    _tokenize,
    evaluate_candidate,
    extract_candidate_proposals,
)


class TestMemorySchemas:
    """Tests for Memory request/response DTOs, bounds, and MemoryType."""

    def test_memory_types_valid(self) -> None:
        """Verify all valid MemoryType literals: fact, preference, instruction, decision, procedural."""
        for m_type in ["fact", "preference", "instruction", "decision", "procedural"]:
            mem = MemoryCreate(content="Testing memory type", type=m_type)  # type: ignore[arg-type]
            assert mem.type == m_type

    def test_memory_type_invalid(self) -> None:
        """Invalid memory types must be rejected."""
        with pytest.raises(ValidationError):
            MemoryCreate(content="Testing memory", type="opinion")  # type: ignore[arg-type]

    def test_memory_create_bounds(self) -> None:
        """Verify length bounds and confidence validation on MemoryCreate."""
        # Empty content rejected
        with pytest.raises(ValidationError):
            MemoryCreate(content="")

        # Oversized content rejected
        with pytest.raises(ValidationError):
            MemoryCreate(content="x" * (MAX_MEMORY_CONTENT_LENGTH + 1))

        # Confidence bounds [0.0, 1.0]
        with pytest.raises(ValidationError):
            MemoryCreate(content="Valid", confidence=1.5)
        with pytest.raises(ValidationError):
            MemoryCreate(content="Valid", confidence=-0.1)

        # Extra fields forbidden
        with pytest.raises(ValidationError):
            MemoryCreate.model_validate({"content": "Valid", "extra_prop": 123})

    def test_memory_update_patch_validation(self) -> None:
        """Verify MemoryUpdate patch payload."""
        update = MemoryUpdate(confidence=0.85, reason="Refined confidence")
        assert update.confidence == 0.85
        assert update.content is None

        with pytest.raises(ValidationError):
            MemoryUpdate(confidence=2.0)

    def test_memory_read_projection(self) -> None:
        """Verify MemoryRead projection from attributes."""
        mem_id = uuid4()
        now = datetime.now(UTC)
        read = MemoryRead(
            id=mem_id,
            content="User prefers dark theme",
            memory_type="preference",
            provenance={"source": "settings"},
            confidence=0.95,
            reason="Explicit user toggle",
            status="active",
            is_manual=True,
            created_at=now,
            updated_at=now,
        )
        assert read.id == mem_id
        assert read.type == "preference"
        assert read.status == "active"
        assert read.is_manual is True

    def test_memory_candidate_schemas(self) -> None:
        """Verify MemoryCandidateCreate and MemoryCandidateRead schemas."""
        cand_id = uuid4()
        now = datetime.now(UTC)

        cand_create = MemoryCandidateCreate(
            content="Working on project Umwelt-OS",
            type="fact",
            confidence=0.8,
        )
        assert cand_create.content == "Working on project Umwelt-OS"
        assert cand_create.type == "fact"

        cand_read = MemoryCandidateRead(
            id=cand_id,
            content="Working on project Umwelt-OS",
            memory_type="fact",
            provenance={},
            confidence=0.8,
            novelty_score=0.9,
            usefulness_score=0.85,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        assert cand_read.novelty_score == 0.9
        assert cand_read.usefulness_score == 0.85
        assert cand_read.status == "pending"

    def test_memory_privacy_config_defaults(self) -> None:
        """Verify MemoryPrivacyConfig default security settings."""
        config = MemoryPrivacyConfig()
        # History enabled by default, but agent memory and auto-accept are strictly opt-in
        assert config.store_conversation_history is True
        assert config.store_agent_memory is False
        assert config.auto_accept_memory is False

    def test_memory_purge_request_and_response(self) -> None:
        """Verify purge request and response structures."""
        req = MemoryPurgeRequest(purge_forgotten_memories=True, purge_rejected_candidates=True)
        assert req.purge_forgotten_memories is True
        assert req.purge_conversation_history is False

        res = MemoryPurgeResponse(purged_memories_count=5, purged_candidates_count=12)
        assert res.purged_memories_count == 5
        assert res.purged_candidates_count == 12


class TestImportanceScoreCalculations:
    """Tests for novelty, usefulness, confidence, and auto-acceptance math in selection.py."""

    def test_tokenize_stopwords_and_length(self) -> None:
        """Verify _tokenize filters out common stopwords and extracts clean word tokens."""
        text = "This is a test of the emergency broadcast system"
        tokens = _tokenize(text)
        assert "this" not in tokens
        assert "is" not in tokens
        assert "the" not in tokens
        assert "of" not in tokens
        assert "test" in tokens
        assert "emergency" in tokens
        assert "broadcast" in tokens
        assert "system" in tokens

    def test_jaccard_similarity_calculation(self) -> None:
        """Verify Jaccard similarity coefficient math: intersection / union."""
        set_a = {"apple", "banana", "cherry"}
        set_b = {"banana", "cherry", "date"}
        # intersection = {banana, cherry} (2), union = {apple, banana, cherry, date} (4) -> 2/4 = 0.5
        assert _jaccard_similarity(set_a, set_b) == 0.5
        assert _jaccard_similarity(set(), set_b) == 0.0
        assert _jaccard_similarity(set_a, set()) == 0.0
        assert _jaccard_similarity(set_a, set_a) == 1.0

    def test_novelty_score_high_overlap_duplicate(self) -> None:
        """High overlap (>= 0.80) flags candidate as duplicate with low novelty."""
        existing = ["I prefer dark theme in IDE and terminal"]
        candidate = "I always prefer dark theme in IDE and terminal"
        eval_result = evaluate_candidate(candidate, "preference", existing)

        assert eval_result.is_duplicate is True
        assert eval_result.novelty_score <= 0.20
        assert "High overlap" in eval_result.reason

    def test_novelty_score_moderate_overlap(self) -> None:
        """Moderate overlap (0.50 - 0.79) adds context without flagging as duplicate."""
        existing = ["Project Alpha uses Python and FastAPI"]
        candidate = "Project Alpha uses Python, FastAPI and Postgres database"
        eval_result = evaluate_candidate(candidate, "fact", existing)

        assert eval_result.is_duplicate is False
        assert 0.40 <= eval_result.novelty_score <= 0.80
        assert "Related to existing memory" in eval_result.reason

    def test_novelty_score_novel_information(self) -> None:
        """Low overlap (< 0.50) is marked fully novel."""
        existing = ["I like tea in the morning"]
        candidate = "Next week we fly to Tokyo for the conference"
        eval_result = evaluate_candidate(candidate, "fact", existing)

        assert eval_result.is_duplicate is False
        assert eval_result.novelty_score == 1.0
        assert "Novel information" in eval_result.reason

    def test_usefulness_score_transient_patterns(self) -> None:
        """Transient conversational utterances receive low usefulness (0.1)."""
        transient_phrases = [
            "Hello there!",
            "Thank you very much",
            "yes sure got it",
            "What time is the meeting?",
            "ping test foo",
        ]
        for phrase in transient_phrases:
            eval_result = evaluate_candidate(phrase, "fact", [])
            assert eval_result.usefulness_score == 0.1
            assert "Transient" in eval_result.reason

    def test_usefulness_score_high_value_patterns(self) -> None:
        """Owner preferences (0.95), instructions (0.9), and facts (0.85) receive high usefulness."""
        pref = evaluate_candidate("I prefer to write asynchronous Python code", "preference", [])
        assert pref.usefulness_score == 0.95

        inst = evaluate_candidate("Always respond with concise summaries", "instruction", [])
        assert inst.usefulness_score == 0.90

        fact = evaluate_candidate("My role is lead software architect", "fact", [])
        assert fact.usefulness_score == 0.85

    def test_confidence_score_speculative_vs_direct(self) -> None:
        """Speculative phrases reduce confidence (0.4), while direct statements yield high confidence (0.95)."""
        speculative = evaluate_candidate("Maybe we might launch on Friday perhaps", "fact", [])
        assert speculative.confidence_score == 0.40

        direct = evaluate_candidate("My name is Alex and I live in Seattle", "fact", [])
        assert direct.confidence_score == 0.95

    def test_auto_accept_logic(self) -> None:
        """Auto-accept requires explicit owner opt-in and strict score thresholds."""
        high_quality = "I prefer dark mode across all interfaces"

        # By default auto_accept_enabled is False -> should_auto_accept must be False
        eval_default = evaluate_candidate(high_quality, "preference", [], auto_accept_enabled=False)
        assert eval_default.should_auto_accept is False

        # When auto_accept_enabled is True and thresholds are met (novelty >= 0.7, usefulness >= 0.75, confidence >= 0.85)
        eval_opt_in = evaluate_candidate(high_quality, "preference", [], auto_accept_enabled=True)
        assert eval_opt_in.novelty_score >= 0.7
        assert eval_opt_in.usefulness_score >= 0.75
        assert eval_opt_in.confidence_score >= 0.85
        assert eval_opt_in.should_auto_accept is True

        # When candidate is a duplicate, should_auto_accept is always False even if opt-in is True
        existing = [high_quality]
        eval_dup = evaluate_candidate(high_quality, "preference", existing, auto_accept_enabled=True)
        assert eval_dup.is_duplicate is True
        assert eval_dup.should_auto_accept is False

    def test_extract_candidate_proposals(self) -> None:
        """Verify extraction of candidate proposals from conversational text."""
        conversation = (
            "Hi there! How are you doing? "
            "I prefer to use TypeScript for web frontends. "
            "Always respond in Markdown format. "
            "My name is John. "
            "Thanks for your help!"
        )
        proposals = extract_candidate_proposals(conversation)
        types = [p["type"] for p in proposals]
        assert "preference" in types
        assert "instruction" in types
        assert "fact" in types
        # Transient phrases ("Hi there!", "Thanks for your help!") must be excluded
        contents = " ".join(p["content"] for p in proposals)
        assert "Hi there" not in contents
        assert "Thanks for your help" not in contents
