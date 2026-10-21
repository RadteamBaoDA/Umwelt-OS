# Account erasure persistence coverage

Reviewed 21 October 2026. This is the registered SQL model inventory, not evidence of a live database migration or runtime erasure. The coverage test rejects missing/duplicate ownership. Ordinary owners use authorized, bounded, child-first deletion and final absence checks; peer content outside the predicate survives. Remote, graph, file, checkpoint and cache stages precede SQL removal.

| Table | Owning contract | Cleanup or retention rule |
| --- | --- | --- |
| `account_deletion_operations` | [core/auth/deletion_worker.py](../core/auth/deletion_worker.py) | Clear personal scope/cursor on success; retain opaque receipt/status seven days. Failed operations retain authority for retry. |
| `agent_approvals` | [modules/agents/erasure.py](../modules/agents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `agent_browser_grants` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `agent_effects` | [modules/agents/erasure.py](../modules/agents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `agent_evidence_cleanups` | [modules/agents/erasure.py](../modules/agents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `agent_profile_revisions` | [modules/agents/erasure.py](../modules/agents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `agent_profiles` | [modules/agents/erasure.py](../modules/agents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `agent_runs` | [modules/agents/erasure.py](../modules/agents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `agent_tool_calls` | [modules/agents/erasure.py](../modules/agents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `ai_settings` | [modules/settings/erasure.py](../modules/settings/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `auth_session` | [core/auth/deletion_worker.py](../core/auth/deletion_worker.py) | Revoke all sessions atomically on acceptance; delete residual rows at finalization. |
| `automation_cursors` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `automation_revisions` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `automation_run_actions` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `automation_runs` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `automation_schedules` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `automation_triggers` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `automation_webhook_credentials` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `automations` | [modules/automations/erasure.py](../modules/automations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `brief_schedules` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `browser_page_evidence` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `browser_read_jobs` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_agent_activity_links` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_conversations` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_message_mutation_receipts` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_messages` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_response_runs` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_stream_events` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_templates` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `chat_todos` | [modules/chat/erasure.py](../modules/chat/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `collector_credentials` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_admission_slots` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Clear owned slot fields; retain empty/global capacity and peer reservations. |
| `connector_collection_requests` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_managed_credentials` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_native_credentials` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_provider_sends` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_provider_terms` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_provisioning` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_quota_debits` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_quota_windows` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Remove windows with no remaining peer debit; retain shared provider-budget counters needed by peers. |
| `connector_rest_credentials` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_schedules` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_workspace_dispatch` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `connector_world_credentials` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `content_translations` | [modules/translations/erasure.py](../modules/translations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `daily_brief_evidence` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `daily_briefs` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `dashboard_groups` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `dashboard_layouts` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `dashboards` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `demo_seed_receipts` | [core/auth/deletion_cleanup.py](../core/auth/deletion_cleanup.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `document_chunks` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `document_cleanup_evidence_references` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `document_cleanup_operations` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `document_interactions` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `document_versions` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `documents` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entities` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_alias_evidence` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_aliases` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_correction_decisions` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_evidence_memberships` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_extraction_results` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_extraction_work` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_field_evidence` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_owner_actions` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `entity_redirects` | [modules/knowledge/entities/erasure.py](../modules/knowledge/entities/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `event_outbox` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `gadget_definitions` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `gadget_highlight_progress` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `gadget_highlight_suppression` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `gadget_instances` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `gadget_placements` | [modules/dashboard/erasure.py](../modules/dashboard/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `github_oauth_attempts` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `github_oauth_coordinators` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `github_oauth_grants` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `github_oauth_operations` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `github_source_hints` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `github_sync_resets` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `github_webhook_capacity` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Adjust owned reservations; retain singleton aggregate counters without account content. |
| `github_webhook_deliveries` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Remove exclusive app/installation receipts; preserve receipts still required by a peer grant. |
| `github_webhook_outbox` | [modules/connectors/erasure.py](../modules/connectors/erasure.py) | Clear copied owned fanout scope; rebuild remaining peer fanout without owned snapshots. |
| `goals` | [modules/goals/erasure.py](../modules/goals/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `google_identity` | [core/auth/deletion_worker.py](../core/auth/deletion_worker.py) | Delete local issuer/subject/email identity at finalization; no claim of provider account deletion. |
| `ingestion_batches` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `ingestion_collection_receipts` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `ingestion_runs` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `ingestion_stages` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `maintenance_summary` | [modules/observability/erasure.py](../modules/observability/erasure.py) | Retain deployment-wide maintenance times and aggregate counts; contains no account identifier or content. |
| `mcp_capabilities` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `mcp_capability_grants` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `mcp_connections` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `mcp_discoveries` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `mcp_inbound_clients` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `memories` | [modules/memory/erasure.py](../modules/memory/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `memory_candidates` | [modules/memory/erasure.py](../modules/memory/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `memory_privacy_settings` | [modules/memory/erasure.py](../modules/memory/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `module_lifecycle_settings` | [modules/settings/erasure.py](../modules/settings/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `news_observations` | [modules/news/erasure.py](../modules/news/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `news_recovery_checkpoints` | [modules/news/erasure.py](../modules/news/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `news_stories` | [modules/news/erasure.py](../modules/news/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `news_story_identities` | [modules/news/erasure.py](../modules/news/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `news_topics` | [modules/news/erasure.py](../modules/news/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `normalized_document_identities` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `normalized_version_provenance` | [modules/knowledge/documents/erasure.py](../modules/knowledge/documents/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `notifications` | [modules/notifications/erasure.py](../modules/notifications/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `observation_normalizations` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `observations` | [modules/knowledge/observations/erasure.py](../modules/knowledge/observations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `onboarding_state` | [modules/settings/erasure.py](../modules/settings/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `owner` | [core/auth/deletion_worker.py](../core/auth/deletion_worker.py) | Delete ordinary identities last; account 1 retains only the documented sanitized disabled bootstrap tombstone. |
| `owner_preferences` | [modules/settings/erasure.py](../modules/settings/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `realtime_replay_events` | [core/auth/deletion_cleanup.py](../core/auth/deletion_cleanup.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `realtime_replay_head` | [core/auth/deletion_cleanup.py](../core/auth/deletion_cleanup.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `relationship_evidence` | [modules/knowledge/relationships/erasure.py](../modules/knowledge/relationships/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `relationship_snapshot_history` | [modules/knowledge/relationships/erasure.py](../modules/knowledge/relationships/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `relationships` | [modules/knowledge/relationships/erasure.py](../modules/knowledge/relationships/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `remote_heavy_guards` | [modules/tools/erasure.py](../modules/tools/erasure.py) | Wait for owned browser runtime bound then remove owned guards; preserve unrelated deployment work. |
| `retention_settings` | [modules/settings/erasure.py](../modules/settings/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `search_index_generations` | [modules/search/erasure.py](../modules/search/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `search_index_items` | [modules/search/erasure.py](../modules/search/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `source_ingestion_state` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `source_observations` | [modules/ingestion/erasure.py](../modules/ingestion/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `source_purge_operations` | [modules/sources/erasure.py](../modules/sources/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `sources` | [modules/sources/erasure.py](../modules/sources/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `tasks` | [modules/tasks/erasure.py](../modules/tasks/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_allocations` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_changes` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_dispatches` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_mappings` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_operations` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_partitions` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_rebuild_dependencies` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_receipts` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_reconcile_members` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_reconcile_runs` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `temporal_supports` | [modules/knowledge/temporal/erasure.py](../modules/knowledge/temporal/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_event_audits` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_event_evidence` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_event_participants` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_event_suppressions` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_events` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_extraction_results` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_extraction_work` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `timeline_participant_evidence` | [modules/timeline/erasure.py](../modules/timeline/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `translation_admission_slots` | [modules/translations/erasure.py](../modules/translations/erasure.py) | Clear owned holder and increment fencing token; retain empty singleton/peer holder. |
| `translation_batch_items` | [modules/translations/erasure.py](../modules/translations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `translation_batches` | [modules/translations/erasure.py](../modules/translations/erasure.py) | Delete scoped rows in bounded pages; verify absence before identity finalization. |
| `translation_settings` | [modules/settings/erasure.py](../modules/settings/erasure.py) | Delete owned settings; anonymize matching updater IDs in peer settings while preserving their choices/revision/timestamps, including account 1; verify no updater reference remains. |
| `workspace_invitations` | [core/workspaces/erasure.py](../core/workspaces/erasure.py) | Remove accepted/email references and owned invitations; live owned invitations block acceptance. |
| `workspace_memberships` | [core/workspaces/erasure.py](../core/workspaces/erasure.py) | Remove departing memberships in peer workspaces; remove owned memberships during finalization. |
| `workspace_shares` | [core/workspaces/erasure.py](../core/workspaces/erasure.py) | Remove departing member references; remove owned workspace shares during finalization. |
| `workspaces` | [core/workspaces/erasure.py](../core/workspaces/erasure.py) | Delete emptied owned workspace last; shared members/live invitations block acceptance. |

Total: 152 registered tables; 136 ordinary cleanup tables, 8 shared/operational tables, 8 identity/workspace/receipt tables.

External persistence is separate: graph content and its permanent opaque late-write fence, filesystem originals, Agent saver tables and Redis namespaces are owned by their respective stage contracts. Host environment secrets, provider backups/logs, exported copies and third-party retained data require separate operator/provider handling.
