"""Ingestion arq worker jobs facade.

The implementation lives in the private ``modules.ingestion._worker_*`` submodules; every name is re-exported
here so ``apps/worker/main.py`` job references and tests keep the original ``modules.ingestion.worker`` surface.
"""

from __future__ import annotations

from modules.connectors import public as connectors
from modules.ingestion import public as ingestion_api
from modules.ingestion._worker_collect import (
    ConnectorRetryError as ConnectorRetryError,
)
from modules.ingestion._worker_collect import (
    _collect_web_job as _collect_web_job,
)
from modules.ingestion._worker_collect import (
    _commit_ingestion_change as _commit_ingestion_change,
)
from modules.ingestion._worker_collect import (
    _prepare_crawl_records as _prepare_crawl_records,
)
from modules.ingestion._worker_collect import (
    _refresh_run_status as _refresh_run_status,
)
from modules.ingestion._worker_collect import (
    _retry_after_seconds as _retry_after_seconds,
)
from modules.ingestion._worker_collect import (
    _update_native_run_lease as _update_native_run_lease,
)
from modules.ingestion._worker_collect import (
    _wake_dispatcher_after_commit as _wake_dispatcher_after_commit,
)
from modules.ingestion._worker_lock import (
    _admit_worker_delivery as _admit_worker_delivery,
)
from modules.ingestion._worker_lock import (
    _capture_normalization_claim as _capture_normalization_claim,
)
from modules.ingestion._worker_lock import (
    _capture_normalization_retry_claim_in_uow as _capture_normalization_retry_claim_in_uow,
)
from modules.ingestion._worker_lock import (
    _claim_matches as _claim_matches,
)
from modules.ingestion._worker_lock import (
    _discover_normalization as _discover_normalization,
)
from modules.ingestion._worker_lock import (
    _lock_normalization_retry_event as _lock_normalization_retry_event,
)
from modules.ingestion._worker_lock import (
    _lock_normalization_rows as _lock_normalization_rows,
)
from modules.ingestion._worker_lock import (
    _lock_run_batch_stage as _lock_run_batch_stage,
)
from modules.ingestion._worker_lock import (
    _lock_worker_event as _lock_worker_event,
)
from modules.ingestion._worker_lock import (
    _lock_worker_outbox as _lock_worker_outbox,
)
from modules.ingestion._worker_lock import (
    _lock_worker_source as _lock_worker_source,
)
from modules.ingestion._worker_lock import (
    _prepare_upload_document as _prepare_upload_document,
)
from modules.ingestion._worker_lock import (
    _raise_preparation_conflict as _raise_preparation_conflict,
)
from modules.ingestion._worker_normalize import (
    _apply_owner_effects as _apply_owner_effects,
)
from modules.ingestion._worker_normalize import (
    _ClaimBox as _ClaimBox,
)
from modules.ingestion._worker_normalize import (
    _count_disposition as _count_disposition,
)
from modules.ingestion._worker_normalize import (
    _defer_until_observations_enabled as _defer_until_observations_enabled,
)
from modules.ingestion._worker_normalize import (
    _normalization_filters as _normalization_filters,
)
from modules.ingestion._worker_normalize import (
    _normalize_row as _normalize_row,
)
from modules.ingestion._worker_normalize import (
    _normalize_rows as _normalize_rows,
)
from modules.ingestion._worker_normalize import (
    _normalize_slice as _normalize_slice,
)
from modules.ingestion._worker_normalize import (
    _prepare_row as _prepare_row,
)
from modules.ingestion._worker_normalize import (
    _PreparedRow as _PreparedRow,
)
from modules.ingestion._worker_normalize import (
    _publish_ready_events as _publish_ready_events,
)
from modules.ingestion._worker_normalize import (
    _settle_failure as _settle_failure,
)
from modules.ingestion._worker_normalize import (
    _settle_stage as _settle_stage,
)
from modules.ingestion._worker_normalize import (
    _settle_terminal_run as _settle_terminal_run,
)
from modules.ingestion._worker_normalize import (
    _SliceState as _SliceState,
)
from modules.ingestion._worker_normalize import (
    _write_world_observation as _write_world_observation,
)
from modules.ingestion._worker_normalize import (
    process_normalize_event as process_normalize_event,
)
from modules.ingestion._worker_stage import (
    _claim_ingestion_stage as _claim_ingestion_stage,
)
from modules.ingestion._worker_stage import (
    _complete_ingestion_stage as _complete_ingestion_stage,
)
from modules.ingestion._worker_stage import (
    _fail_ingestion_stage as _fail_ingestion_stage,
)
from modules.ingestion._worker_stage import (
    _ingestion_enabled as _ingestion_enabled,
)
from modules.ingestion._worker_stage import (
    _schedule_transient_retry as _schedule_transient_retry,
)
from modules.ingestion._worker_stage import (
    process_ingestion_event as process_ingestion_event,
)
from modules.ingestion._worker_state import (
    MAX_STAGE_ATTEMPTS as MAX_STAGE_ATTEMPTS,
)
from modules.ingestion._worker_state import (
    NORMALIZATION_BATCH_BYTES as NORMALIZATION_BATCH_BYTES,
)
from modules.ingestion._worker_state import (
    NORMALIZATION_BATCH_RECORDS as NORMALIZATION_BATCH_RECORDS,
)
from modules.ingestion._worker_state import (
    NORMALIZATION_VERSION as NORMALIZATION_VERSION,
)
from modules.ingestion._worker_state import (
    STAGE_TIMEOUT_SECONDS as STAGE_TIMEOUT_SECONDS,
)
from modules.ingestion._worker_state import (
    NormalizationCandidate as NormalizationCandidate,
)
from modules.ingestion._worker_state import (
    NormalizationPreparationConflict as NormalizationPreparationConflict,
)
from modules.ingestion._worker_state import (
    WorkerClaim as WorkerClaim,
)
from modules.ingestion._worker_state import (
    WorkerState as WorkerState,
)
from modules.ingestion._worker_state import (
    _Admitted as _Admitted,
)
from modules.ingestion._worker_state import (
    _capture_worker_claim as _capture_worker_claim,
)
from modules.ingestion._worker_state import (
    _Discovery as _Discovery,
)
from modules.ingestion._worker_state import (
    _same_worker_claim as _same_worker_claim,
)
from modules.ingestion._worker_state import (
    logger as logger,
)
from modules.ingestion._worker_upload import (
    _cap_parsed_text as _cap_parsed_text,
)
from modules.ingestion._worker_upload import (
    _claim_upload_extraction as _claim_upload_extraction,
)
from modules.ingestion._worker_upload import (
    _failure_code as _failure_code,
)
from modules.ingestion._worker_upload import (
    _publish_extraction as _publish_extraction,
)
from modules.ingestion._worker_upload import (
    _record_extraction_failure as _record_extraction_failure,
)
from modules.ingestion._worker_upload import (
    _UploadClaim as _UploadClaim,
)
from modules.ingestion._worker_upload import (
    cleanup_storage_orphans as cleanup_storage_orphans,
)
from modules.ingestion._worker_upload import (
    process_uploaded_file as process_uploaded_file,
)
from modules.ingestion._worker_upload import (
    purge_collection_receipts as purge_collection_receipts,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.observations import public as observations
from modules.sources import public as sources

__all__ = [
    "connectors",
    "documents",
    "ingestion_api",
    "observations",
    "sources",
    "logger",
    "STAGE_TIMEOUT_SECONDS",
    "MAX_STAGE_ATTEMPTS",
    "NORMALIZATION_VERSION",
    "NORMALIZATION_BATCH_RECORDS",
    "NORMALIZATION_BATCH_BYTES",
    "WorkerClaim",
    "NormalizationCandidate",
    "NormalizationPreparationConflict",
    "WorkerState",
    "_capture_worker_claim",
    "_same_worker_claim",
    "_Admitted",
    "_Discovery",
    "_capture_normalization_retry_claim_in_uow",
    "_lock_normalization_retry_event",
    "_admit_worker_delivery",
    "_lock_worker_source",
    "_prepare_upload_document",
    "_discover_normalization",
    "_lock_run_batch_stage",
    "_lock_normalization_rows",
    "_lock_worker_outbox",
    "_lock_worker_event",
    "_claim_matches",
    "_raise_preparation_conflict",
    "_capture_normalization_claim",
    "_wake_dispatcher_after_commit",
    "_commit_ingestion_change",
    "_refresh_run_status",
    "ConnectorRetryError",
    "_update_native_run_lease",
    "_retry_after_seconds",
    "_prepare_crawl_records",
    "_collect_web_job",
    "_fail_ingestion_stage",
    "_ingestion_enabled",
    "_claim_ingestion_stage",
    "_schedule_transient_retry",
    "_complete_ingestion_stage",
    "process_ingestion_event",
    "_ClaimBox",
    "_SliceState",
    "_PreparedRow",
    "_prepare_row",
    "_write_world_observation",
    "_publish_ready_events",
    "_apply_owner_effects",
    "_normalize_row",
    "_normalize_rows",
    "_normalization_filters",
    "_count_disposition",
    "_settle_terminal_run",
    "_settle_stage",
    "_defer_until_observations_enabled",
    "_normalize_slice",
    "_settle_failure",
    "process_normalize_event",
    "_UploadClaim",
    "_claim_upload_extraction",
    "_publish_extraction",
    "_record_extraction_failure",
    "process_uploaded_file",
    "_cap_parsed_text",
    "_failure_code",
    "cleanup_storage_orphans",
    "purge_collection_receipts",
]
