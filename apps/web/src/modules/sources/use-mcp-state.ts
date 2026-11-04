'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { usePagination } from '@/components/ui/pagination';
import type { Source } from './api';
import type { McpConnection, McpDiscovery, McpGrant, McpInboundClient, McpNativeTool } from './mcp-api';
import type { ConfirmState, Draft, GrantDraft, McpMessageKey, TokenView } from './mcp-types';

/** Identity (connection id and base revision) a connection draft is permanently bound to. */
export type DraftIdentity = { id: string | null; baseRevision: number | null };

/** Fresh empty per-capability grant draft used before the owner touches a descriptor row (never shared between rows). */
export function emptyGrantDraft(): GrantDraft {
  return { selected: false, purpose: 'chat', sources: [], destinations: '', expires: '' };
}

/**
 * Request fences and shared status flags of the MCP settings page: the read generation, the session generation (bumped when
 * authentication ends), the mounted flag, the single-flight action lock, the in-flight read controller and the id of the
 * connection the page currently shows. Every async publisher compares against these refs before writing state.
 */
export function useMcpCore() {
  const [confirm, setConfirm] = useState<ConfirmState>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [conflict, setConflict] = useState(false);
  const [reconciliationRequired, setReconciliationRequired] = useState(false);
  const [error, setError] = useState<McpMessageKey | ''>('');
  const generation = useRef(0);
  const sessionGeneration = useRef(0);
  const mounted = useRef(false);
  const actionLock = useRef(false);
  const activeRead = useRef<AbortController | null>(null);
  const currentId = useRef<string | null>(null);

  /** Marks the page mounted so snapshot reads may publish. */
  const markMounted = useCallback(() => {
    mounted.current = true;
  }, []);

  /** Marks the page unmounted and advances every fence so late completions are dropped; aborts the in-flight read (latest controller). */
  const markUnmounted = useCallback(() => {
    mounted.current = false;
    sessionGeneration.current += 1;
    generation.current += 1;
    activeRead.current?.abort();
  }, []);

  /** Advances the read fence and aborts any selection or metadata request already in flight. */
  const cancelReads = useCallback(() => {
    generation.current += 1;
    activeRead.current?.abort();
    activeRead.current = null;
    if (mounted.current) setLoading(false);
  }, []);

  return {
    confirm,
    setConfirm,
    busy,
    setBusy,
    loading,
    setLoading,
    conflict,
    setConflict,
    reconciliationRequired,
    setReconciliationRequired,
    error,
    setError,
    generation,
    sessionGeneration,
    mounted,
    actionLock,
    activeRead,
    currentId,
    cancelReads,
    markMounted,
    markUnmounted,
  };
}

/**
 * Connection list, selection and the connection draft with its immutable owner. The draft owner is mirrored into a ref so
 * async snapshot refreshes can compare against it without retargeting retained input; `hasDraftRef` is kept current by
 * `useMcpDraftGuard` and tells a refresh whether the owner has unsaved work.
 */
export function useMcpDraft() {
  const [connections, setConnections] = useState<McpConnection[]>([]);
  const [selected, setSelected] = useState<McpConnection | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftIdentity, setDraftIdentity] = useState<DraftIdentity>({ id: null, baseRevision: null });
  const [dirty, setDirty] = useState(false);
  const [editing, setEditing] = useState(false);
  const draftIdentityRef = useRef(draftIdentity);
  const hasDraftRef = useRef(false);

  /** Keeps the immutable draft owner available to async refreshes without retargeting retained input. */
  const setDraftOwner = useCallback((identity: DraftIdentity) => {
    draftIdentityRef.current = identity;
    setDraftIdentity(identity);
  }, []);

  return {
    connections,
    setConnections,
    selected,
    setSelected,
    draft,
    setDraft,
    draftIdentity,
    setDraftOwner,
    draftIdentityRef,
    hasDraftRef,
    dirty,
    setDirty,
    editing,
    setEditing,
  };
}

/** Descriptor discovery result, the saved grants of the selected connection and the volatile per-capability grant drafts. */
export function useMcpGrants() {
  const [discovery, setDiscovery] = useState<McpDiscovery | null>(null);
  const capabilityPager = usePagination(discovery?.capabilities ?? []);
  const [grants, setGrants] = useState<McpGrant[]>([]);
  const [grantDrafts, setGrantDrafts] = useState<Record<string, GrantDraft>>({});
  const [grantDirty, setGrantDirty] = useState(false);

  /** Drops the discovery result, saved grants and grant drafts together (they are only meaningful for one connection). */
  const clearGrants = useCallback(() => {
    setDiscovery(null);
    setGrants([]);
    setGrantDrafts({});
  }, []);

  return {
    discovery,
    setDiscovery,
    capabilityPager,
    grants,
    setGrants,
    grantDrafts,
    setGrantDrafts,
    grantDirty,
    setGrantDirty,
    clearGrants,
  };
}

/**
 * Inbound-client issuing state: the clients, native tools and remote-safe sources the owner can scope a client to, the issue
 * form, and the volatile one-time token. `tokenState` mirrors "a token exists / was acknowledged" for the leave guard, and
 * `tokenGeneration` invalidates clipboard completions started for a replaced token.
 */
export function useMcpInbound() {
  const [sources, setSources] = useState<Source[]>([]);
  const [sourcesTruncated, setSourcesTruncated] = useState(false);
  const [clients, setClients] = useState<McpInboundClient[]>([]);
  const [tools, setTools] = useState<McpNativeTool[]>([]);
  const [chosenTools, setChosenTools] = useState<string[]>([]);
  const [clientName, setClientName] = useState('');
  const [clientExpiry, setClientExpiry] = useState('');
  const [clientSourceIds, setClientSourceIds] = useState<string[]>([]);
  const [capabilities, setCapabilities] = useState('');
  const [tokenView, setTokenView] = useState<TokenView>(null);
  const [copied, setCopied] = useState(false);
  const [tokenAcknowledged, setTokenAcknowledged] = useState(false);
  const tokenState = useRef<{ exists: boolean; acknowledged: boolean }>({ exists: false, acknowledged: false });
  const tokenGeneration = useRef(0);

  /** Replaces volatile token text and invalidates any clipboard completion started for its predecessor. */
  const setDisplayedToken = useCallback((value: TokenView) => {
    tokenGeneration.current += 1;
    setTokenView(value);
    tokenState.current = { exists: Boolean(value), acknowledged: false };
  }, []);

  /** Clears the issue form fields (not the token) after a client was issued. */
  const resetIssueForm = useCallback(() => {
    setClientName('');
    setClientExpiry('');
    setClientSourceIds([]);
    setChosenTools([]);
    setCapabilities('');
  }, []);

  /** Records the owner's token-copy acknowledgement in state and in the leave-guard ref (kept together so they never diverge). */
  const markTokenAcknowledged = (acknowledged: boolean) => {
    setTokenAcknowledged(acknowledged);
    tokenState.current = { exists: Boolean(tokenView), acknowledged };
  };

  return {
    sources,
    setSources,
    sourcesTruncated,
    setSourcesTruncated,
    clients,
    setClients,
    tools,
    setTools,
    chosenTools,
    setChosenTools,
    clientName,
    setClientName,
    clientExpiry,
    setClientExpiry,
    clientSourceIds,
    setClientSourceIds,
    capabilities,
    setCapabilities,
    tokenView,
    copied,
    setCopied,
    tokenAcknowledged,
    setTokenAcknowledged,
    tokenState,
    tokenGeneration,
    setDisplayedToken,
    resetIssueForm,
    markTokenAcknowledged,
  };
}

/** Keeps `hasDraftRef` equal to "the owner has unsaved connection, grant or editing work" for async snapshot refreshes. */
export function useMcpDraftGuard(hasDraftRef: { current: boolean }, dirty: boolean, grantDirty: boolean, editing: boolean) {
  useEffect(() => {
    hasDraftRef.current = dirty || grantDirty || editing;
  }, [hasDraftRef, dirty, grantDirty, editing]);
}
