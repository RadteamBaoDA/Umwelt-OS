import { listSources, type Source } from './api';
import { listMcpConnections, listMcpGrants, listMcpInboundClients, listMcpNativeTools, type McpConnection } from './mcp-api';
import { errorKey, fromConnection, supportedInbound } from './mcp-model';
import type { useMcpCore, useMcpDraft, useMcpGrants, useMcpInbound } from './use-mcp-state';

/** The state bundles every MCP async helper reads and writes; stable setters and refs, rebuilt each render. */
export type McpState = {
  core: ReturnType<typeof useMcpCore>;
  drafts: ReturnType<typeof useMcpDraft>;
  grants: ReturnType<typeof useMcpGrants>;
  inbound: ReturnType<typeof useMcpInbound>;
};

/** Sequential catalog page cap (1,000 rows); add server-side search if larger catalogs need complete review. */
const MAX_SOURCE_PAGES = 20;

/** Publication fences of one snapshot read: `owns` ignores the abort signal (used by cleanup), `live` includes it. */
function snapshotFences(state: McpState, requestGeneration: number, requestSession: number, controller: AbortController) {
  const { core } = state;
  const owns = () =>
    core.mounted.current && core.generation.current === requestGeneration && core.sessionGeneration.current === requestSession;
  return { owns, live: () => owns() && !controller.signal.aborted };
}

/**
 * Pages through active remote-safe sources, stopping at the page cap or when the read is cancelled.
 * Resolves the sources and the cursor of the first page not fetched (truthy means the catalog was truncated).
 */
async function fetchRemoteSources(state: McpState, requestGeneration: number, controller: AbortController) {
  const remoteSources: Source[] = [];
  let sourceCursor: string | undefined;
  let sourcePages = 0;
  do {
    const sourceResult = await listSources(sourceCursor);
    remoteSources.push(...sourceResult.items.filter((source) => source.status === 'active' && !source.local_only && !source.retired_at));
    sourceCursor = sourceResult.next_cursor ?? undefined;
    sourcePages += 1;
  } while (
    sourceCursor &&
    sourcePages < MAX_SOURCE_PAGES &&
    !controller.signal.aborted &&
    state.core.generation.current === requestGeneration
  );
  return { remoteSources, sourceCursor };
}

/** Picks the connection to show: the draft owner's when preserving a draft, else the preferred, current or first connection. */
function pickConnection(state: McpState, nextConnections: McpConnection[], preferredId: string | undefined, preserveDraft: boolean) {
  const expectedId = preserveDraft ? state.drafts.draftIdentityRef.current.id : (preferredId ?? state.core.currentId.current);
  return nextConnections.find((item) => item.id === expectedId) ?? (preserveDraft ? null : (nextConnections[0] ?? null));
}

/**
 * Raises a conflict when a preserved draft's owner vanished or was revised: the draft stays attached to its original identity
 * and every reviewed descriptor is dropped. Resolves whether a conflict was detected.
 */
function flagOwnerConflict(state: McpState, next: McpConnection | null): boolean {
  const { core, drafts, grants } = state;
  const owner = drafts.draftIdentityRef.current;
  if (!owner.id || !drafts.hasDraftRef.current) return false;
  if (!next) {
    // Keep the draft attached to its original identity; a missing owner cannot be replaced by a fallback row.
    core.currentId.current = null;
    drafts.setSelected(null);
  } else if (owner.baseRevision === next.revision) {
    return false;
  }
  // A refreshed server revision invalidates the original edit base and every reviewed descriptor.
  grants.setDiscovery(null);
  grants.setGrants([]);
  core.setConflict(true);
  return true;
}

/**
 * Chooses the connection the page shows after a refresh. When preserving a draft, the draft owner's id is authoritative and a
 * missing or revised owner raises `ownerConflictDetected` instead of retargeting the draft to another connection.
 */
function applySelection(state: McpState, nextConnections: McpConnection[], preferredId: string | undefined, preserveDraft: boolean) {
  const { core, drafts } = state;
  const hasOwnerDraft = drafts.hasDraftRef.current;
  const next = pickConnection(state, nextConnections, preferredId, preserveDraft);
  if (!preserveDraft || next) {
    core.currentId.current = next?.id ?? null;
    drafts.setSelected(next);
  }
  const ownerConflictDetected = preserveDraft ? flagOwnerConflict(state, next) : false;
  return { next, ownerConflictDetected, hasOwnerDraft };
}

/** Resets (or, when the owner has no unsaved work, re-seeds) the connection draft after a selection was applied. */
function applyDraftReset(state: McpState, next: McpConnection | null, preserveDraft: boolean, hasOwnerDraft: boolean) {
  const { core, drafts, grants } = state;
  if (!preserveDraft) {
    drafts.setDraft(next ? fromConnection(next) : null);
    drafts.setDraftOwner({ id: next?.id ?? null, baseRevision: next?.revision ?? null });
    drafts.setDirty(false);
    grants.setGrantDirty(false);
    drafts.setEditing(false);
    core.setConflict(false);
    grants.clearGrants();
  } else if (!hasOwnerDraft && next) {
    drafts.setDraft(fromConnection(next));
    drafts.setDraftOwner({ id: next.id, baseRevision: next.revision });
  } else if (!hasOwnerDraft && !next) {
    drafts.setDraft(null);
    drafts.setDraftOwner({ id: null, baseRevision: null });
  }
}

/** Reads the three owner collections in parallel, then the remote-safe source catalog; resolves null when a fence was lost. */
async function readOwnerData(state: McpState, requestGeneration: number, controller: AbortController, live: () => boolean) {
  const [connectionResult, clientResult, toolResult] = await Promise.all([
    listMcpConnections(controller.signal),
    listMcpInboundClients(controller.signal),
    listMcpNativeTools(controller.signal),
  ]);
  if (!live()) return null;
  const { inbound, drafts } = state;
  drafts.setConnections(connectionResult.items);
  inbound.setClients(clientResult.items);
  inbound.setTools(toolResult.items.filter((tool) => supportedInbound.has(tool.name) && tool.risk === 'READ_ONLY'));
  const { remoteSources, sourceCursor } = await fetchRemoteSources(state, requestGeneration, controller);
  if (!live()) return null;
  inbound.setSources(remoteSources);
  inbound.setSourcesTruncated(Boolean(sourceCursor));
  return connectionResult.items;
}

/**
 * Loads uncached connections, clients, tools, remote-safe sources, and selected grants.
 * The boolean result is true only after the complete applicable snapshot was fetched;
 * read generations, abort signals, session generations, and mount state fence late publication.
 * When preserving an active draft, its ID/revision remain immutable and any missing or revised
 * owner invalidates descriptor review instead of retargeting the draft to another connection.
 */
export async function loadSnapshot(state: McpState, preferredId?: string, preserveDraft = false): Promise<boolean> {
  const { core, grants } = state;
  const requestGeneration = ++core.generation.current;
  const requestSession = core.sessionGeneration.current;
  core.activeRead.current?.abort();
  const controller = new AbortController();
  core.activeRead.current = controller;
  const { owns, live } = snapshotFences(state, requestGeneration, requestSession, controller);
  try {
    const nextConnections = await readOwnerData(state, requestGeneration, controller, live);
    if (!nextConnections) return false;
    const { next, ownerConflictDetected, hasOwnerDraft } = applySelection(state, nextConnections, preferredId, preserveDraft);
    applyDraftReset(state, next, preserveDraft, hasOwnerDraft);
    if (next) {
      const grantResult = await listMcpGrants(next.id, controller.signal);
      if (!live() || core.currentId.current !== next.id) return false;
      grants.setGrants(grantResult.items);
    } else if (!preserveDraft) {
      grants.setGrants([]);
    }
    core.setError(ownerConflictDetected ? 'conflict' : '');
    if (owns()) core.setReconciliationRequired(false);
    return true;
  } catch (cause) {
    if (live()) core.setError(errorKey(cause));
    return false;
  } finally {
    if (owns()) core.setLoading(false);
  }
}

/**
 * Clears session-owned snapshots and volatile credentials so a following login cannot inherit prior state.
 * Bumps the session generation first so every in-flight publisher of the ended session is dropped.
 */
export function clearSessionState(state: McpState) {
  const { core, drafts, grants, inbound } = state;
  core.sessionGeneration.current += 1;
  core.cancelReads();
  core.currentId.current = null;
  drafts.setConnections([]);
  drafts.setSelected(null);
  drafts.setDraft(null);
  drafts.setDraftOwner({ id: null, baseRevision: null });
  drafts.setDirty(false);
  grants.setGrantDirty(false);
  drafts.setEditing(false);
  core.setConflict(false);
  grants.clearGrants();
  inbound.setClients([]);
  inbound.setTools([]);
  inbound.setSources([]);
  inbound.setSourcesTruncated(false);
  inbound.resetIssueForm();
  inbound.setDisplayedToken(null);
  inbound.setCopied(false);
  inbound.setTokenAcknowledged(false);
  core.setReconciliationRequired(false);
  core.setError('');
}
