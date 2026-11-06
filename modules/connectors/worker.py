"""Connector arq worker jobs facade.

The implementation lives in the private ``modules.connectors._worker_*`` submodules; every name is re-exported
here so ``apps/worker/main.py`` job references and tests keep the original ``modules.connectors.worker`` surface.
"""

from sqlalchemy import select as select

from core.realtime import commit_with_replay as commit_with_replay
from core.workspaces.public import read_access_fence as read_access_fence
from modules.connectors import provisioning as provisioning
from modules.connectors._worker_base import (
    _CURSOR_KEYS as _CURSOR_KEYS,
)
from modules.connectors._worker_base import (
    _DENIED as _DENIED,
)
from modules.connectors._worker_base import (
    _PASS_LIMIT as _PASS_LIMIT,
)
from modules.connectors._worker_base import (
    _admit_lineage as _admit_lineage,
)
from modules.connectors._worker_base import (
    _admit_source_job as _admit_source_job,
)
from modules.connectors._worker_base import (
    _cursor_ctx as _cursor_ctx,
)
from modules.connectors._worker_base import (
    _lineage as _lineage,
)
from modules.connectors._worker_base import (
    _page as _page,
)
from modules.connectors._worker_base import (
    _read_cursor as _read_cursor,
)
from modules.connectors._worker_base import (
    _read_slot_cursor as _read_slot_cursor,
)
from modules.connectors._worker_base import (
    _slot_state as _slot_state,
)
from modules.connectors._worker_base import (
    _write_cursor as _write_cursor,
)
from modules.connectors._worker_base import (
    _write_slot_cursor as _write_slot_cursor,
)
from modules.connectors._worker_base import (
    logger as logger,
)
from modules.connectors._worker_grants import (
    _COORDINATOR_STALE_AFTER as _COORDINATOR_STALE_AFTER,
)
from modules.connectors._worker_grants import (
    _DELETED_SOURCE_REVOKE as _DELETED_SOURCE_REVOKE,
)
from modules.connectors._worker_grants import (
    DIGEST_RETENTION as DIGEST_RETENTION,
)
from modules.connectors._worker_grants import (
    _claim_deleted_grant_revoke as _claim_deleted_grant_revoke,
)
from modules.connectors._worker_grants import (
    _delete_credential as _delete_credential,
)
from modules.connectors._worker_grants import (
    _DeletedGrantRevoke as _DeletedGrantRevoke,
)
from modules.connectors._worker_grants import (
    _idle_coordinator as _idle_coordinator,
)
from modules.connectors._worker_grants import (
    _open_grant_access_token as _open_grant_access_token,
)
from modules.connectors._worker_grants import (
    _record_deleted_grant_revoke as _record_deleted_grant_revoke,
)
from modules.connectors._worker_grants import (
    _settle_credential_delete as _settle_credential_delete,
)
from modules.connectors._worker_grants import (
    expire_github_webhook_details as expire_github_webhook_details,
)
from modules.connectors._worker_grants import (
    reclaim_github_webhook_digests as reclaim_github_webhook_digests,
)
from modules.connectors._worker_grants import (
    revoke_deleted_source_github_grants as revoke_deleted_source_github_grants,
)
from modules.connectors._worker_outbox import (
    _OUTBOX_OPEN_STATES as _OUTBOX_OPEN_STATES,
)
from modules.connectors._worker_outbox import (
    _admit_binding_target as _admit_binding_target,
)
from modules.connectors._worker_outbox import (
    _claim_due_hint as _claim_due_hint,
)
from modules.connectors._worker_outbox import (
    _complete_outbox_without_installation as _complete_outbox_without_installation,
)
from modules.connectors._worker_outbox import (
    _fan_out_page as _fan_out_page,
)
from modules.connectors._worker_outbox import (
    _recover_deferred_hints as _recover_deferred_hints,
)
from modules.connectors._worker_outbox import (
    _resolve_fanout_page as _resolve_fanout_page,
)
from modules.connectors._worker_outbox import (
    _settle_hint_after_wake as _settle_hint_after_wake,
)
from modules.connectors._worker_outbox import (
    _settle_outbox as _settle_outbox,
)
from modules.connectors._worker_outbox import (
    _snapshot_outbox as _snapshot_outbox,
)
from modules.connectors._worker_outbox import (
    _target_applies_to_binding as _target_applies_to_binding,
)
from modules.connectors._worker_outbox import (
    _wake_hint_source as _wake_hint_source,
)
from modules.connectors._worker_outbox import (
    _webhook_retry_delay as _webhook_retry_delay,
)
from modules.connectors._worker_outbox import (
    dispatch_github_webhooks as dispatch_github_webhooks,
)
from modules.connectors._worker_outbox import (
    process_collection_request as process_collection_request,
)
from modules.connectors._worker_reconcile import (
    _advance_transitions as _advance_transitions,
)
from modules.connectors._worker_reconcile import (
    _discover_created_workflow as _discover_created_workflow,
)
from modules.connectors._worker_reconcile import (
    _drive_workflow as _drive_workflow,
)
from modules.connectors._worker_reconcile import (
    _recover_unknown_create as _recover_unknown_create,
)
from modules.connectors._worker_reconcile import (
    _resume_activation as _resume_activation,
)
from modules.connectors._worker_reconcile import (
    reconcile_connectors as reconcile_connectors,
)
from modules.connectors.credentials import N8nCredentials as N8nCredentials
from modules.sources import (
    public as sources,  # noqa: F401  # re-exported: tests patch ``worker.sources``
)
