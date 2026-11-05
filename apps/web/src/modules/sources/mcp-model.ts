import { ApiError } from '@/core/api';
import type { McpConnection, McpConnectionDraft, McpDiscovery, McpGrant, McpGrantChoice, McpNativeTool } from './mcp-api';
import type { Draft, GrantDraft } from './mcp-types';

/** Native tool names an inbound client may be bound to (read-only search and knowledge access). */
export const supportedInbound = new Set(['search.query', 'knowledge.get_document', 'knowledge.list_documents']);

/** Converts a server snapshot to an editable draft without retrieving credential contents. */
export function fromConnection(value: McpConnection): Draft {
  return {
    name: value.name,
    transport: value.transport,
    endpoint: value.endpoint ?? '',
    profile: value.deployment_profile_id ?? '',
    auth: value.auth_method,
    credentialAction: 'retain',
    credential: '',
    timeout: String(value.timeout_seconds),
  };
}

/** Blank draft for a brand-new streamable-HTTP connection. */
export function emptyDraft(): Draft {
  return {
    name: '',
    transport: 'streamable_http',
    endpoint: '',
    profile: '',
    auth: 'none',
    credentialAction: 'retain',
    credential: '',
    timeout: '30',
  };
}

/** Converts editor fields to the strict server draft contract and omits irrelevant target fields. */
export function toPayload(value: Draft): McpConnectionDraft {
  const credential_update =
    value.credentialAction === 'replace' ? { action: 'replace' as const, value: value.credential } : { action: value.credentialAction };
  return {
    name: value.name.trim(),
    transport: value.transport,
    auth_method: value.auth,
    credential_update,
    timeout_seconds: Number(value.timeout),
    ...(value.transport === 'stdio' ? { deployment_profile_id: value.profile.trim() } : { endpoint: value.endpoint.trim() }),
  };
}

/** Maps server error states to localized feedback while distinguishing conflicts and committed refresh failures. */
export function errorKey(
  error: unknown,
): 'requestFailed' | 'sessionExpired' | 'conflict' | 'catalogFull' | 'invalidInput' | 'runtimeRefreshFailed' | 'transportUnavailable' {
  if (!(error instanceof ApiError)) return 'requestFailed';
  if (error.status === 401) return 'sessionExpired';
  if (error.status === 409) return error.message.toLowerCase().includes('catalog') ? 'catalogFull' : 'conflict';
  if (error.status === 422) return 'invalidInput';
  if (error.status === 503 && error.code === 'mcp_runtime_refresh_failed') return 'runtimeRefreshFailed';
  if (error.status === 503) return 'transportUnavailable';
  return 'requestFailed';
}

/** Maps server health codes to localized state labels without confusing the connected code with an error. */
export function healthMessageKey(
  value: string | null,
):
  | 'healthConnected'
  | 'healthDisabled'
  | 'healthNeedsReview'
  | 'healthUnavailable'
  | 'healthProtocolError'
  | 'healthAuthError'
  | 'healthTimeout'
  | 'healthUnknown' {
  const keys: Record<string, ReturnType<typeof healthMessageKey>> = {
    connected: 'healthConnected',
    disabled: 'healthDisabled',
    needs_review: 'healthNeedsReview',
    unavailable: 'healthUnavailable',
    protocol_error: 'healthProtocolError',
    auth_error: 'healthAuthError',
    timeout: 'healthTimeout',
  };
  return value ? (keys[value] ?? 'healthUnknown') : 'healthUnknown';
}

/** Maps the supported grant purposes to their translated labels. */
export function grantPurposeMessageKey(value: McpGrant['purpose']): 'purposeChat' | 'purposeCollection' {
  return value === 'collection' ? 'purposeCollection' : 'purposeChat';
}

/** Maps server risk classifications to translated labels while retaining their visible policy meaning. */
export function grantRiskMessageKey(
  value: McpGrant['risk'],
): 'riskReadOnly' | 'riskInternalWrite' | 'riskExternalWrite' | 'riskDestructive' {
  const keys = {
    READ_ONLY: 'riskReadOnly',
    INTERNAL_WRITE: 'riskInternalWrite',
    EXTERNAL_WRITE: 'riskExternalWrite',
    DESTRUCTIVE: 'riskDestructive',
  } as const;
  return keys[value];
}

/** True when a selected grant row has no valid scope: a collection grant needs exactly one source, an expiry must be in the future. */
export function grantDraftsInvalid(grantDrafts: Record<string, GrantDraft>): boolean {
  const drafts = Object.values(grantDrafts);
  if (drafts.some((value) => value.selected && value.purpose === 'collection' && value.sources.length !== 1)) return true;
  return drafts.some((value) => {
    if (!value.selected || !value.expires) return false;
    const time = new Date(value.expires).getTime();
    return !Number.isFinite(time) || time <= Date.now();
  });
}

/** Builds the read-only grant selections the owner ticked; unsupported descriptor kinds are never granted. */
export function buildGrantSelections(discovery: McpDiscovery, grantDrafts: Record<string, GrantDraft>): McpGrantChoice[] {
  return discovery.capabilities.flatMap((item) => {
    const value = grantDrafts[item.id];
    if (!value?.selected || !['tool', 'resource'].includes(item.kind)) return [];
    return [
      {
        capability_id: item.id,
        descriptor_hash: item.descriptor_hash,
        purpose: value.purpose,
        risk: 'READ_ONLY' as const,
        source_ids: value.sources,
        destinations:
          value.purpose === 'collection'
            ? ['local']
            : value.destinations
                .split(',')
                .map((entry) => entry.trim())
                .filter(Boolean),
        ...(value.expires ? { expires_at: new Date(value.expires).toISOString() } : {}),
      },
    ];
  });
}

/** Total UTF-8 byte length of a string list, used against the server's 1,024 byte bound. */
function byteLength(values: string[]): number {
  return values.reduce((sum, value) => sum + new TextEncoder().encode(value).length, 0);
}

/** True when any selection violates the source (1..100) or destination (1..8, unique, <=255 chars, <=1,024 bytes) bounds. */
export function selectionsViolateBounds(selections: McpGrantChoice[]): boolean {
  return selections.some(
    (item) =>
      item.source_ids.length === 0 ||
      item.source_ids.length > 100 ||
      item.destinations.length === 0 ||
      item.destinations.length > 8 ||
      new Set(item.destinations).size !== item.destinations.length ||
      item.destinations.some((value) => !value || value.length > 255) ||
      byteLength(item.destinations) > 1024,
  );
}

/** Validated inbound-client issue form, or null when the form violates a bound (the caller reports `inboundRequired`). */
export function buildInboundIssue(form: {
  clientName: string;
  clientExpiry: string;
  chosenTools: string[];
  clientSourceIds: string[];
  capabilities: string;
  tools: McpNativeTool[];
}) {
  const bindings = form.tools
    .filter((tool) => form.chosenTools.includes(`${tool.name}@${tool.version}`))
    .map(({ name, version, schema_fingerprint }) => ({ name, version, schema_fingerprint }));
  const capabilityValues = form.capabilities
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  const tooLarge =
    form.clientSourceIds.length > 100 ||
    capabilityValues.length > 20 ||
    new Set(capabilityValues).size !== capabilityValues.length ||
    capabilityValues.some((item) => item.length > 80) ||
    byteLength(capabilityValues) > 1024;
  if (!form.clientName.trim() || !form.clientExpiry || bindings.length === 0 || tooLarge) return null;
  const expiresAt = new Date(form.clientExpiry);
  if (!Number.isFinite(expiresAt.getTime()) || expiresAt.getTime() <= Date.now()) return null;
  return {
    name: form.clientName.trim(),
    audience: '/api/v1/mcp/',
    tool_bindings: bindings,
    source_ids: form.clientSourceIds,
    capabilities: capabilityValues,
    expires_at: expiresAt.toISOString(),
  };
}

/** True when a saved grant is usable now: live, read-only, scoped and reviewed against the selected connection and a discovered descriptor. */
function grantIsUsable(grant: McpGrant, selected: McpConnection, discovery: McpDiscovery, now: number): boolean {
  const live = !grant.revoked_at && (!grant.expires_at || new Date(grant.expires_at).getTime() > now);
  const scoped =
    grant.connection_id === selected.id &&
    ['chat', 'collection'].includes(grant.purpose) &&
    grant.risk === 'READ_ONLY' &&
    grant.source_ids.length > 0 &&
    (grant.purpose !== 'collection' || grant.source_ids.length === 1);
  const reviewed =
    grant.reviewed_connection_revision === selected.revision && grant.reviewed_profile_hash === selected.deployment_profile_hash;
  return (
    live &&
    scoped &&
    reviewed &&
    discovery.capabilities.some(
      (capability) => capability.id === grant.capability_id && capability.descriptor_hash === grant.descriptor_hash,
    )
  );
}

/**
 * True when the selected connection may be enabled: not mid-edit, discovery reviewed for exactly this revision and deployment profile,
 * health connected, stdio has a profile, and at least one usable read-only grant exists.
 */
export function computeCanEnable(
  selected: McpConnection | null,
  editing: boolean,
  discovery: McpDiscovery | null,
  grants: McpGrant[],
  now: number,
): boolean {
  if (!selected || editing || !discovery) return false;
  const reviewed =
    discovery.connection_id === selected.id &&
    discovery.connection_revision === selected.revision &&
    discovery.deployment_profile_hash === selected.deployment_profile_hash;
  const transportOk = selected.transport !== 'stdio' || Boolean(selected.deployment_profile_id && selected.deployment_profile_hash);
  return (
    reviewed && selected.health === 'connected' && transportOk && grants.some((grant) => grantIsUsable(grant, selected, discovery, now))
  );
}

/** Identifies the only descriptor kinds that have a currently supported read-only chat dispatch path. */
export function isSupportedCapability(item: McpDiscovery['capabilities'][number]): boolean {
  return ['tool', 'resource'].includes(item.kind);
}
