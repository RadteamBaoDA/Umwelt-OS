import type { McpInboundClient } from './mcp-api';

/** Editable connection form fields; credential contents are never retrieved from the server. */
export type Draft = {
  name: string;
  transport: 'streamable_http' | 'stdio';
  endpoint: string;
  profile: string;
  auth: 'none' | 'bearer';
  credentialAction: 'retain' | 'replace' | 'remove';
  credential: string;
  timeout: string;
};
/** Volatile per-capability grant choice made while reviewing a discovered descriptor. */
export type GrantDraft = { selected: boolean; purpose: 'chat' | 'collection'; sources: string[]; destinations: string; expires: string };
/** One-time plaintext token and the client it belongs to; exists only until acknowledged dismissal. */
export type TokenView = { token: string; client: McpInboundClient } | null;
/** Message keys of the `mcp` catalog used for error and status feedback. */
export type McpMessageKey =
  | 'requestFailed'
  | 'sessionExpired'
  | 'conflict'
  | 'catalogFull'
  | 'invalidInput'
  | 'runtimeRefreshFailed'
  | 'transportUnavailable'
  | 'clipboardFailed'
  | 'inboundRequired'
  | 'grantScopeRequired'
  | 'reconciliationFailed'
  | 'tokenAckRequired';
/** Pending owner confirmation for a destructive or invalidating MCP action. */
export type ConfirmState = { action: 'rotate' | 'revoke' | 'dismiss' | 'save-empty-grants' | 'discover'; client?: McpInboundClient } | null;
