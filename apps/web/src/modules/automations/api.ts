import { ApiError, apiRequest, csrfHeaders } from '@/core/api';

export type TriggerType = 'schedule' | 'new_event' | 'new_document' | 'entity_changed' | 'task_due' | 'goal_deadline' | 'webhook' | 'connector_sync_result';
export type ActionType = 'run_agent' | 'create_task' | 'create_notification' | 'generate_brief' | 'call_webhook';
export type Operator = 'eq' | 'ne' | 'in' | 'gt' | 'gte' | 'lt' | 'lte';
export type Scalar = string | number | boolean;

/** Trigger parameters; only the fields of the selected `type` are meaningful. */
export interface Trigger { type: TriggerType; cron?: string; timezone?: string; lead_minutes?: number; lead_days?: number; hook?: string }
/** One AND-ed comparison against a field declared by the trigger. */
export interface Condition { field: string; operator: Operator; value: Scalar | Scalar[] }
/** Action parameters; only the fields of the selected `type` are meaningful. */
export interface Action {
  type: ActionType; profile_id?: string; instruction?: string; title?: string; description?: string | null;
  due_in_days?: number | null; message?: string; link?: string | null; scope?: 'daily'; alias?: string; event?: string;
}
/** Rule definition without identity, the part that every revision snapshots. */
export interface Definition { trigger: Trigger; conditions: Condition[]; actions: Action[] }
/** Public rule projection at its current revision. */
export interface Automation extends Definition { id: string; name: string; enabled: boolean; revision: number; created_at: string; updated_at: string }

/** Trigger or action type with owning-module availability, as reported by the server. */
export interface CapabilityItem { type: string; module: string | null; available: boolean; reason: string | null; requires_approval: boolean; fields: Record<string, 'string' | 'number' | 'boolean'> }
/** Editor options derived from the server schemas; webhook aliases are names only, never URLs. */
export interface Capabilities { triggers: CapabilityItem[]; actions: CapabilityItem[]; webhook_aliases: string[] }
export interface WebhookCredential { alias: string; token: string; revision: number; expires_at: string; endpoint: string }

export interface PreviewResult { matched: boolean; reasons: Array<{ index: number; field: string; operator: string; outcome: string }>; planned_actions: Array<{ type: string; module: string; requires_approval: boolean }> }
export interface RunAction { ordinal: number; type: string; status: string; attempts: number; error_code: string | null; result_reference: string | null; approval_expires_at: string | null }
export interface Run {
  id: string; automation_id: string; revision: number; trigger_type: string; trigger_event_id: string | null;
  scheduled_slot: string | null; depth: number; status: string; reason: string | null; attempts: number;
  created_at: string; finished_at: string | null; actions: RunAction[];
}

const base = '/api/v1/automations';

/** Builds JSON write headers carrying the session CSRF token. */
function jsonHeaders(csrfToken: string): HeadersInit { return { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }; }

/** Lists the owner's live rules. */
export function listAutomations(): Promise<{ items: Automation[] }> { return apiRequest(base); }

/** Reads trigger fields, action availability by owning module and webhook alias names. */
export function getCapabilities(): Promise<Capabilities> { return apiRequest(`${base}/capabilities`); }

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
  return apiRequest(base, { method: 'POST', headers: jsonHeaders(csrfToken), body: JSON.stringify({ name, enabled: false, ...definition }) });
}

/** Applies a revision-fenced patch (definition edit, rename, enable or disable). */
export function patchAutomation(id: string, expectedRevision: number, patch: Partial<Definition> & { name?: string; enabled?: boolean }, csrfToken: string): Promise<Automation> {
  return apiRequest(`${base}/${encodeURIComponent(id)}`, { method: 'PATCH', headers: jsonHeaders(csrfToken), body: JSON.stringify({ expected_revision: expectedRevision, ...patch }) });
}

/** Soft-deletes a rule at its expected revision; its run history is retained. */
export function deleteAutomation(id: string, expectedRevision: number, csrfToken: string): Promise<void> {
  return apiRequest(`${base}/${encodeURIComponent(id)}?expected_revision=${expectedRevision}`, { method: 'DELETE', headers: csrfHeaders(csrfToken) });
}

/** Dry-runs an inline definition against a sample; nothing is queued, sent or created. */
export function previewAutomation(definition: Definition, sample: Record<string, Scalar>, csrfToken: string): Promise<PreviewResult> {
  return apiRequest(`${base}/preview`, { method: 'POST', headers: jsonHeaders(csrfToken), body: JSON.stringify({ definition, sample }) });
}

/** Queues one manual run; the client request id makes a retried click return the same run. */
export function runAutomation(id: string, expectedRevision: number, clientRequestId: string, csrfToken: string): Promise<Run> {
  return apiRequest(`${base}/${encodeURIComponent(id)}/run`, { method: 'POST', headers: jsonHeaders(csrfToken), body: JSON.stringify({ expected_revision: expectedRevision, client_request_id: clientRequestId }) });
}

/** Lists newest-first runs with per-action outcomes. */
export function listRuns(id: string): Promise<{ items: Run[] }> { return apiRequest(`${base}/${encodeURIComponent(id)}/runs?limit=50`); }

/** Approves or denies one action waiting for approval through the owner decision route. */
export function decideAction(runId: string, ordinal: number, decision: 'approve' | 'deny', csrfToken: string): Promise<Run> {
  return apiRequest(`${base}/runs/${encodeURIComponent(runId)}/actions/${ordinal}/decision`, { method: 'POST', headers: jsonHeaders(csrfToken), body: JSON.stringify({ decision }) });
}

/** Returns the per-rule Chat conversation id used by agent actions, or null before any ran. */
export async function getRuleConversationId(id: string): Promise<string | null> {
  return (await apiRequest<{ conversation_id: string | null }>(`${base}/${encodeURIComponent(id)}/conversation`)).conversation_id;
}

/** Maps a failed automation request to a localized message key; server text is never rendered. */
export function errorMessageKey(error: unknown, fallback: 'actionFailed' | 'saveFailed'): string {
  if (!(error instanceof ApiError)) return fallback;
  if (error.status === 409) {
    return ({ brief_slot_owned: 'conflictBriefSlot', quota_exceeded: 'conflictQuota', stale_revision: 'conflict' } as Record<string, string>)[error.code ?? ''] ?? 'conflictGeneric';
  }
  if (error.status === 422) return error.code === 'pause_before_brief_edit' ? 'pauseBeforeBriefEdit' : 'invalidServer';
  return fallback;
}
