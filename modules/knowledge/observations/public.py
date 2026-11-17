"""Owner-scoped observation revision writes, selection and bounded reads."""

# Approved read projection (AGENTS.md "Testing Guidelines" / docs/architecture/module-boundaries.md): knowledge.observations owns
# Observation; other modules may build SELECT/JOIN statements on these columns and must never INSERT, UPDATE, DELETE,
# instantiate or add them (guarded by tests/unit/architecture/test_module_boundaries.py). Permission and deletion
# checks stay with the caller's own admission fence plus the owner's workspace/source predicates; the projection adds
# no access of its own.
from modules.knowledge.observations.models import Observation as ObservationReadProjection

# isort: split


from modules.knowledge.observations._cleanup import (
    _CLEANUP_LIMIT as _CLEANUP_LIMIT,
)
from modules.knowledge.observations._cleanup import (
    ObservationCleanupClosure as ObservationCleanupClosure,
)
from modules.knowledge.observations._cleanup import (
    _held_observation_ids as _held_observation_ids,
)
from modules.knowledge.observations._cleanup import (
    _require_cleanup_fences as _require_cleanup_fences,
)
from modules.knowledge.observations._cleanup import (
    current_observation_references as current_observation_references,
)
from modules.knowledge.observations._cleanup import (
    observation_cleanup_ids as observation_cleanup_ids,
)
from modules.knowledge.observations._cleanup import (
    prepare_document_cleanup_in_uow as prepare_document_cleanup_in_uow,
)
from modules.knowledge.observations._cleanup import (
    purge_document_in_uow as purge_document_in_uow,
)
from modules.knowledge.observations._cleanup import (
    purge_expired_noncurrent_observations as purge_expired_noncurrent_observations,
)
from modules.knowledge.observations._cleanup import (
    purge_source_in_uow as purge_source_in_uow,
)
from modules.knowledge.observations._common import (
    _admit as _admit,
)
from modules.knowledge.observations._export import (
    OBSERVATION_EXPORT_PAGE_MAX_BYTES as OBSERVATION_EXPORT_PAGE_MAX_BYTES,
)
from modules.knowledge.observations._export import (
    _decode_observation_export_cursor as _decode_observation_export_cursor,
)
from modules.knowledge.observations._export import (
    _observation_export_count as _observation_export_count,
)
from modules.knowledge.observations._export import (
    _observation_export_cursor as _observation_export_cursor,
)
from modules.knowledge.observations._export import (
    _observation_export_digest as _observation_export_digest,
)
from modules.knowledge.observations._export import (
    export_page as export_page,
)
from modules.knowledge.observations._export import (
    validate_export_fences as validate_export_fences,
)
from modules.knowledge.observations._ingest import (
    IngestionObservationAcceptedState as IngestionObservationAcceptedState,
)
from modules.knowledge.observations._ingest import (
    IngestionObservationPreparation as IngestionObservationPreparation,
)
from modules.knowledge.observations._ingest import (
    IngestionObservationSeriesState as IngestionObservationSeriesState,
)
from modules.knowledge.observations._ingest import (
    ObservationScopeRead as ObservationScopeRead,
)
from modules.knowledge.observations._ingest import (
    ObservationWriteResult as ObservationWriteResult,
)
from modules.knowledge.observations._ingest import (
    _apply_ingestion_observation as _apply_ingestion_observation,
)
from modules.knowledge.observations._ingest import (
    _observation_key_snapshot as _observation_key_snapshot,
)
from modules.knowledge.observations._ingest import (
    _observation_source_proof as _observation_source_proof,
)
from modules.knowledge.observations._ingest import (
    _validate_observation_key as _validate_observation_key,
)
from modules.knowledge.observations._ingest import (
    prepare_ingestion_observation_keys as prepare_ingestion_observation_keys,
)
from modules.knowledge.observations._ingest import (
    upsert_from_ingestion as upsert_from_ingestion,
)
from modules.knowledge.observations._ingest import (
    upsert_from_ingestion_in_uow as upsert_from_ingestion_in_uow,
)
from modules.knowledge.observations._reads import (
    _current_source_scopes as _current_source_scopes,
)
from modules.knowledge.observations._reads import (
    _cursor_fingerprint as _cursor_fingerprint,
)
from modules.knowledge.observations._reads import (
    _decode_cursor as _decode_cursor,
)
from modules.knowledge.observations._reads import (
    _filtered_window as _filtered_window,
)
from modules.knowledge.observations._reads import (
    _scan_evidence_page as _scan_evidence_page,
)
from modules.knowledge.observations._reads import (
    _ScanResult as _ScanResult,
)
from modules.knowledge.observations._reads import (
    _window_statement as _window_statement,
)
from modules.knowledge.observations._reads import (
    list_geospatial_observations as list_geospatial_observations,
)
from modules.knowledge.observations._reads import (
    list_observations as list_observations,
)
from modules.knowledge.observations.schemas import (
    ObservationQuery,
    WorldMeasurement,
)

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "ObservationQuery",
    "ObservationReadProjection",
    "WorldMeasurement",
]
