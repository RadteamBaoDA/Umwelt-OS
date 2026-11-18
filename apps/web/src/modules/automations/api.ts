import type { ApiResponse } from '@/core/api-contract';
import { ApiError, apiRequest, csrfHeaders } from '@/core/api';
import type { AutomationRequestErrorKey } from './message-keys';

/** Daily brief sections a task can include, in display order. */
export type BriefSection = 'stories' | 'markets' | 'weather' | 'hazards' | 'agenda' | 'source_health';
export type TriggerType =
  'schedule' | 'new_event' | 'new_document' | 'entity_changed' | 'task_due' | 'goal_deadline' | 'webhook' | 'connector_sync_result';
export type ActionType = 'run_agent' | 'create_task' | 'create_notification' | 'generate_brief' | 'call_webhook' | 'collect_source';
export type Operator = 'eq' | 'ne' | 'in' | 'gt' | 'gte' | 'lt' | 'lte';
export type Scalar = string | number | boolean;

/** Trigger parameters; only the fields of the selected `type` are meaningful. */
export interface Trigger {
  type: TriggerType;
  cron?: string;
  timezone?: string;
  lead_minutes?: number;
  lead_days?: number;
  hook?: string;
}
/** One AND-ed comparison against a field declared by the trigger. */
export interface Condition {
  field: string;
  operator: Operator;
  value: Scalar | Scalar[];
}
/** What a Topic watch looks for; mirrors the backend `TopicWatchSpec`. */
export interface TopicWatchSpec {
  topic: string;
  keep_rule: string | null;
  languages: string[];
  include_sites: string[];
  exclude_sites: string[];
  max_items: number;
}
/** One-time owner consent as stored by the server (never written by the client). */
export interface TopicConsent {
  hash: string;
  granted_at: string;
  est_tokens: number;
}
/** Action parameters; only the fields of the selected `type` are meaningful. */
export interface Action {
  type: ActionType;
  profile_id?: string;
  instruction?: string;
  title?: string;
  description?: string | null;
  due_in_days?: number | null;
  message?: string;
  link?: string | null;
  scope?: 'daily';
  sections?: BriefSection[];
  extra_instruction?: string | null;
  deliver_chat?: boolean;
  model_role?: 'brief' | 'chat' | 'background';
  alias?: string;
  event?: string;
  /** `collect_source` (Topic watch): the watch spec. */
  watch?: TopicWatchSpec;
  /** `collect_source`: server-assigned task-owned Source id (client values are ignored). */
  source_id?: string | null;
  /** `collect_source`: server-assigned consent; absent until the owner grants it. */
  consent?: TopicConsent | null;
  /** `collect_source`, request only: the per-run token estimate the owner accepts. */
  grant_consent_est_tokens?: number;
}
/** Rule definition without identity, the part that every revision snapshots. */
export interface Definition {
  trigger: Trigger;
  conditions: Condition[];
  actions: Action[];
}
/** Public rule projection at its current revision. */
export interface Automation extends Definition {
  id: string;
  name: string;
  enabled: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
  /** Next scheduled fire time (UTC ISO), or null when paused or not schedule-driven. */
  next_run_at?: string | null;
}

/** Trigger or action type with owning-module availability, as reported by the server. */
/** One trigger or action the server can run, with availability by owning module (derived from the backend OpenAPI schema). */
export type CapabilityItem = Capabilities['triggers'][number];
/** Editor options derived from the server schemas; webhook aliases are names only, never URLs. */
export type Capabilities = ApiResponse<'/api/v1/automations/capabilities'>;
export interface WebhookCredential {
  alias: string;
  token: string;
  revision: number;
  expires_at: string;
  endpoint: string;
}

export interface PreviewResult {
  matched: boolean;
  reasons: Array<{ index: number; field: string; operator: string; outcome: string }>;
  planned_actions: Array<{ type: string; module: string; requires_approval: boolean }>;
}
export interface RunAction {
  ordinal: number;
  type: string;
  status: string;
  attempts: number;
  error_code: string | null;
  result_reference: string | null;
  approval_expires_at: string | null;
}
export interface Run {
  id: string;
  automation_id: string;
  revision: number;
  trigger_type: string;
  trigger_event_id: string | null;
  scheduled_slot: string | null;
  depth: number;
  status: string;
  reason: string | null;
  attempts: number;
  created_at: string;
  finished_at: string | null;
  actions: RunAction[];
}

const base = '/api/v1/automations';

/** Builds JSON write headers carrying the session CSRF token. */
function jsonHeaders(csrfToken: string): HeadersInit {
  return { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' };
}

/** Lists the owner's live rules. */
export function listAutomations(): Promise<{ items: Automation[] }> {
  return apiRequest(base);
}

/** Reads one live rule (trigger, enabled flag and revision); rejects with ApiError (404) when it no longer exists. */
export function getAutomation(id: string): Promise<Automation> {
  return apiRequest(`${base}/${encodeURIComponent(id)}`);
}

/** Reads trigger fields, action availability by owning module and webhook alias names. */
export function getCapabilities(): Promise<Capabilities> {
  return apiRequest(`${base}/capabilities`);
}

/** Rotates one inbound alias bearer; plaintext is returned only by this protected call. */
export function issueWebhookCredential(alias: string, csrfToken: string): Promise<WebhookCredential> {
  return apiRequest(`${base}/webhook-credentials/${encodeURIComponent(alias)}`, { method: 'POST', headers: csrfHeaders(csrfToken) });
}

/** Revokes the alias token immediately; a replacement can be issued from the same owner form. */
export function revokeWebhookCredential(alias: string, csrfToken: string): Promise<void> {
  return apiRequest(`${base}/webhook-credentials/${encodeURIComponent(alias)}`, { method: 'DELETE', headers: csrfHeaders(csrfToken) });
}

/** Creates a rule; new rules start disabled so enabling is always a separate explicit step. */
export function createAutomation(name: string, definition: Definition, csrfToken: string): Promise<Automation> {
  return apiRequest(base, {
    method: 'POST',
    headers: jsonHeaders(csrfToken),
    body: JSON.stringify({ name, enabled: false, ...definition }),
  });
}

/** Applies a revision-fenced patch (definition edit, rename, enable or disable). */
export function patchAutomation(
  id: string,
  expectedRevision: number,
  patch: Partial<Definition> & { name?: string; enabled?: boolean },
  csrfToken: string,
): Promise<Automation> {
  return apiRequest(`${base}/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: jsonHeaders(csrfToken),
    body: JSON.stringify({ expected_revision: expectedRevision, ...patch }),
  });
}

/**
 * Soft-deletes a rule at its expected revision; its run history is retained. For a Topic watch, `purgeItems` also
 * deletes the collected items; by default the source is archived and the items stay searchable.
 */
export function deleteAutomation(id: string, expectedRevision: number, csrfToken: string, purgeItems?: boolean): Promise<void> {
  const purge = purgeItems === undefined ? '' : `&purge_items=${purgeItems}`;
  return apiRequest(`${base}/${encodeURIComponent(id)}?expected_revision=${expectedRevision}${purge}`, {
    method: 'DELETE',
    headers: csrfHeaders(csrfToken),
  });
}

/** Dry-runs an inline definition against a sample; nothing is queued, sent or created. */
export function previewAutomation(definition: Definition, sample: Record<string, Scalar>, csrfToken: string): Promise<PreviewResult> {
  return apiRequest(`${base}/preview`, { method: 'POST', headers: jsonHeaders(csrfToken), body: JSON.stringify({ definition, sample }) });
}

/** Queues one manual run; the client request id makes a retried click return the same run. */
export function runAutomation(id: string, expectedRevision: number, clientRequestId: string, csrfToken: string): Promise<Run> {
  return apiRequest(`${base}/${encodeURIComponent(id)}/run`, {
    method: 'POST',
    headers: jsonHeaders(csrfToken),
    body: JSON.stringify({ expected_revision: expectedRevision, client_request_id: clientRequestId }),
  });
}

/** Lists newest-first runs with per-action outcomes. */
export function listRuns(id: string): Promise<{ items: Run[] }> {
  return apiRequest(`${base}/${encodeURIComponent(id)}/runs?limit=50`);
}

/** Approves or denies one action waiting for approval through the owner decision route. */
export function decideAction(runId: string, ordinal: number, decision: 'approve' | 'deny', csrfToken: string): Promise<Run> {
  return apiRequest(`${base}/runs/${encodeURIComponent(runId)}/actions/${ordinal}/decision`, {
    method: 'POST',
    headers: jsonHeaders(csrfToken),
    body: JSON.stringify({ decision }),
  });
}

/** Returns the per-rule Chat conversation id used by agent actions, or null before any ran. */
export async function getRuleConversationId(id: string): Promise<string | null> {
  return (await apiRequest<{ conversation_id: string | null }>(`${base}/${encodeURIComponent(id)}/conversation`)).conversation_id;
}
/** Maps a failed automation request to a localized message key; server text is never rendered. */
export function errorMessageKey(error: unknown, fallback: 'action.failed' | 'saveFailed'): AutomationRequestErrorKey {
  if (!(error instanceof ApiError)) return fallback;
  if (error.status === 409) {
    const byCode = { brief_slot_owned: 'conflict.briefSlot', quota_exceeded: 'conflict.quota', stale_revision: 'conflict.label' } as const;
    return Object.hasOwn(byCode, error.code ?? '') ? byCode[error.code as keyof typeof byCode] : 'conflict.generic';
  }
  if (error.status === 422) {
    if (error.code === 'consent_estimate_mismatch') return 'scheduledTasks.topic.consentMismatch';
    return error.code === 'pause_before_brief_edit' ? 'pauseBeforeBriefEdit' : 'invalidServer';
  }
  return fallback;
}

/** Creates an enabled scheduled task rule (unlike `createAutomation`, which always starts disabled). */
export function createScheduledTask(name: string, definition: Definition, csrfToken: string): Promise<Automation> {
  return apiRequest(base, {
    method: 'POST',
    headers: jsonHeaders(csrfToken),
    body: JSON.stringify({ name, enabled: true, ...definition }),
  });
}

/** Returns the newest run of one rule (empty list before the first), for the "Last run" column. */
export function getLastRun(id: string): Promise<{ items: Run[] }> {
  return apiRequest(`${base}/${encodeURIComponent(id)}/runs?limit=1`);
}

/** Reads the workspace brief timezone from the existing brief schedule; the editor falls back to the browser zone on failure. */
export async function getBriefTimezone(): Promise<string> {
  return (await apiRequest<{ timezone: string }>('/api/v1/briefs/schedule')).timezone;
}

/** Model role a scheduled brief can run under; mirrors the backend `GenerateBriefAction.model_role`. */
export type BriefModelRole = 'brief' | 'chat' | 'background';
/** The selectable roles in display order. */
export const BRIEF_MODEL_ROLES: BriefModelRole[] = ['brief', 'chat', 'background'];

/**
 * Reads the configured model name of each selectable role from Settings > Models (read-only). `background` falls
 * back to `brief`, then `chat`, like the backend resolver; a role with no model maps to null.
 */
export async function getBriefRoleModels(): Promise<Record<BriefModelRole, string | null>> {
  const { defaults } = await apiRequest<ApiResponse<'/api/v1/settings/models'>>('/api/v1/settings/models');
  const brief = defaults.brief?.model || null;
  const chat = defaults.chat?.model || null;
  return { brief, chat, background: defaults.background?.model || brief || chat };
}

/** Cap of the collected-items count query; a result at the cap is shown as "100+". */
export const ITEM_COUNT_CAP = 100;

/**
 * Counts the documents a Topic watch collected into its source (at most {@link ITEM_COUNT_CAP}); `capped` is true
 * when more may exist. Read-only; rejects with ApiError on failure.
 */
export async function countSourceDocuments(sourceId: string): Promise<{ count: number; capped: boolean }> {
  const page = await apiRequest<{ items: unknown[]; next_cursor?: string | null }>(
    `/api/v1/documents?source_id=${encodeURIComponent(sourceId)}&limit=${ITEM_COUNT_CAP}`,
  );
  return { count: page.items.length, capped: page.items.length >= ITEM_COUNT_CAP || Boolean(page.next_cursor) };
}
