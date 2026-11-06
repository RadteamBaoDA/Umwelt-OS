# Privacy and data handling

The current bilingual draft privacy notice is in [English](legal/en-us/privacy.md) and [Vietnamese](legal/vi-vi/privacy.md). Public pages are `/privacy`, `/terms` and `/refunds`. The [service and data-flow inventory](third-party-services.md) records implementation evidence and activation limits.

These drafts require the operator to replace **[LEGAL NAME]**, **[CONTACT EMAIL]** and **[JURISDICTION]**, and complete legal review before offering the deployment to others. Host backups, deployment secrets and external provider retention need separate operator handling.

The [account-erasure coverage map](account-erasure-coverage.md) assigns every registered SQL table to an owner, finalizer or explicit shared/operational retention rule. Local test and build evidence does not establish runtime erasure on a deployed instance.

## Privacy advisory lock (per workspace, shared/exclusive)

`modules/memory/public.py::lock_export_privacy(session, workspace_id, *, shared=False)` serializes consent
reads and publications with privacy writes and erasure. The lock is a transaction-scoped Postgres advisory
lock on the two-int key `(1297109577, int32(sha256("privacy:<workspace uuid>")[:4]))` (`privacy_lock_key`).

- Different workspaces never contend (a 32-bit collision only adds harmless extra contention).
- **Readers** (`shared=True`, `pg_advisory_xact_lock_shared`) need a consistent consent read or must stop
  publishing once consent is revoked. They do not block each other.
- **Writers/erasers** (default, `pg_advisory_xact_lock`) change privacy state or delete/purge consented data.
  They block every reader and writer of the same workspace. A writer must never take the shared form, and a
  transaction must not take shared and then exclusive on the same key (lock upgrade; it is logged as
  `privacy_lock_upgrade_shared_to_exclusive`).
- `expire_stale_requests` spans workspaces and uses `lock_export_privacy_workspaces`, which takes every
  affected key in ascending key order (deadlock-free). Account erasure/export do NOT call it: they run through
  the per-workspace module finalizers, each of which locks ONE workspace key per transaction and commits before
  the next. A transaction never holds two privacy keys there, so no lock-order cycle can form.
- Known limitation: `MemoryService._read_privacy_config_under_fence` (used by `get_active_memory_context` and the
  novelty evaluator) takes the exclusive form because the same path can insert the default row and its
  callers pass the fence on to fenced writes. These consent readers therefore still serialize against other
  readers of that one workspace (never across workspaces). Splitting it into shared-read plus exclusive-on-absent
  is deferred.
- Lock order is unchanged: privacy, then Source/Document/parent rows, then run rows.

`tests/unit/modules/memory/test_privacy_lock_scoping.py` parses every call site (AST) and fails when a new one
is not classified in the table below or a writer passes `shared=True`.

| Call site | Role | Mode |
| --- | --- | --- |
| agents/_harness_steps `_lock_native_output_fences_for_publication` | publication fence | shared |
| agents/_public_cleanup `_cleanup_finalized_marker`, `_cleanup_unavailable_marker`, `_cleanup_phase_a`, `_cleanup_phase_b` | copied-evidence erasure | exclusive |
| chat/public `delete_conversation`, `purge_document_copied_evidence_page` | erasure | exclusive |
| chat/public `read_memory_export_origin` | consent read | shared |
| chat/routes `create_conversation`, `send_message`, `mutate_message`, `_locked_poll`, `cancel_response` (2 sites) | consent-gated publish/read | shared |
| chat/user_input `expire_stale_requests` (all affected workspaces, sorted), `await_user_input` | consent-gated run update | shared |
| chat/worker `is_history_storage_enabled`, `_require_privacy_fence`, `_release_on_shutdown`, `_mark_cancelled`, `_mark_privacy_cancelled`, `_mark_failed`, `_fail_expired_pending` | consent-gated publish/redact | shared |
| chat/worker `_purge_expired_conversations`, `_purge_expired_activity_links`, `_purge_expired_runs` | expiry erasure | exclusive |
| knowledge/documents/worker `_advance_raw_document_cleanup`, `_evict_memory_cache_after_commit`, `_advance_copied_cleanup`, `_recover_attempt` (via `lock_export_privacy_in_uow`) | cleanup erasure | exclusive |
| sources/worker `_lock_operation` (via `lock_export_privacy_in_uow`) | purge | exclusive |
| memory `export_page`, `validate_export_fences`, `_verified_memory_read`, `_verified_candidate_read`, `get_memories`, `get_memory`, `get_candidates` | consistent read | shared |
| memory `create_memory`, `update_memory`, `forget_memory`, `invalidate_memory`, `supersede_memory`, `accept_candidate`, `reject_candidate`, `purge_memories` | memory mutation/erasure | exclusive |
| memory `get_privacy_config` (default insert), `_read_privacy_config_under_fence`, `update_privacy_config` | privacy state writer | exclusive |
