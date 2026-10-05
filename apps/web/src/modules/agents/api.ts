import { apiRequest, csrfHeaders } from '@/core/api';

/** Exact registered tool revision an owner profile may select. */
export interface AgentProfileTool {
  name: string;
  version: string;
  fingerprint: string;
}

/** Owner-visible profile configuration plus server-derived capability and limit state. */
export interface AgentProfile {
  id: string;
  title: string;
  enabled: boolean;
  revision: number;
  model_alias: string;
  prompt: string;
  allowed_tools: AgentProfileTool[];
  available_tools: AgentProfileTool[];
  source_ids: string[];
  capability: 'available' | 'partial' | 'unavailable';
  unavailable_reasons: string[];
  limits: Record<string, number>;
}

/** Durable agent run summary shared by Settings history and full Chat. */
export interface AgentRun {
  id: string;
  agent_id: string;
  status: 'queued' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'cancelled';
  answer: string | null;
  error_code: string | null;
  steps: number;
  tool_calls: number;
  active_seconds: number;
  token_usage: number | null;
  token_budget: number | null;
  token_budget_available: boolean;
  token_usage_unknown: boolean;
  profile_id: string | null;
  profile_revision_hash: string | null;
  activities: Array<{ kind: string; status: string; tool_name?: string | null; created_at: string }>;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

/** Read the fixed specialist roster and server-derived capabilities. */
export function getAgentProfiles(): Promise<AgentProfile[]> {
  return apiRequest<AgentProfile[]>('/api/v1/agents/profiles');
}

/** Save one optimistic profile revision with exact registry tool contracts and source scope. */
export function updateAgentProfile(
  profile: AgentProfile,
  csrfToken: string,
): Promise<AgentProfile> {
  return apiRequest<AgentProfile>(`/api/v1/agents/profiles/${encodeURIComponent(profile.id)}`, {
    method: 'PATCH',
    headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      expected_revision: profile.revision,
      enabled: profile.enabled,
      model_alias: profile.model_alias,
      prompt: profile.prompt,
      allowed_tools: profile.allowed_tools,
      source_ids: profile.source_ids,
    }),
  });
}

/** Start an idempotent linked specialist run after pinning the expected profile revision. */
export function startAgentRun(
  profileId: string,
  payload: { prompt: string; expected_profile_revision: number; conversation_id: string; client_request_id: string },
  csrfToken: string,
): Promise<AgentRun> {
  return apiRequest<AgentRun>(`/api/v1/agents/${encodeURIComponent(profileId)}/runs`, {
    method: 'POST',
    headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** Read one owner run without exposing hidden reasoning or private tool payloads. */
export function getAgentRun(runId: string): Promise<AgentRun> {
  return apiRequest<AgentRun>(`/api/v1/agent-runs/${encodeURIComponent(runId)}`);
}

/** Read bounded profile or conversation history using the opaque server cursor. */
export function listAgentRuns(params: {
  profile_id?: string;
  conversation_id?: string;
  limit?: number;
  cursor?: string;
} = {}): Promise<{ items: AgentRun[]; next_cursor: string | null }> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) query.set(key, String(value));
  }
  const queryString = query.toString();
  return apiRequest<{ items: AgentRun[]; next_cursor: string | null }>(`/api/v1/agent-runs${queryString ? `?${queryString}` : ''}`);
}

/** Commit cancellation intent for the selected durable agent run. */
export function cancelAgentRun(runId: string, csrfToken: string): Promise<AgentRun> {
  return apiRequest<AgentRun>(`/api/v1/agent-runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST', headers: csrfHeaders(csrfToken),
  });
}

/** Owner-visible exact action projection returned through a live Chat agent link. */
export interface AgentApproval {
  id: string;
  action_id: string;
  run_id: string;
  conversation_id: string;
  tool_name: string;
  tool_version: string;
  arguments: Record<string, unknown> | null;
  argument_hash: string;
  destination_id: string;
  destination_revision: string;
  status: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled' | 'requires_review';
  effect_status: 'reserved' | 'in_flight' | 'succeeded' | 'failed' | 'requires_review' | null;
  result_reference: string | null;
  created_at: string;
  expires_at: string;
}

/** Read only the bounded approvals linked to the requested owner conversation. */
export function getConversationApprovals(conversationId: string): Promise<AgentApproval[]> {
  return apiRequest(`/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/agent-approvals`);
}

/** Submit the displayed digest with the current session's CSRF token. */
export function decideApproval(
  approval: AgentApproval,
  decision: 'approve' | 'deny',
  csrfToken: string,
): Promise<{ id: string; status: AgentApproval['status']; run_status: string }> {
  return apiRequest(`/api/v1/approvals/${encodeURIComponent(approval.id)}/${decision}`, {
    method: 'POST',
    headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_argument_hash: approval.argument_hash }),
  });
}
