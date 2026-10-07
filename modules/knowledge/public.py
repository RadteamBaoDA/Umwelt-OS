"""Public knowledge composition surface consumed by API presentation routes."""

from modules.knowledge.documents.public import ChatEvidenceChunk
from modules.knowledge.service import KnowledgeService

__all__ = ["ChatEvidenceChunk", "KnowledgeService"]

