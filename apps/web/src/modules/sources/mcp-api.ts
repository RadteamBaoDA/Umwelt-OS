import { apiRequest, csrfHeaders } from '@/core/api';

/** Credential-free owner snapshot; the service never exposes stored secret material. */
export type McpConnection = {
  id: string; name: string; transport: 'streamable_http' | 'stdio'; endpoint: string | null;
  deployment_profile_id: string | null; deployment_profile_hash: string | null; revision: number;
  enabled: boolean; auth_method: 'none' | 'bearer'; credential_configured: boolean;
  timeout_seconds: number; health: string | null; error_code: string | null; updated_at: string;
};
/** Editable connection payload with an explicit write-only credential operation. */
export type McpConnectionDraft = {
  name: string; transport: 'streamable_http' | 'stdio'; endpoint?: string;
  deployment_profile_id?: string; auth_method: 'none' | 'bearer';
  credential_update: { action: 'retain' | 'replace' | 'remove'; value?: string }; timeout_seconds: number;
};
/** Immutable server discovery snapshot; descriptor content is untrusted display data. */
export type McpDiscovery = {
  id: string; connection_id: string; connection_revision: number; deployment_profile_hash: string | null;
  protocol: string; schema_set_hash: string; created_at: string;
  capabilities: { id: string; kind: string; remote_key: string; descriptor: Record<string, unknown>; descriptor_hash: string }[];
};
/** Exact capability grant choice bound to the server returned descriptor hash. */
export type McpGrantChoice = {
  capability_id: string; descriptor_hash: string; purpose: 'chat' | 'collection';
  risk: 'READ_ONLY' | 'INTERNAL_WRITE' | 'EXTERNAL_WRITE' | 'DESTRUCTIVE';
  source_ids: string[]; destinations: string[]; expires_at?: string;
};
/** Persisted grant summary, including revocation and immutable review identity. */
export type McpGrant = McpGrantChoice & {
  id: string; connection_id: string; reviewed_connection_revision: number;
  reviewed_profile_hash: string | null; grant_revision: number; revoked_at: string | null;
};
/** Metadata-only inbound client record; raw tokens are never returned by list calls. */
export type McpInboundClient = {
  id: string; name: string; token_prefix: string; audience: string;
  bindings: { name: string; version: string; schema_fingerprint: string }[];
  source_ids: string[]; capabilities: string[]; expires_at: string; revoked_at: string | null; revision: number;
};
/** Native tool definition used only to select exact supported inbound bindings. */
export type McpNativeTool = {
  name: string; version: string; input_schema: Record<string, unknown>; output_schema: Record<string, unknown>;
  risk: string; permissions: string[]; module: string; timeout_seconds: number; max_result_bytes: number; schema_fingerprint: string;
};
export type McpCollectionConfig = { connection_id: string; calls: { grant_id: string; arguments: Record<string, unknown> }[]; schedule_interval_minutes: 15 | 30 | 60 | 360 | 1440; timezone: string };

/** Calls a same-origin MCP management read without persisting response data in browser storage. */
async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  return apiRequest<T>(path, { signal, cache: 'no-store' });
}
/** Sends a CSRF-protected same-origin mutation; issuance replies remain no-store in the API client. */
async function write<T>(path: string, csrfToken: string, method: string, body?: unknown): Promise<T> {
  return apiRequest<T>(path, {
    method, headers: { ...csrfHeaders(csrfToken), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }), cache: 'no-store',
  });
}
/** Lists the bounded owner connection catalog. */
export function listMcpConnections(signal?: AbortSignal) { return read<{ items: McpConnection[] }>('/api/v1/mcp/connections', signal); }
/** Creates a disabled connection from the explicit owner draft. */
export function createMcpConnection(draft: McpConnectionDraft, csrf: string) { return write<McpConnection>('/api/v1/mcp/connections', csrf, 'POST', draft); }
/** Reads a connection snapshot by its owner-scoped identifier. */
export function getMcpConnection(id: string, signal?: AbortSignal) { return read<McpConnection>(`/api/v1/mcp/connections/${encodeURIComponent(id)}`, signal); }
/** Saves a draft against its expected revision; the server disables the revised connection. */
export function saveMcpConnection(id: string, revision: number, draft: McpConnectionDraft, csrf: string) { return write<McpConnection>(`/api/v1/mcp/connections/${encodeURIComponent(id)}`, csrf, 'PATCH', { expected_revision: revision, draft }); }
/** Checks health for the saved server configuration, never unsaved browser fields. */
export function checkMcpConnection(id: string, csrf: string) { return write<McpConnection>(`/api/v1/mcp/connections/${encodeURIComponent(id)}/draft-check`, csrf, 'POST'); }
/** Discovers immutable descriptors; this POST is the only supported discovery read. */
export function discoverMcpConnection(id: string, csrf: string) { return write<McpDiscovery>(`/api/v1/mcp/connections/${encodeURIComponent(id)}/discover`, csrf, 'POST'); }
/** Lists saved grant metadata, including revoked records, without recovering descriptor bodies. */
export function listMcpGrants(id: string, signal?: AbortSignal) { return read<{ items: McpGrant[] }>(`/api/v1/mcp/connections/${encodeURIComponent(id)}/grants`, signal); }
/** Atomically replaces the full grant set for the exact discovery and connection revision. */
export function replaceMcpGrants(id: string, revision: number, discoveryId: string, selections: McpGrantChoice[], csrf: string) { return write<{ items: McpGrant[] }>(`/api/v1/mcp/connections/${encodeURIComponent(id)}/grants`, csrf, 'PUT', { expected_connection_revision: revision, discovery_id: discoveryId, selections }); }
/** Enables a checked connection only at the expected durable revision. */
export function enableMcpConnection(id: string, revision: number, csrf: string) { return write<McpConnection>(`/api/v1/mcp/connections/${encodeURIComponent(id)}/enable?expected_revision=${revision}`, csrf, 'POST'); }
/** Disables a connection at the expected revision and fences new dispatch. */
export function disableMcpConnection(id: string, revision: number, csrf: string) { return write<McpConnection>(`/api/v1/mcp/connections/${encodeURIComponent(id)}/disable?expected_revision=${revision}`, csrf, 'POST'); }
/** Lists inbound client metadata without any bearer token. */
export function listMcpInboundClients(signal?: AbortSignal) { return read<{ items: McpInboundClient[] }>('/api/v1/mcp/inbound-clients', signal); }
/** Loads the genuine native tool catalog; caller filters supported names and hashes. */
export function listMcpNativeTools(signal?: AbortSignal) { return read<{ items: McpNativeTool[] }>('/api/v1/tools', signal); }
/** Saves scheduled source collection using only grant identities already reviewed for this source. */
export function saveMcpCollection(sourceId: string, generation: number, configuration: McpCollectionConfig, csrf: string) {
  return write<{ source_id: string; source_generation: number; expected_revision: number; configuration: McpCollectionConfig }>(`/api/v1/connectors/sources/${encodeURIComponent(sourceId)}/mcp-collection`, csrf, 'PUT', { expected_generation: generation, configuration });
}
/** Issues an inbound token once; callers must not automatically retry ambiguous outcomes. */
export function createMcpInboundClient(payload: { name: string; audience: string; tool_bindings: McpInboundClient['bindings']; source_ids: string[]; capabilities: string[]; expires_at: string }, csrf: string) {
  return write<{ client: McpInboundClient; token: string }>('/api/v1/mcp/inbound-clients', csrf, 'POST', payload);
}
/** Revokes an inbound client after a separate explicit confirmation. */
export function revokeMcpInboundClient(id: string, csrf: string) { return write<McpInboundClient>(`/api/v1/mcp/inbound-clients/${encodeURIComponent(id)}/revoke`, csrf, 'POST'); }
/** Rotates an inbound bearer token once against the exact client revision. */
export function rotateMcpInboundClient(id: string, revision: number, csrf: string) { return write<{ client: McpInboundClient; token: string }>(`/api/v1/mcp/inbound-clients/${encodeURIComponent(id)}/rotate?expected_revision=${revision}`, csrf, 'POST'); }
