"""Unit tests for the per-Document Memory sweep's attribution of Source-scoped copies."""

from uuid import uuid4

from modules.knowledge.documents.public import (
    DocumentCleanupEvidenceIdentity,
    DocumentCleanupEvidenceScope,
)
from modules.memory.public import _document_provenance_match


def _scope() -> DocumentCleanupEvidenceScope:
    version, chunk = uuid4(), uuid4()
    return DocumentCleanupEvidenceScope(
        operation_id=uuid4(), source_id=uuid4(), document_id=uuid4(),
        references=(DocumentCleanupEvidenceIdentity(version, chunk, "chunk"),), next_cursor=None,
        workspace_id=uuid4(), actor_user_id=1,
    )


def test_source_scoped_copy_without_document_identity_is_not_attributable_to_the_document() -> None:
    scope = _scope()
    # The Source-wide sweep owns it: this Document's stage must skip it, not fail on it.
    assert _document_provenance_match({"source_id": str(scope.source_id)}, scope) == (False, False)


def test_source_scoped_copy_with_unreadable_or_partial_identity_stays_unresolved() -> None:
    scope = _scope()
    source = str(scope.source_id)
    assert _document_provenance_match({"source_id": source, "legacy": "x"}, scope) == (False, True)
    assert _document_provenance_match({"source_id": source, "document_id": None}, scope) == (False, True)
    assert _document_provenance_match({"source_id": source, "document_id": "not-a-uuid"}, scope) == (False, True)


def test_exact_identity_matches_and_other_document_is_never_unresolved() -> None:
    scope = _scope()
    reference = scope.references[0]
    exact = {"source_id": str(scope.source_id), "document_id": str(scope.document_id)}
    assert _document_provenance_match(exact, scope) == (True, False)
    assert _document_provenance_match({"chunk_id": str(reference.chunk_id)}, scope) == (True, False)
    other = {"source_id": str(scope.source_id), "document_id": str(uuid4())}
    assert _document_provenance_match(other, scope) == (False, False)
