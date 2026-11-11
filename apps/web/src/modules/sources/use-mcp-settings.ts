'use client';

import { useCallback, useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react';
import { useTranslations } from 'next-intl';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { useBrowserTimezone } from '../shared/use-browser-timezone';
import * as actions from './mcp-actions';
import { computeCanEnable, isSupportedCapability } from './mcp-model';
import { clearSessionState, loadSnapshot, type McpState } from './mcp-snapshot';
import type { GrantDraft } from './mcp-types';
import { useMcpCore, useMcpDraft, useMcpDraftGuard, useMcpGrants, useMcpInbound } from './use-mcp-state';

export type { Draft, GrantDraft, TokenView, McpMessageKey } from './mcp-types';
export { fromConnection, toPayload, errorKey, healthMessageKey, grantPurposeMessageKey, grantRiskMessageKey } from './mcp-model';

/** Subscribes to a coarse wall clock so expiry checks stay pure during render. */
function useClock(stepMs: number): number {
  return useSyncExternalStore(
    (notify) => {
      const id = window.setInterval(notify, stepMs);
      return () => window.clearInterval(id);
    },
    () => Math.floor(Date.now() / stepMs) * stepMs,
    () => 0,
  );
}

/**
 * Mount lifecycle of the settings page: marks the page mounted, starts the first snapshot read (deferred so it begins from an
 * event-style callback), clears session-owned state when authentication ends, and on unmount advances every fence so
 * late completions are dropped.
 */
function useMcpLifecycle(core: McpState['core'], stateRef: { current: McpState }, load: () => Promise<boolean>) {
  const { markMounted, markUnmounted } = core;
  useEffect(() => {
    markMounted();
    const initialLoad = window.setTimeout(() => {
      void load();
    }, 0);
    const clearSecret = () => clearSessionState(stateRef.current);
    window.addEventListener('umwelt:auth-ending', clearSecret);
    return () => {
      window.clearTimeout(initialLoad);
      markUnmounted();
      window.removeEventListener('umwelt:auth-ending', clearSecret);
    };
  }, [markMounted, markUnmounted, load, stateRef]);
}

/**
 * Navigation guard: unsaved drafts, grant edits or an unacknowledged one-time token block leaving the page; accepting the
 * leave drops the typed credential and the displayed token.
 */
function useMcpLeaveGuard(state: McpState, t: (key: 'discardDraftConfirm' | 'tokenAckRequired') => string) {
  const { registerLeaveGuard } = useGuardedNavigation();
  const { dirty, setDirty, setDraft } = state.drafts;
  const { grantDirty, setGrantDirty } = state.grants;
  const { tokenState, setDisplayedToken } = state.inbound;
  useEffect(
    () =>
      registerLeaveGuard({
        hasUnsavedChanges: () => dirty || grantDirty || tokenState.current.exists,
        confirmDiscard: () => {
          if (tokenState.current.exists && !tokenState.current.acknowledged) {
            window.alert(t('tokenAckRequired'));
            return false;
          }
          return window.confirm(t('discardDraftConfirm'));
        },
        acceptLeave: () => {
          setDirty(false);
          setGrantDirty(false);
          setDraft((value) => (value ? { ...value, credential: '' } : value));
          setDisplayedToken(null);
        },
      }),
    [dirty, grantDirty, registerLeaveGuard, setDisplayedToken, setDirty, setDraft, setGrantDirty, tokenState, t],
  );
}

/**
 * Owns the MCP settings page: composes the core fences, connection-draft, grants and inbound-client state hooks, wires the
 * snapshot lifecycle and leave guard, and binds the action helpers to the current render. Returns the view model the section
 * components render.
 */
export function useMcpSettings() {
  const t = useTranslations('mcp');
  const { csrfToken } = useWorkspaceSession();
  const display = useDisplayPreferences();
  const core = useMcpCore();
  const drafts = useMcpDraft();
  const grants = useMcpGrants();
  const inbound = useMcpInbound();
  const state: McpState = { core, drafts, grants, inbound };
  const stateRef = useRef(state);
  useLayoutEffect(() => {
    stateRef.current = state;
  });
  const load = useCallback((preferredId?: string, preserveDraft = false) => loadSnapshot(stateRef.current, preferredId, preserveDraft), []);
  const { setLoading } = core;
  const refresh = useCallback(
    (preferredId?: string, preserveDraft = false) => {
      setLoading(true);
      return load(preferredId, preserveDraft);
    },
    [load, setLoading],
  );
  useMcpLifecycle(core, stateRef, load);
  useMcpLeaveGuard(state, t);
  useMcpDraftGuard(drafts.hasDraftRef, drafts.dirty, grants.grantDirty, drafts.editing);
  const ctx: actions.McpActionContext = { ...state, csrfToken, t, refresh };
  const now = useClock(30_000);
  const browserTimezone = useBrowserTimezone(display.timezone);
  const inputsFrozen = core.busy || core.loading || core.reconciliationRequired || core.conflict;
  return {
    ...core,
    ...drafts,
    ...grants,
    ...inbound,
    csrfToken,
    display,
    browserTimezone,
    inputsFrozen,
    canEnable: computeCanEnable(drafts.selected, drafts.editing, grants.discovery, grants.grants, now),
    supported: isSupportedCapability,
    choose: (value: Parameters<typeof actions.choose>[1]) => actions.choose(ctx, value),
    startNewConnection: () => actions.startNewConnection(ctx),
    perform: (operation: () => Promise<unknown>, refreshAfter?: boolean, preserveDraft?: boolean, onSuccess?: (result: unknown) => void) =>
      actions.perform(ctx, operation, refreshAfter, preserveDraft, onSuccess),
    save: () => actions.save(ctx),
    check: () => actions.check(ctx),
    discover: () => actions.discover(ctx),
    saveGrants: () => actions.saveGrants(ctx),
    issue: () => actions.issue(ctx),
    copyToken: () => actions.copyToken(ctx),
    confirmClientAction: () => actions.confirmClientAction(ctx),
    updateGrant: (id: string, update: Partial<GrantDraft>) => actions.updateGrant(ctx, id, update, inputsFrozen),
    reloadServer: () => actions.reloadServer(ctx),
    reconcileMetadata: () => actions.reconcileMetadata(ctx),
  };
}
