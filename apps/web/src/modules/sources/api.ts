import { apiRequest, csrfHeaders } from '@/core/api';

export type Source = {
  id: string;
  type: string;
  name: string;
  provider: string | null;
  status: 'active' | 'paused' | 'archived';
  local_only: boolean;
  last_sync_at: string | null;
  last_success_at: string | null;
  last_error_at: string | null;
  last_error_code: string | null;
  collected_at: string | null;
  indexed_at: string | null;
  collection_error_code: string | null;
  processing_error_code: string | null;
  generation: number;
  retired_at: string | null;
  created_at: string;
  updated_at: string;
};
export type SourcePage = { items: Source[]; next_cursor: string | null };
export type IngestionRun = {
  run_id: string;
  source_id: string;
  status: 'queued' | 'running' | 'succeeded' | 'needs_ocr' | 'failed';
  stages: { stage_key: string; status: string; attempts: number; error_code: string | null; result_count: number | null; normalized_count: number; duplicate_count: number; skipped_count: number; failed_count: number; pending_count: number; updated_at: string }[];
  error_code: string | null;
  created_at: string;
  updated_at: string;
};
export type ConnectorConfig = {
  url?: string;
  feed_url?: string;
  js_render?: boolean;
  max_pages?: number;
  max_depth?: number;
  timeout_seconds: number;
  items_path?: string;
  id_field?: string;
  title_field?: string;
  content_field?: string;
  updated_field?: string;
  timezone: string;
  schedule_interval_minutes: 15 | 30 | 60 | 360 | 1440;
  youtube_channel_id?: string;
  arxiv_category?: string;
  huggingface_author?: string;
  github_owner?: string;
  github_repository?: string;
  include_issues?: boolean;
  include_pulls?: boolean;
  include_commits?: boolean;
  include_releases?: boolean;
  github_history_days?: number;
  telegram_chat_ids?: string[];
  history_mode?: 'returned_snapshot' | 'pending_updates';
  connection_id?: string;
  calls?: { grant_id: string; arguments: Record<string, unknown> }[];
};
export type ConnectorSettings = {
  expected_revision: number;
  configuration: ConnectorConfig;
  auth_method: 'none' | 'http_header' | 'telegram_bot_token';
  auth_header_name?: string;
};
export type DraftValidationRequest = ConnectorSettings & { expected_source_generation: number; secret_action?: 'keep' | 'replace'; secret?: string };
export type ConnectorActivation = {
  source_id: string;
  desired_revision: number;
  applied_revision: number;
  state: string;
  error_code: string | null;
  credential_recovery: string;
};
export type ConnectorConfiguration = {
  source_id: string;
  source_type: 'rss' | 'web' | 'api' | 'mcp';
  source_generation: number;
  provider: string | null;
  configuration: ConnectorConfig;
  expected_revision: number;
  auth_method: 'none' | 'http_header' | 'telegram_bot_token';
  auth_header_name: string | null;
  desired_enabled: boolean;
  activation_state: string;
  activation_error_code: string | null;
  provider_credential_configured: boolean;
  provider_credential_state: string | null;
};
export type DraftValidation = {
  source_id: string;
  source_generation: number;
  expected_revision: number;
  validated_at: string;
  validation_status: 'valid';
  checks: ('configuration' | 'public_url_policy' | 'provider_identity' | 'provider_scope' | 'receive_mode')[];
  verified_bot_id?: string | null;
  scope_verified?: boolean | null;
};
export type ConnectorCatalogEntry = {
  provider_id: string;
  label: string;
  auth_methods: string[];
  scope_fields: string[];
  configuration_fields: string[];
  quota_limits: Record<string, number>;
  history_description: string | null;
  collection_modes: string[];
  supports_history: boolean;
  supports_edit: boolean;
  supports_delete: boolean;
  availability: 'available' | 'implemented' | 'requires_credentials' | 'unsupported_operation' | 'planned' | 'unavailable';
  unavailable_reason?: string | null;
  availability_reason?: string | null;
  unavailable_operations: string[];
};
export type SourceIngestion = { current_run: IngestionRun | null; items: IngestionRun[]; next_cursor: string | null };
export type PurgeOperation = { operation_id: string; source_id: string; status: string; error_code: string | null; created_at: string; updated_at: string };
export const sourceKeys = { all: ['sources'] as const, list: ['sources', 'list'] as const, detail: (id: string) => ['sources', id] as const };
export const connectorKeys = {
  catalog: ['connector-catalog'] as const,
  githubWebhookStatus: ['github-webhook-status'] as const,
  configuration: (id: string) => ['connector-configuration', id] as const,
  activation: (id: string) => ['connector-activation', id] as const,
  ingestion: (id: string) => ['source-ingestion', id] as const,
};

/** Lists source records in pages of 50 and includes the optional opaque cursor. */
export function listSources(cursor?: string) {
  return apiRequest<SourcePage>(`/api/v1/sources?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
}

/** Fetches one source by its identifier. */
export function getSource(id: string) { return apiRequest<Source>(`/api/v1/sources/${id}`); }

/** Creates a manual source with the supplied name and CSRF token. */
export function createManualSource(name: string, csrfToken: string) {
  return apiRequest<Source>('/api/v1/sources', { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ type: 'manual', name }) });
}

/** Archives a source through the source DELETE endpoint using CSRF protection. */
export function archiveSource(id: string, csrfToken: string) {
  return apiRequest<void>(`/api/v1/sources/${id}`, { method: 'DELETE', headers: csrfHeaders(csrfToken) });
}

/** Requests source deletion with source data purge and returns the asynchronous purge operation. */
export function purgeSource(id: string, csrfToken: string) {
  return apiRequest<PurgeOperation>(`/api/v1/sources/${id}?with_data=true`, { method: 'DELETE', headers: csrfHeaders(csrfToken) });
}

/** Sets a source to active or paused, forwarding the abort signal and CSRF token. */
export function updateSourceStatus(id: string, status: 'active' | 'paused', csrfToken: string, signal?: AbortSignal) {
  return apiRequest<Source>(`/api/v1/sources/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ status }), signal });
}

/** Fetches the connector catalog and forwards an optional abort signal. */
export function getConnectorCatalog(signal?: AbortSignal) {
  return apiRequest<ConnectorCatalogEntry[]>('/api/v1/connectors/catalog', { signal });
}

/** Creates a connector with its immutable native provider identity, if any, using the source owner route. */
export function createConnectorSource(type: 'rss' | 'web' | 'api' | 'mcp', name: string, csrfToken: string, signal?: AbortSignal, provider?: string) {
  return apiRequest<Source>('/api/v1/sources', { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ type, name, ...(provider ? { provider } : {}) }), signal });
}

/** Fetches the connector configuration and forwards an optional abort signal. */
export function getConnectorConfiguration(id: string, signal?: AbortSignal) {
  return apiRequest<ConnectorConfiguration>(`/api/v1/connectors/${id}/configuration`, { signal });
}

/** Checks matching source configuration snapshots against every same-source revision and generation fence. */
export function connectorConfigurationIsAtLeast(
  candidate: ConnectorConfiguration,
  fences: readonly (ConnectorConfiguration | undefined)[],
) {
  return fences.every((fence) => !fence || fence.source_id !== candidate.source_id
    || (candidate.expected_revision >= fence.expected_revision && candidate.source_generation >= fence.source_generation));
}

/** Selects the newest snapshot that is not older than the other snapshots for the requested source. */
export function newestConnectorConfiguration(
  snapshots: readonly (ConnectorConfiguration | undefined)[],
  sourceId: string,
) {
  const candidates = snapshots.filter((value): value is ConnectorConfiguration => value?.source_id === sourceId);
  return candidates.find((candidate) => connectorConfigurationIsAtLeast(candidate, candidates)) ?? null;
}

/** Accepts the candidate when it satisfies all revision fences, otherwise falls back to the newest fenced snapshot. */
export function selectMonotonicConnectorConfiguration(
  candidate: ConnectorConfiguration,
  fences: readonly (ConnectorConfiguration | undefined)[],
) {
  return connectorConfigurationIsAtLeast(candidate, fences)
    ? candidate
    : newestConnectorConfiguration(fences, candidate.source_id);
}

/** Fetches connector configuration, rejects a mismatched source ID, and selects a result that does not regress local revision fences. */
export async function getMonotonicConnectorConfiguration(
  id: string,
  signal: AbortSignal | undefined,
  current: () => readonly (ConnectorConfiguration | undefined)[],
) {
  const incoming = await getConnectorConfiguration(id, signal);
  if (incoming.source_id !== id) throw new Error('connector_configuration_source_mismatch');
  const fences = current();
  return selectMonotonicConnectorConfiguration(incoming, fences)
    ?? [...fences].reverse().find((value) => value?.source_id === id) ?? incoming;
}

/** Validates an unsaved connector draft; optional write-only Telegram replacement bytes are sent directly and never returned or cached here. */
export function validateDraftConnector(id: string, settings: DraftValidationRequest, signal?: AbortSignal) {
  return apiRequest<DraftValidation>(`/api/v1/connectors/${id}/validate-draft`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings), signal });
}

/** Saves connector settings with CSRF protection and the optional abort signal. */
export function saveConnectorConfiguration(id: string, settings: ConnectorSettings, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<ConnectorActivation>(`/api/v1/connectors/${id}/configuration`, { method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify(settings), signal });
}

/** Fetches the connector activation state and forwards an optional abort signal. */
export function getConnectorActivation(id: string, signal?: AbortSignal) {
  return apiRequest<ConnectorActivation>(`/api/v1/connectors/${id}/activation`, { signal });
}

export type GitHubPeer = { source_id: string; source_generation: number; configuration_revision: number; token_revision: number; state: string };
export type GitHubStatus = { state: 'not_connected' | 'ready' | 'refreshing' | 'reconciliation_required' | 'revoked'; expires_at: string | null; error_code: string | null; coordinator_state: string; operation_id: string | null; coordinator_error_code: string | null; recovery_available: boolean; operation_kind: 'authorization' | 'refresh' | 'revoke' | null; operation_source_id: string | null; operation_source_generation: number | null; operation_configuration_revision: number | null; operation_state: string | null; sync: { history_days: number; scope_sha256: string | null; cursor_invalid: boolean; last_reset_at: string | null; gap_recorded: boolean; unverified_hints: number; reconcile_exhausted: number; resources: { resource: string; phase: string; page: number; next_page: number | null; sweep_revision: number; floor?: string; upper?: string; completed_upper: string | null; completed_sweep_revision: number; incomplete: boolean }[] } };

export type GitHubWebhookStatus = {
  receiver_configured: boolean;
  receiver_revision: string;
  digest_count: number;
  pending_count: number;
  pending_deliveries: number;
  pending_hints: number;
  needs_attention: number;
  oldest_pending_at: string | null;
};

/** Reads secret-free GitHub receiver readiness and global durable backlog totals. */
export function getGitHubWebhookStatus(signal?: AbortSignal) {
  return apiRequest<GitHubWebhookStatus>('/api/v1/connectors/github/webhook-status', { signal });
}

/** Canonical mapped GitHub record counts for one source; live_verified is false unless a live check exists. */
export type GitHubSummary = {
  resource_counts: { repositories: number; issues: number; pull_requests: number; commits: number; releases: number };
  live_verified: boolean;
  last_event_at: string | null;
};

/** Reads canonical collected GitHub counts from the owner-only summary route. */
export function getGitHubSummary(id: string, signal?: AbortSignal) {
  return apiRequest<GitHubSummary>(`/api/v1/connectors/${id}/github/summary`, { signal });
}

/** Reads secret-free GitHub grant status and its access-token expiry for this source. */
export function getGitHubStatus(id: string, signal?: AbortSignal) {
  return apiRequest<GitHubStatus>(`/api/v1/connectors/${id}/github/status`, { signal });
}

/** Resets GitHub polling only for the current reviewed source revisions and scope digest. */
export function resetGitHubSync(id: string, sourceGeneration: number, connectorRevision: number, scopeSha256: string, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<{ status: 'reset' }>(`/api/v1/connectors/${id}/github/sync/reset`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ expected_source_generation: sourceGeneration, expected_connector_revision: connectorRevision, expected_scope_sha256: scopeSha256 }), signal });
}

/** Starts browser-bound OAuth for the exact saved source and connector revisions. */
export function startGitHubOAuth(id: string, sourceGeneration: number, expectedRevision: number, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<{ authorization_url: string }>(`/api/v1/connectors/${id}/github/oauth/start`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ expected_source_generation: sourceGeneration, expected_revision: expectedRevision }), signal });
}

/** Rotates the stored GitHub user grant through the owner-serialized server route. */
export function refreshGitHubOAuth(id: string, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<{ state: string }>(`/api/v1/connectors/${id}/github/oauth/refresh`, { method: 'POST', headers: csrfHeaders(csrfToken), signal });
}

/** Retrieves the bounded, secret-free peer snapshot that the owner must review before disconnect. */
export function getGitHubPeers(id: string, signal?: AbortSignal) {
  return apiRequest<{ complete: boolean; peers: GitHubPeer[] }>(`/api/v1/connectors/${id}/github/peers`, { signal });
}

/** Submits the owner's reviewed peer snapshot for app/user-wide revocation. */
export function disconnectGitHubOAuth(id: string, peers: GitHubPeer[], csrfToken: string, operationId?: string, signal?: AbortSignal) {
  return apiRequest<{ state: string }>(`/api/v1/connectors/${id}/github/disconnect`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ reviewed_peers: peers.map(({ source_id, source_generation, configuration_revision, token_revision }) => ({ source_id, source_generation, configuration_revision, token_revision })), ...(operationId ? { operation_id: operationId } : {}) }), signal });
}

/** Acknowledges an exact owner-wide OAuth tombstone; the selected editor source does not replace the stored operation origin. */
export function acknowledgeGitHubReconnect(id: string, operationId: string, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<{ state: string }>(`/api/v1/connectors/${id}/github/reconcile/reconnect`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ operation_id: operationId, acknowledge_unresolved_cleanup: true }), signal });
}

/** Activates a connector at the expected revision with an explicit keep or replace action; secret bytes are sent only in this request body. */
export function activateConnector(id: string, expectedRevision: number, secretAction: 'keep' | 'replace', secret: string | undefined, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<ConnectorActivation>(`/api/v1/connectors/${id}/activate`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ expected_revision: expectedRevision, secret_action: secretAction, ...(secret ? { secret } : {}) }), signal });
}

/** Deactivates the connector using CSRF protection and the optional abort signal. */
export function deactivateConnector(id: string, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<ConnectorActivation>(`/api/v1/connectors/${id}/deactivate`, { method: 'POST', headers: csrfHeaders(csrfToken), signal });
}

/** Removes the provider credential only at the supplied expected revision, using CSRF protection. */
export function removeProviderCredential(id: string, expectedRevision: number, csrfToken: string, signal?: AbortSignal) {
  return apiRequest<ConnectorActivation>(`/api/v1/connectors/${id}/credentials/provider?expected_revision=${expectedRevision}`, { method: 'DELETE', headers: csrfHeaders(csrfToken), signal });
}

/** Starts connector collection with CSRF protection and returns the accepted run or batch identifiers. */
export function triggerCollection(id: string, csrfToken: string) {
  return apiRequest<{ run_id: string | null; batch_id: string | null; status: string }>(`/api/v1/connectors/sources/${id}/collect`, { method: 'POST', headers: csrfHeaders(csrfToken) });
}

/** Fetches an ingestion run by ID. */
export function getRun(id: string) { return apiRequest<IngestionRun>(`/api/v1/ingestion/runs/${id}`); }

/** Lists a source’s ingestion runs in pages of 20 using the optional cursor. */
export function getSourceIngestion(id: string, cursor?: string) {
  return apiRequest<SourceIngestion>(`/api/v1/ingestion/sources/${id}/runs?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
}

/** Retries the selected ingestion stage using CSRF protection. */
export function retryRun(id: string, stageKey: string, csrfToken: string) {
  return apiRequest<{ run_id: string }>(`/api/v1/ingestion/runs/${id}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) }, body: JSON.stringify({ stage_key: stageKey }) });
}

/** Fetches a system operation by ID. */
export function getOperation(id: string) { return apiRequest<PurgeOperation>(`/api/v1/system/operations/${id}`); }
