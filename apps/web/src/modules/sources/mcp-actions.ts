import { ApiError } from '@/core/api';
import {
  checkMcpConnection,
  createMcpConnection,
  createMcpInboundClient,
  discoverMcpConnection,
  listMcpGrants,
  replaceMcpGrants,
  revokeMcpInboundClient,
  rotateMcpInboundClient,
  saveMcpConnection,
  type McpConnection,
  type McpDiscovery,
  type McpGrant,
  type McpInboundClient,
} from './mcp-api';
import {
  buildGrantSelections,
  buildInboundIssue,
  emptyDraft,
  errorKey,
  fromConnection,
  grantDraftsInvalid,
  selectionsViolateBounds,
  toPayload,
} from './mcp-model';
import type { McpState } from './mcp-snapshot';
import type { GrantDraft } from './mcp-types';
import { emptyGrantDraft } from './use-mcp-state';

/** Everything an MCP action reads: the state bundles of the current render plus the CSRF token, translator and snapshot refresh. */
export type McpActionContext = McpState & {
  csrfToken: string;
  t: (key: 'discardDraftConfirm' | 'tokenAckRequired') => string;
  refresh: (preferredId?: string, preserveDraft?: boolean) => Promise<boolean>;
};

/** True while the page is mounted and still on the authenticated session the action started in. */
function sessionIsLive(ctx: McpActionContext, requestSession: number): boolean {
  return ctx.core.mounted.current && ctx.core.sessionGeneration.current === requestSession;
}

/** Re-reads the snapshot preserving the draft after a failed write, then shows `successKey` or `reconciliationFailed`. */
async function reconcileAfterFailure(ctx: McpActionContext, requestSession: number, successKey: ReturnType<typeof errorKey> | 'conflict') {
  const reconciled = await ctx.refresh(ctx.core.currentId.current ?? undefined, true);
  if (!sessionIsLive(ctx, requestSession)) return;
  ctx.core.setError(reconciled ? successKey : 'reconciliationFailed');
}

/** Freezes writes for a conflict or a committed runtime-refresh failure, then reconciles; 404 only reconciles. */
async function recoverFromFailure(ctx: McpActionContext, cause: unknown, key: ReturnType<typeof errorKey>, requestSession: number) {
  if (!(cause instanceof ApiError)) return;
  if (cause.status === 409) ctx.core.setConflict(true);
  else if (cause.status === 503 && cause.code === 'mcp_runtime_refresh_failed') ctx.core.setReconciliationRequired(true);
  else if (cause.status !== 404) return;
  await reconcileAfterFailure(ctx, requestSession, key);
}

/** True when a mutation must not start: another one runs, a read is loading, writes are frozen, or a shown token is unacknowledged. */
function performBlocked(ctx: McpActionContext): boolean {
  const { core, inbound } = ctx;
  if (core.actionLock.current || core.loading || core.reconciliationRequired || core.conflict) return true;
  return Boolean(inbound.tokenView && !inbound.tokenAcknowledged);
}

/**
 * Serializes one owner mutation, publishes only for the initiating live session, and optionally
 * reconciles metadata. Conflicts freeze writes until explicit reload; committed runtime-refresh
 * failures freeze writes until a complete independent metadata refresh succeeds.
 */
export async function perform(
  ctx: McpActionContext,
  operation: () => Promise<unknown>,
  refreshAfter = true,
  preserveDraft = false,
  onSuccess?: (result: unknown) => void,
) {
  const { core } = ctx;
  if (performBlocked(ctx)) return;
  core.actionLock.current = true;
  const requestSession = core.sessionGeneration.current;
  core.setBusy(true);
  core.setError('');
  try {
    const result = await operation();
    if (!sessionIsLive(ctx, requestSession)) return;
    onSuccess?.(result);
    if (!refreshAfter) return;
    const reconciled = await ctx.refresh(core.currentId.current ?? undefined, preserveDraft);
    if (!sessionIsLive(ctx, requestSession)) return;
    if (!reconciled) {
      core.setReconciliationRequired(true);
      core.setError('reconciliationFailed');
    }
  } catch (cause) {
    if (!sessionIsLive(ctx, requestSession)) return;
    const key = errorKey(cause);
    core.setError(key);
    await recoverFromFailure(ctx, cause, key, requestSession);
  } finally {
    core.actionLock.current = false;
    if (core.mounted.current) core.setBusy(false);
  }
}

/** True when a save must not start: nothing to save, another action running, writes frozen, or the draft no longer owns the selection. */
function saveBlocked(ctx: McpActionContext): boolean {
  const { core, drafts } = ctx;
  const { draft, draftIdentity, selected } = drafts;
  if (!draft || core.actionLock.current || core.reconciliationRequired || core.conflict || core.loading) return true;
  return Boolean(draftIdentity.id && (!selected || selected.id !== draftIdentity.id || selected.revision !== draftIdentity.baseRevision));
}

/** Handles a rejected save: keeps the draft, freezes writes for conflicts or committed runtime-refresh failures, then reconciles. */
async function handleSaveFailure(ctx: McpActionContext, cause: unknown, requestSession: number) {
  const { core, drafts } = ctx;
  if (!sessionIsLive(ctx, requestSession)) return;
  core.setError(errorKey(cause));
  if (!(cause instanceof ApiError)) return;
  if (cause.status === 503 && cause.code === 'mcp_runtime_refresh_failed') {
    // The server reports a durable save; clear the one-time input while retaining the other values for explicit review.
    drafts.setDraft((value) =>
      value
        ? { ...value, credential: '', credentialAction: value.credentialAction === 'replace' ? 'retain' : value.credentialAction }
        : value,
    );
    core.setReconciliationRequired(true);
    if (!drafts.draftIdentity.id) core.setConflict(true);
    await reconcileAfterFailure(ctx, requestSession, 'runtimeRefreshFailed');
  } else if (cause.status === 409) {
    core.setConflict(true);
    await reconcileAfterFailure(ctx, requestSession, 'conflict');
  }
}

/** Saves a disabled connection against its immutable draft identity/revision; conflict or ambiguous commit retains the draft for explicit reconciliation. */
export async function save(ctx: McpActionContext) {
  if (saveBlocked(ctx)) return;
  const { core, drafts } = ctx;
  const { draft, draftIdentity } = drafts;
  core.actionLock.current = true;
  const requestSession = core.sessionGeneration.current;
  core.setBusy(true);
  core.setError('');
  try {
    const payload = toPayload(draft!);
    if (draftIdentity.id && draftIdentity.baseRevision === null) return;
    const result = draftIdentity.id
      ? await saveMcpConnection(draftIdentity.id, draftIdentity.baseRevision as number, payload, ctx.csrfToken)
      : await createMcpConnection(payload, ctx.csrfToken);
    if (!sessionIsLive(ctx, requestSession)) return;
    drafts.setDraft((value) => (value ? { ...value, credential: '' } : value));
    drafts.setDirty(false);
    drafts.setEditing(false);
    core.setConflict(false);
    core.currentId.current = result.id;
    const reconciled = await ctx.refresh(result.id);
    if (!sessionIsLive(ctx, requestSession)) return;
    if (!reconciled) {
      core.setReconciliationRequired(true);
      core.setError('reconciliationFailed');
    }
  } catch (cause) {
    await handleSaveFailure(ctx, cause, requestSession);
  } finally {
    core.actionLock.current = false;
    if (core.mounted.current) core.setBusy(false);
  }
}

/** Selects a different connection only after an explicit dirty-draft decision. */
export function choose(ctx: McpActionContext, value: McpConnection) {
  const { core, drafts, grants, inbound } = ctx;
  if (core.busy || core.loading || core.reconciliationRequired || core.conflict || core.confirm || inbound.tokenView) return;
  if ((drafts.dirty || grants.grantDirty) && !window.confirm(ctx.t('discardDraftConfirm'))) return;
  core.cancelReads();
  inbound.setDisplayedToken(null);
  inbound.setTokenAcknowledged(false);
  inbound.setCopied(false);
  core.currentId.current = value.id;
  drafts.setSelected(value);
  drafts.setDraft(fromConnection(value));
  drafts.setDraftOwner({ id: value.id, baseRevision: value.revision });
  drafts.setDirty(false);
  drafts.setEditing(false);
  core.setConflict(false);
  grants.clearGrants();
  void ctx.refresh(value.id);
}

/** Creates a new connection draft and cancels any older selection metadata read before it can publish. */
export function startNewConnection(ctx: McpActionContext) {
  const { core, drafts, grants, inbound } = ctx;
  if (core.busy || drafts.dirty || grants.grantDirty || core.confirm || inbound.tokenView) return;
  core.cancelReads();
  core.currentId.current = null;
  drafts.setSelected(null);
  drafts.setDraftOwner({ id: null, baseRevision: null });
  drafts.setDraft(emptyDraft());
  drafts.setDirty(true);
  drafts.setEditing(true);
  core.setConflict(false);
  grants.clearGrants();
}

/** Checks health for the saved snapshot, not the unsaved browser draft. */
export function check(ctx: McpActionContext) {
  const { selected } = ctx.drafts;
  if (!selected) return;
  void perform(
    ctx,
    () => checkMcpConnection(selected.id, ctx.csrfToken),
    false,
    false,
    (value) => {
      const result = value as McpConnection;
      if (ctx.core.currentId.current !== result.id) return;
      ctx.drafts.setSelected(result);
      ctx.drafts.setConnections((items) => items.map((item) => (item.id === result.id ? result : item)));
    },
  );
}

/** Requires separate owner confirmation before descriptor rediscovery can invalidate grants. */
export function discover(ctx: McpActionContext) {
  if (ctx.drafts.selected && !ctx.core.busy) ctx.core.setConfirm({ action: 'discover' });
}

/** Replaces the saved grants of the selected connection and clears the grant-dirty flag on success. */
function replaceGrants(ctx: McpActionContext, selections: Parameters<typeof replaceMcpGrants>[3]) {
  const { selected } = ctx.drafts;
  const { discovery } = ctx.grants;
  if (!selected || !discovery) return;
  void perform(
    ctx,
    () => replaceMcpGrants(selected.id, selected.revision, discovery.id, selections, ctx.csrfToken),
    false,
    true,
    (value) => {
      ctx.grants.setGrants((value as { items: McpGrant[] }).items);
      ctx.grants.setGrantDirty(false);
    },
  );
}

/** Replaces grants only when every selected read grant has explicit source and destination scopes. */
export function saveGrants(ctx: McpActionContext) {
  const { selected } = ctx.drafts;
  const { discovery, grantDrafts } = ctx.grants;
  if (!selected || !discovery) return;
  if (grantDraftsInvalid(grantDrafts)) {
    ctx.core.setError('grantScopeRequired');
    return;
  }
  const selections = buildGrantSelections(discovery, grantDrafts);
  if (selectionsViolateBounds(selections)) {
    ctx.core.setError('grantScopeRequired');
    return;
  }
  if (selections.length === 0) {
    ctx.core.setConfirm({ action: 'save-empty-grants' });
    return;
  }
  replaceGrants(ctx, selections);
}

/** Issues once from exact native tool fingerprints and explicit active remote-safe scopes. */
export function issue(ctx: McpActionContext) {
  const { core, inbound } = ctx;
  if (inbound.tokenView || core.reconciliationRequired || core.conflict) return;
  const payload = buildInboundIssue(inbound);
  if (!payload) {
    core.setError('inboundRequired');
    return;
  }
  void perform(
    ctx,
    () => createMcpInboundClient(payload, ctx.csrfToken),
    true,
    true,
    (value) => {
      const issued = value as { client: McpInboundClient; token: string };
      inbound.setDisplayedToken({ token: issued.token, client: issued.client });
      inbound.setCopied(false);
      inbound.setTokenAcknowledged(false);
      inbound.resetIssueForm();
    },
  );
}

/** Copies the currently displayed one-time token after an explicit gesture and ignores clipboard completion after session change or unmount. */
export async function copyToken(ctx: McpActionContext) {
  const { core, inbound } = ctx;
  if (!inbound.tokenView) return;
  const requestSession = core.sessionGeneration.current;
  const requestToken = inbound.tokenGeneration.current;
  /** True while the token shown when the copy started is still the displayed one. */
  const stillShown = () =>
    core.mounted.current && core.sessionGeneration.current === requestSession && inbound.tokenGeneration.current === requestToken;
  try {
    await navigator.clipboard.writeText(inbound.tokenView.token);
    if (stillShown()) inbound.setCopied(true);
  } catch {
    if (stillShown()) core.setError('clipboardFailed');
  }
}

/** Rediscovers descriptors for the selected connection and installs the result with fresh grant drafts. */
function confirmDiscover(ctx: McpActionContext) {
  const { selected } = ctx.drafts;
  if (!selected) return;
  void perform(
    ctx,
    async () => {
      const result = await discoverMcpConnection(selected.id, ctx.csrfToken);
      const savedGrants = await listMcpGrants(result.connection_id);
      return { result, grants: savedGrants.items };
    },
    false,
    false,
    (value) => {
      const { result, grants: savedGrants } = value as { result: McpDiscovery; grants: McpGrant[] };
      if (ctx.core.currentId.current !== result.connection_id) return;
      const { grants } = ctx;
      grants.setDiscovery(result);
      grants.setGrantDrafts(Object.fromEntries(result.capabilities.map((item) => [item.id, emptyGrantDraft()])));
      grants.setGrantDirty(false);
      grants.setGrants(savedGrants);
    },
  );
}

/** Applies a rotate or revoke to an inbound client; a token being displayed blocks it (never auto-retried). */
function confirmRotateOrRevoke(ctx: McpActionContext, action: 'rotate' | 'revoke', client: McpInboundClient) {
  const { inbound } = ctx;
  if (inbound.tokenView) return;
  void perform(
    ctx,
    () =>
      action === 'rotate'
        ? rotateMcpInboundClient(client.id, client.revision, ctx.csrfToken)
        : revokeMcpInboundClient(client.id, ctx.csrfToken),
    true,
    true,
    (value) => {
      if (action !== 'rotate') return;
      const result = value as { client: McpInboundClient; token: string };
      inbound.setDisplayedToken({ token: result.token, client: result.client });
      inbound.setCopied(false);
      inbound.setTokenAcknowledged(false);
    },
  );
}

/** Applies the confirmed token action; dismiss clears volatile plaintext and issue/rotate is never auto-retried. */
export function confirmClientAction(ctx: McpActionContext) {
  const { confirm } = ctx.core;
  if (!confirm) return;
  const { action, client } = confirm;
  ctx.core.setConfirm(null);
  if (action === 'save-empty-grants') {
    replaceGrants(ctx, []);
    return;
  }
  if (action === 'discover') {
    confirmDiscover(ctx);
    return;
  }
  if (!client) return;
  const { inbound } = ctx;
  if (action === 'dismiss') {
    if (!inbound.tokenAcknowledged || inbound.tokenView?.client.id !== client.id) return;
    inbound.setDisplayedToken(null);
    inbound.setCopied(false);
    inbound.setTokenAcknowledged(false);
    return;
  }
  confirmRotateOrRevoke(ctx, action, client);
}

/** Updates only volatile descriptor grant draft state and marks it for navigation guarding. */
export function updateGrant(ctx: McpActionContext, id: string, update: Partial<GrantDraft>, inputsFrozen: boolean) {
  if (inputsFrozen || ctx.core.conflict) return;
  ctx.grants.setGrantDirty(true);
  ctx.grants.setGrantDrafts((values) => ({ ...values, [id]: { ...(values[id] ?? emptyGrantDraft()), ...update } }));
}

/** Reloads the server snapshot and discards local drafts after explicit owner confirmation. */
export function reloadServer(ctx: McpActionContext) {
  if (!window.confirm(ctx.t('discardDraftConfirm'))) return;
  ctx.core.cancelReads();
  ctx.core.currentId.current = null;
  void ctx.refresh(undefined, false);
}

/** Refetches metadata without replaying a mutation; only a complete refresh clears gates, and a clean conflict may then resume. */
export async function reconcileMetadata(ctx: McpActionContext) {
  const { core, drafts } = ctx;
  if (core.actionLock.current) return;
  const requestSession = core.sessionGeneration.current;
  const succeeded = await ctx.refresh(core.currentId.current ?? undefined, true);
  if (!sessionIsLive(ctx, requestSession)) return;
  if (!succeeded) {
    core.setReconciliationRequired(true);
    core.setError('reconciliationFailed');
    return;
  }
  if (core.conflict && !drafts.hasDraftRef.current) core.setConflict(false);
}
