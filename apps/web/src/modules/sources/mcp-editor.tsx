'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { listSources, type Source } from './api';
import {
  checkMcpConnection, createMcpConnection, createMcpInboundClient, discoverMcpConnection,
  disableMcpConnection, enableMcpConnection, listMcpConnections, listMcpGrants,
  listMcpInboundClients, listMcpNativeTools, replaceMcpGrants, revokeMcpInboundClient,
  rotateMcpInboundClient, saveMcpConnection, type McpConnection, type McpConnectionDraft,
  type McpDiscovery, type McpGrant, type McpGrantChoice, type McpInboundClient,
  type McpNativeTool,
} from './mcp-api';

type Draft = { name: string; transport: 'streamable_http' | 'stdio'; endpoint: string; profile: string; auth: 'none' | 'bearer'; credentialAction: 'retain' | 'replace' | 'remove'; credential: string; timeout: string };
type GrantDraft = { selected: boolean; sources: string[]; destinations: string; expires: string };
type TokenView = { token: string; client: McpInboundClient } | null;
type McpMessageKey = 'requestFailed' | 'sessionExpired' | 'conflict' | 'catalogFull' | 'invalidInput' | 'runtimeRefreshFailed' | 'transportUnavailable' | 'clipboardFailed' | 'inboundRequired' | 'grantScopeRequired' | 'reconciliationFailed' | 'tokenAckRequired';
const supportedInbound = new Set(['search.query', 'knowledge.get_document', 'knowledge.list_documents']);

/** Converts a server snapshot to an editable draft without retrieving credential contents. */
function fromConnection(value: McpConnection): Draft {
  return { name: value.name, transport: value.transport, endpoint: value.endpoint ?? '', profile: value.deployment_profile_id ?? '', auth: value.auth_method, credentialAction: 'retain', credential: '', timeout: String(value.timeout_seconds) };
}
/** Converts editor fields to the strict server draft contract and omits irrelevant target fields. */
function toPayload(value: Draft): McpConnectionDraft {
  const credential_update = value.credentialAction === 'replace' ? { action: 'replace' as const, value: value.credential } : { action: value.credentialAction };
  return { name: value.name.trim(), transport: value.transport, auth_method: value.auth, credential_update, timeout_seconds: Number(value.timeout), ...(value.transport === 'stdio' ? { deployment_profile_id: value.profile.trim() } : { endpoint: value.endpoint.trim() }) };
}
/** Maps server error states to localized feedback while distinguishing conflicts and committed refresh failures. */
function errorKey(error: unknown): 'requestFailed' | 'sessionExpired' | 'conflict' | 'catalogFull' | 'invalidInput' | 'runtimeRefreshFailed' | 'transportUnavailable' {
  if (!(error instanceof ApiError)) return 'requestFailed';
  if (error.status === 401) return 'sessionExpired';
  if (error.status === 409) return error.message.toLowerCase().includes('catalog') ? 'catalogFull' : 'conflict';
  if (error.status === 422) return 'invalidInput';
  if (error.status === 503 && error.code === 'mcp_runtime_refresh_failed') return 'runtimeRefreshFailed';
  if (error.status === 503) return 'transportUnavailable';
  return 'requestFailed';
}
/** Formats server instants with the user locale and explicit configured timezone. */
function dateLabel(value: string, locale: string, timezone: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short', timeZone: timezone }).format(new Date(value));
}
/** Maps server health codes to localized state labels without confusing the connected code with an error. */
function healthMessageKey(value: string | null): 'healthConnected' | 'healthDisabled' | 'healthNeedsReview' | 'healthUnavailable' | 'healthProtocolError' | 'healthAuthError' | 'healthTimeout' | 'healthUnknown' {
  const keys: Record<string, ReturnType<typeof healthMessageKey>> = {
    connected: 'healthConnected', disabled: 'healthDisabled', needs_review: 'healthNeedsReview', unavailable: 'healthUnavailable',
    protocol_error: 'healthProtocolError', auth_error: 'healthAuthError', timeout: 'healthTimeout',
  };
  return value ? keys[value] ?? 'healthUnknown' : 'healthUnknown';
}
/** Maps the supported grant purposes to their translated labels. */
function grantPurposeMessageKey(value: McpGrant['purpose']): 'purposeChat' | 'purposeCollection' {
  return value === 'collection' ? 'purposeCollection' : 'purposeChat';
}
/** Maps server risk classifications to translated labels while retaining their visible policy meaning. */
function grantRiskMessageKey(value: McpGrant['risk']): 'riskReadOnly' | 'riskInternalWrite' | 'riskExternalWrite' | 'riskDestructive' {
  const keys = { READ_ONLY: 'riskReadOnly', INTERNAL_WRITE: 'riskInternalWrite', EXTERNAL_WRITE: 'riskExternalWrite', DESTRUCTIVE: 'riskDestructive' } as const;
  return keys[value];
}

/**
 * Renders owner-scoped outbound connections, descriptor review, grants, and inbound clients.
 * Uses the authenticated workspace CSRF token for mutations; the server remains responsible
 * for authorization and permission checks. Draft identity is fixed to its original ID and
 * revision, async work is fenced across selection/session/unmount, and one-time tokens stay
 * in volatile component state until acknowledged dismissal or accepted session/page cleanup.
 */
export function McpSettings() {
  const t = useTranslations('mcp');
  const { csrfToken } = useWorkspaceSession();
  const { registerLeaveGuard } = useGuardedNavigation();
  const display = useDisplayPreferences();
  const [connections, setConnections] = useState<McpConnection[]>([]);
  const [selected, setSelected] = useState<McpConnection | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftIdentity, setDraftIdentity] = useState<{ id: string | null; baseRevision: number | null }>({ id: null, baseRevision: null });
  const [dirty, setDirty] = useState(false);
  const [grantDirty, setGrantDirty] = useState(false);
  const [editing, setEditing] = useState(false);
  const [discovery, setDiscovery] = useState<McpDiscovery | null>(null);
  const [grants, setGrants] = useState<McpGrant[]>([]);
  const [grantDrafts, setGrantDrafts] = useState<Record<string, GrantDraft>>({});
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
  const [confirm, setConfirm] = useState<{ action: 'rotate' | 'revoke' | 'dismiss' | 'save-empty-grants' | 'discover'; client?: McpInboundClient } | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [conflict, setConflict] = useState(false);
  const [reconciliationRequired, setReconciliationRequired] = useState(false);
  const [error, setError] = useState<McpMessageKey | ''>('');
  const [browserTimezone, setBrowserTimezone] = useState(display.timezone);
  const generation = useRef(0);
  const sessionGeneration = useRef(0);
  const mounted = useRef(false);
  const actionLock = useRef(false);
  const activeRead = useRef<AbortController | null>(null);
  const currentId = useRef<string | null>(null);
  const draftIdentityRef = useRef(draftIdentity);
  const hasDraftRef = useRef(false);
  const tokenState = useRef<{ exists: boolean; acknowledged: boolean }>({ exists: false, acknowledged: false });
  const tokenGeneration = useRef(0);

  /** Keeps the immutable draft owner available to async refreshes without retargeting retained input. */
  const setDraftOwner = useCallback((identity: { id: string | null; baseRevision: number | null }) => {
    draftIdentityRef.current = identity;
    setDraftIdentity(identity);
  }, []);

  /** Advances the read fence and aborts any selection or metadata request already in flight. */
  const cancelReads = useCallback(() => {
    generation.current += 1;
    activeRead.current?.abort();
    activeRead.current = null;
    if (mounted.current) setLoading(false);
  }, []);

  /** Replaces volatile token text and invalidates any clipboard completion started for its predecessor. */
  const setDisplayedToken = useCallback((value: TokenView) => {
    tokenGeneration.current += 1;
    setTokenView(value);
    tokenState.current = { exists: Boolean(value), acknowledged: false };
  }, []);

  /**
   * Loads uncached connections, clients, tools, remote-safe sources, and selected grants.
   * The boolean result is true only after the complete applicable snapshot was fetched;
   * read generations, abort signals, session generations, and mount state fence late publication.
   * When preserving an active draft, its ID/revision remain immutable and any missing or revised
   * owner invalidates descriptor review instead of retargeting the draft to another connection.
   */
  const refresh = useCallback(async (preferredId?: string, preserveDraft = false): Promise<boolean> => {
    const requestGeneration = ++generation.current;
    const requestSession = sessionGeneration.current;
    activeRead.current?.abort();
    const controller = new AbortController();
    activeRead.current = controller;
    setLoading(true);
    try {
      const [connectionResult, clientResult, toolResult] = await Promise.all([
        listMcpConnections(controller.signal), listMcpInboundClients(controller.signal), listMcpNativeTools(controller.signal),
      ]);
      if (!mounted.current || controller.signal.aborted || generation.current !== requestGeneration || sessionGeneration.current !== requestSession) return false;
      const nextConnections = connectionResult.items;
      setConnections(nextConnections);
      setClients(clientResult.items);
      setTools(toolResult.items.filter((tool) => supportedInbound.has(tool.name) && tool.risk === 'READ_ONLY'));
      const remoteSources: Source[] = [];
      let sourceCursor: string | undefined;
      let sourcePages = 0;
      do {
        const sourceResult = await listSources(sourceCursor);
        remoteSources.push(...sourceResult.items.filter((source) => source.status === 'active' && !source.local_only && !source.retired_at));
        sourceCursor = sourceResult.next_cursor ?? undefined;
        sourcePages += 1;
      // ponytail: cap the sequential catalog at 1,000 rows; add server-side search if larger catalogs need complete review.
      } while (sourceCursor && sourcePages < 20 && !controller.signal.aborted && generation.current === requestGeneration);
      if (!mounted.current || controller.signal.aborted || generation.current !== requestGeneration || sessionGeneration.current !== requestSession) return false;
      setSources(remoteSources);
      setSourcesTruncated(Boolean(sourceCursor));
      const owner = draftIdentityRef.current;
      const hasOwnerDraft = hasDraftRef.current;
      const expectedId = preserveDraft ? owner.id : (preferredId ?? currentId.current);
      const next = nextConnections.find((item) => item.id === expectedId) ?? (preserveDraft ? null : nextConnections[0] ?? null);
      let ownerConflictDetected = false;
      if (!preserveDraft || next) {
        currentId.current = next?.id ?? null;
        setSelected(next);
      }
      if (preserveDraft && owner.id && !next && hasOwnerDraft) {
        // Keep the draft attached to its original identity; a missing owner cannot be replaced by a fallback row.
        currentId.current = null;
        setSelected(null);
        setDiscovery(null); setGrants([]);
        setConflict(true); ownerConflictDetected = true;
      }
      if (preserveDraft && owner.id && next && owner.baseRevision !== next.revision && hasOwnerDraft) {
        // A refreshed server revision invalidates the original edit base and every reviewed descriptor.
        setConflict(true); setDiscovery(null); setGrants([]); ownerConflictDetected = true;
      }
      if (!preserveDraft) {
        setDraft(next ? fromConnection(next) : null);
        setDraftOwner({ id: next?.id ?? null, baseRevision: next?.revision ?? null });
        setDirty(false);
        setGrantDirty(false);
        setEditing(false);
        setConflict(false);
      }
      if (!preserveDraft) {
        setDiscovery(null);
        setGrants([]);
        setGrantDrafts({});
      } else if (!hasOwnerDraft && next) {
        setDraft(fromConnection(next)); setDraftOwner({ id: next.id, baseRevision: next.revision });
      } else if (!hasOwnerDraft && !next) {
        setDraft(null); setDraftOwner({ id: null, baseRevision: null });
      }
      if (next) {
        const grantResult = await listMcpGrants(next.id, controller.signal);
        if (!mounted.current || controller.signal.aborted || generation.current !== requestGeneration || sessionGeneration.current !== requestSession || currentId.current !== next.id) return false;
        setGrants(grantResult.items);
      } else if (!preserveDraft) {
        setGrants([]);
      }
      setError(ownerConflictDetected ? 'conflict' : '');
      if (mounted.current && generation.current === requestGeneration && sessionGeneration.current === requestSession) setReconciliationRequired(false);
      return true;
    } catch (cause) {
      if (!controller.signal.aborted && mounted.current && generation.current === requestGeneration && sessionGeneration.current === requestSession) setError(errorKey(cause));
      return false;
    } finally {
      if (mounted.current && generation.current === requestGeneration && sessionGeneration.current === requestSession) setLoading(false);
    }
  }, [setDraftOwner]);

  useEffect(() => {
    setBrowserTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone || display.timezone);
  }, [display.timezone]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    /** Clears session-owned snapshots and volatile credentials so a following login cannot inherit prior state. */
    const clearSecret = () => {
      sessionGeneration.current += 1; cancelReads(); currentId.current = null;
      setConnections([]); setSelected(null); setDraft(null); setDraftOwner({ id: null, baseRevision: null });
      setDirty(false); setGrantDirty(false); setEditing(false); setConflict(false);
      setDiscovery(null); setGrants([]); setGrantDrafts({}); setClients([]); setTools([]); setSources([]);
      setSourcesTruncated(false); setChosenTools([]); setClientSourceIds([]); setClientName(''); setClientExpiry(''); setCapabilities('');
      setDisplayedToken(null); setCopied(false); setTokenAcknowledged(false); setReconciliationRequired(false); setError('');
    };
    window.addEventListener('bbd:auth-ending', clearSecret);
    return () => { mounted.current = false; sessionGeneration.current += 1; generation.current += 1; activeRead.current?.abort(); window.removeEventListener('bbd:auth-ending', clearSecret); };
  }, [cancelReads, refresh, setDisplayedToken, setDraftOwner]);

  useEffect(() => registerLeaveGuard({
    hasUnsavedChanges: () => dirty || grantDirty || tokenState.current.exists,
    confirmDiscard: () => {
      if (tokenState.current.exists && !tokenState.current.acknowledged) { window.alert(t('tokenAckRequired')); return false; }
      return window.confirm(t('discardDraftConfirm'));
    },
    acceptLeave: () => { setDirty(false); setGrantDirty(false); setDraft((value) => value ? { ...value, credential: '' } : value); setDisplayedToken(null); },
  }), [dirty, grantDirty, registerLeaveGuard, t]);

  useEffect(() => {
    hasDraftRef.current = dirty || grantDirty || editing;
  }, [dirty, grantDirty, editing]);

  /** Selects a different connection only after an explicit dirty-draft decision. */
  const choose = (value: McpConnection) => {
    if (busy || loading || reconciliationRequired || conflict || confirm || tokenView) return;
    if ((dirty || grantDirty) && !window.confirm(t('discardDraftConfirm'))) return;
    cancelReads();
    setDisplayedToken(null); setTokenAcknowledged(false); setCopied(false);
    currentId.current = value.id;
    setSelected(value); setDraft(fromConnection(value)); setDraftOwner({ id: value.id, baseRevision: value.revision }); setDirty(false); setEditing(false); setConflict(false); setDiscovery(null); setGrants([]); setGrantDrafts({});
    void refresh(value.id);
  };
  /** Creates a new connection draft and cancels any older selection metadata read before it can publish. */
  const startNewConnection = () => {
    if (busy || dirty || grantDirty || confirm || tokenView) return;
    cancelReads();
    currentId.current = null;
    setSelected(null); setDraftOwner({ id: null, baseRevision: null });
    setDraft({ name: '', transport: 'streamable_http', endpoint: '', profile: '', auth: 'none', credentialAction: 'retain', credential: '', timeout: '30' });
    setDirty(true); setEditing(true); setConflict(false); setDiscovery(null); setGrants([]); setGrantDrafts({});
  };
  /**
   * Serializes one owner mutation, publishes only for the initiating live session, and optionally
   * reconciles metadata. Conflicts freeze writes until explicit reload; committed runtime-refresh
   * failures freeze writes until a complete independent metadata refresh succeeds.
   */
  const perform = async (operation: () => Promise<unknown>, refreshAfter = true, preserveDraft = false, onSuccess?: (result: unknown) => void) => {
    if (actionLock.current || loading || reconciliationRequired || conflict || (tokenView && !tokenAcknowledged)) return;
    actionLock.current = true;
    const requestSession = sessionGeneration.current;
    setBusy(true); setError('');
    try {
      const result = await operation();
      if (!mounted.current || sessionGeneration.current !== requestSession) return;
      onSuccess?.(result);
      if (refreshAfter) {
        const reconciled = await refresh(currentId.current ?? undefined, preserveDraft);
        if (!mounted.current || sessionGeneration.current !== requestSession) return;
        if (!reconciled) { setReconciliationRequired(true); setError('reconciliationFailed'); }
      }
    }
    catch (cause) {
      if (!mounted.current || sessionGeneration.current !== requestSession) return;
      const key = errorKey(cause); setError(key);
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(true);
        const reconciled = await refresh(currentId.current ?? undefined, true);
        if (!mounted.current || sessionGeneration.current !== requestSession) return;
        if (!reconciled) setError('reconciliationFailed');
        else setError(key);
      }
      if (cause instanceof ApiError && cause.status === 503 && cause.code === 'mcp_runtime_refresh_failed') {
        setReconciliationRequired(true);
        const reconciled = await refresh(currentId.current ?? undefined, true);
        if (!mounted.current || sessionGeneration.current !== requestSession) return;
        if (!reconciled) setError('reconciliationFailed');
        else setError(key);
      }
      if (cause instanceof ApiError && cause.status === 404) {
        const reconciled = await refresh(currentId.current ?? undefined, true);
        if (!mounted.current || sessionGeneration.current !== requestSession) return;
        if (!reconciled) setError('reconciliationFailed');
        else setError(key);
      }
    } finally {
      actionLock.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  /** Saves a disabled connection against its immutable draft identity/revision; conflict or ambiguous commit retains the draft for explicit reconciliation. */
  const save = async () => {
    if (!draft || actionLock.current || reconciliationRequired || conflict || loading || (draftIdentity.id && (!selected || selected.id !== draftIdentity.id || selected.revision !== draftIdentity.baseRevision))) return;
    actionLock.current = true;
    const requestSession = sessionGeneration.current;
    setBusy(true); setError('');
    try {
      const payload = toPayload(draft);
      if (draftIdentity.id && draftIdentity.baseRevision === null) return;
      const result = draftIdentity.id
        ? await saveMcpConnection(draftIdentity.id, draftIdentity.baseRevision as number, payload, csrfToken)
        : await createMcpConnection(payload, csrfToken);
      if (!mounted.current || sessionGeneration.current !== requestSession) return;
      setDraft((value) => value ? { ...value, credential: '' } : value); setDirty(false); setEditing(false); setConflict(false); currentId.current = result.id;
      const reconciled = await refresh(result.id);
      if (!mounted.current || sessionGeneration.current !== requestSession) return;
      if (!reconciled) { setReconciliationRequired(true); setError('reconciliationFailed'); }
    } catch (cause) {
      if (!mounted.current || sessionGeneration.current !== requestSession) return;
      setError(errorKey(cause));
      if (cause instanceof ApiError && cause.status === 503 && cause.code === 'mcp_runtime_refresh_failed') {
        // The server reports a durable save; clear the one-time input while retaining the other values for explicit review.
        setDraft((value) => value ? { ...value, credential: '', credentialAction: value.credentialAction === 'replace' ? 'retain' : value.credentialAction } : value);
        setReconciliationRequired(true);
        if (!draftIdentity.id) setConflict(true);
      }
      if (cause instanceof ApiError && cause.status === 409) {
        setConflict(true);
        const reconciled = await refresh(currentId.current ?? undefined, true);
        if (!mounted.current || sessionGeneration.current !== requestSession) return;
        if (!reconciled) setError('reconciliationFailed');
        else setError('conflict');
      }
      if (cause instanceof ApiError && cause.status === 503 && cause.code === 'mcp_runtime_refresh_failed') {
        const reconciled = await refresh(currentId.current ?? undefined, true);
        if (!mounted.current || sessionGeneration.current !== requestSession) return;
        if (!reconciled) setError('reconciliationFailed');
        else setError('runtimeRefreshFailed');
      }
    } finally { actionLock.current = false; if (mounted.current) setBusy(false); }
  };
  /** Checks health for the saved snapshot, not the unsaved browser draft. */
  const check = () => selected && void perform(() => checkMcpConnection(selected.id, csrfToken), false, false, (value) => {
    const result = value as McpConnection;
    if (currentId.current !== result.id) return;
    setSelected(result); setConnections((items) => items.map((item) => item.id === result.id ? result : item));
  });
  /** Requires separate owner confirmation before descriptor rediscovery can invalidate grants. */
  const discover = () => {
    if (selected && !busy) setConfirm({ action: 'discover' });
  };
  /** Replaces grants only when every selected read grant has explicit source and destination scopes. */
  const saveGrants = () => {
    if (!selected || !discovery) return;
    const invalidExpiry = Object.values(grantDrafts).some((value) => {
      if (!value.selected || !value.expires) return false;
      const time = new Date(value.expires).getTime();
      return !Number.isFinite(time) || time <= Date.now();
    });
    if (invalidExpiry) { setError('grantScopeRequired'); return; }
    const selections: McpGrantChoice[] = discovery.capabilities.flatMap((item) => {
      const value = grantDrafts[item.id];
      if (!value?.selected || !['tool', 'resource'].includes(item.kind)) return [];
      return [{ capability_id: item.id, descriptor_hash: item.descriptor_hash, purpose: 'chat', risk: 'READ_ONLY', source_ids: value.sources, destinations: value.destinations.split(',').map((entry) => entry.trim()).filter(Boolean), ...(value.expires ? { expires_at: new Date(value.expires).toISOString() } : {}) }];
    });
    if (selections.some((item) => item.source_ids.length === 0 || item.source_ids.length > 100 || item.destinations.length === 0 || item.destinations.length > 8 || new Set(item.destinations).size !== item.destinations.length || item.destinations.some((value) => !value || value.length > 255) || item.destinations.reduce((sum, value) => sum + new TextEncoder().encode(value).length, 0) > 1024)) { setError('grantScopeRequired'); return; }
    if (selections.length === 0) { setConfirm({ action: 'save-empty-grants' }); return; }
    void perform(() => replaceMcpGrants(selected.id, selected.revision, discovery.id, selections, csrfToken), false, true, (value) => {
      setGrants((value as { items: McpGrant[] }).items); setGrantDirty(false);
    });
  };
  /** Issues once from exact native tool fingerprints and explicit active remote-safe scopes. */
  const issue = () => {
    if (tokenView || reconciliationRequired || conflict) return;
    const bindings = tools.filter((tool) => chosenTools.includes(`${tool.name}@${tool.version}`)).map(({ name, version, schema_fingerprint }) => ({ name, version, schema_fingerprint }));
    const capabilityValues = capabilities.split(',').map((item) => item.trim()).filter(Boolean);
    if (!clientName.trim() || !clientExpiry || bindings.length === 0 || clientSourceIds.length > 100 || capabilityValues.length > 20 || new Set(capabilityValues).size !== capabilityValues.length || capabilityValues.some((item) => item.length > 80) || capabilityValues.reduce((sum, item) => sum + new TextEncoder().encode(item).length, 0) > 1024) { setError('inboundRequired'); return; }
    const expiresAt = new Date(clientExpiry);
    if (!Number.isFinite(expiresAt.getTime()) || expiresAt.getTime() <= Date.now()) { setError('inboundRequired'); return; }
    const expires_at = expiresAt.toISOString();
    void perform(() => createMcpInboundClient({ name: clientName.trim(), audience: '/api/v1/mcp/', tool_bindings: bindings, source_ids: clientSourceIds, capabilities: capabilityValues, expires_at }, csrfToken), true, true, (value) => {
      const issued = value as { client: McpInboundClient; token: string };
      setDisplayedToken({ token: issued.token, client: issued.client });
      setCopied(false); setTokenAcknowledged(false); setClientName(''); setClientExpiry(''); setClientSourceIds([]); setChosenTools([]); setCapabilities('');
    });
  };
  /** Copies the currently displayed one-time token after an explicit gesture and ignores clipboard completion after session change or unmount. */
  const copyToken = async () => {
    if (!tokenView) return;
    const requestSession = sessionGeneration.current;
    const requestToken = tokenGeneration.current;
    try { await navigator.clipboard.writeText(tokenView.token); if (mounted.current && sessionGeneration.current === requestSession && tokenGeneration.current === requestToken) setCopied(true); }
    catch { if (mounted.current && sessionGeneration.current === requestSession && tokenGeneration.current === requestToken) setError('clipboardFailed'); }
  };
  /** Applies the confirmed token action; dismiss clears volatile plaintext and issue/rotate is never auto-retried. */
  const confirmClientAction = () => {
    if (!confirm) return;
    const { action, client } = confirm; setConfirm(null);
    if (action === 'save-empty-grants') {
      if (!selected || !discovery) return;
      void perform(() => replaceMcpGrants(selected.id, selected.revision, discovery.id, [], csrfToken), false, true, (value) => {
        setGrants((value as { items: McpGrant[] }).items); setGrantDirty(false);
      });
      return;
    }
    if (action === 'discover') {
      if (!selected) return;
      void perform(async () => {
        const result = await discoverMcpConnection(selected.id, csrfToken);
        const savedGrants = await listMcpGrants(result.connection_id);
        return { result, grants: savedGrants.items };
      }, false, false, (value) => {
        const { result, grants: savedGrants } = value as { result: McpDiscovery; grants: McpGrant[] };
        if (currentId.current !== result.connection_id) return;
        setDiscovery(result);
        setGrantDrafts(Object.fromEntries(result.capabilities.map((item) => [item.id, { selected: false, sources: [], destinations: '', expires: '' }])));
        setGrantDirty(false); setGrants(savedGrants);
      });
      return;
    }
    if (!client) return;
    if (action === 'dismiss') {
      if (!tokenAcknowledged || tokenView?.client.id !== client.id) return;
      setDisplayedToken(null); setCopied(false); setTokenAcknowledged(false); return;
    }
    if (tokenView) return;
    void perform(() => action === 'rotate' ? rotateMcpInboundClient(client.id, client.revision, csrfToken) : revokeMcpInboundClient(client.id, csrfToken), true, true, (value) => {
      if (action === 'rotate') {
        const result = value as { client: McpInboundClient; token: string };
        setDisplayedToken({ token: result.token, client: result.client });
        setCopied(false); setTokenAcknowledged(false);
      }
    });
  };
  /** Updates only volatile descriptor grant draft state and marks it for navigation guarding. */
  const updateGrant = (id: string, update: Partial<GrantDraft>) => { if (inputsFrozen || conflict) return; setGrantDirty(true); setGrantDrafts((values) => ({ ...values, [id]: { ...(values[id] ?? { selected: false, sources: [], destinations: '', expires: '' }), ...update } })); };
  /** Identifies the only descriptor kinds that have a currently supported read-only chat dispatch path. */
  const supported = (item: NonNullable<typeof discovery>['capabilities'][number]) => ['tool', 'resource'].includes(item.kind);
  /** Reloads the server snapshot and discards local drafts after explicit owner confirmation. */
  const reloadServer = () => {
    if (!window.confirm(t('discardDraftConfirm'))) return;
    cancelReads(); currentId.current = null;
    void refresh(undefined, false);
  };
  /** Refetches metadata without replaying a mutation; only a complete refresh clears gates, and a clean conflict may then resume. */
  const reconcileMetadata = async () => {
    if (actionLock.current) return;
    const requestSession = sessionGeneration.current;
    const succeeded = await refresh(currentId.current ?? undefined, true);
    if (!mounted.current || sessionGeneration.current !== requestSession) return;
    if (!succeeded) { setReconciliationRequired(true); setError('reconciliationFailed'); return; }
    if (conflict && !hasDraftRef.current) setConflict(false);
  };
  const canEnable = Boolean(selected && !editing && discovery && discovery.connection_id === selected.id && discovery.connection_revision === selected.revision && discovery.deployment_profile_hash === selected.deployment_profile_hash && selected.health === 'connected' && (selected.transport !== 'stdio' || Boolean(selected.deployment_profile_id && selected.deployment_profile_hash)) && grants.some((grant) => !grant.revoked_at && (!grant.expires_at || new Date(grant.expires_at).getTime() > Date.now()) && grant.connection_id === selected.id && grant.purpose === 'chat' && grant.risk === 'READ_ONLY' && grant.reviewed_connection_revision === selected.revision && grant.reviewed_profile_hash === selected.deployment_profile_hash && discovery.capabilities.some((capability) => capability.id === grant.capability_id && capability.descriptor_hash === grant.descriptor_hash)));
  const inputsFrozen = busy || loading || reconciliationRequired || conflict;

  return <section className="mt-8 border-t border-border pt-8 space-y-6" aria-labelledby="mcp-settings-title">
    <header><h2 id="mcp-settings-title">{t('title')}</h2><p className="muted">{t('description')}</p></header>
    {error && <p className="error" role="alert">{t(error as McpMessageKey)}</p>}
    {(reconciliationRequired || conflict) && <p className="muted" role="status">{t(conflict ? 'conflict' : 'reconciliationRequired')} <Button className="secondary" disabled={busy || loading} onClick={() => void reconcileMetadata()}>{t('refreshMetadata')}</Button></p>}
    {(dirty || grantDirty) && <p className="muted" role="status">{t('draftRetained')} <Button className="secondary" disabled={busy} onClick={reloadServer}>{t('reloadDiscard')}</Button></p>}
    <section className="sub-panel space-y-4" aria-labelledby="mcp-connections-title">
      <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 id="mcp-connections-title">{t('connections')}</h3><p className="muted">{t('connectionOrder')}</p></div><Button disabled={busy || dirty || grantDirty || Boolean(confirm) || Boolean(tokenView)} onClick={startNewConnection}>{t('newConnection')}</Button></div>
      {loading ? <p role="status">{t('loading')}</p> : connections.length === 0 ? <p className="empty-state">{t('emptyConnections')}</p> : <ul className="record-list">{connections.map((item) => <li key={item.id} className="record-row flex flex-wrap items-center justify-between gap-3"><div><strong>{item.name}</strong><p className="muted">{t(item.transport === 'stdio' ? 'transportStdio' : 'transportStreamableHttp')} · {t(item.enabled ? 'enabled' : 'disabled')} · {t('revision', { revision: item.revision })}</p>{item.transport === 'stdio' && <p className="muted">{t('profileHash')}: <code>{item.deployment_profile_hash ?? t('unknown')}</code></p>}</div><Button className="secondary" disabled={busy || loading || reconciliationRequired || Boolean(confirm) || Boolean(tokenView)} aria-pressed={selected?.id === item.id} onClick={() => choose(item)}>{t(selected?.id === item.id ? 'selected' : 'select')}</Button></li>)}</ul>}
      {(selected || (editing && draft)) && <div className="space-y-3 rounded-md border border-border p-4">{selected && <div className="flex flex-wrap justify-between gap-2"><div><h4>{selected.name}</h4><p className="muted">{t('health')}: {t(healthMessageKey(selected.health))} · {selected.error_code && selected.error_code !== 'connected' ? <>{t('reportedCode')}: <code>{selected.error_code}</code></> : t('noError')}</p><p className="muted">{t('updatedAt')}: {dateLabel(selected.updated_at, display.locale === 'vi-vi' ? 'vi-VN' : 'en-US', display.timezone)} · {display.timezone}</p></div><div className="flex flex-wrap gap-2"><Button className="secondary" disabled={inputsFrozen || dirty || grantDirty || Boolean(confirm)} onClick={check}>{t('checkSaved')}</Button><Button className="secondary" disabled={inputsFrozen || dirty || grantDirty || Boolean(confirm)} onClick={discover}>{t('discover')}</Button><Button className="secondary" disabled={inputsFrozen || dirty || grantDirty || Boolean(confirm) || Boolean(tokenView)} onClick={() => { setDraft(fromConnection(selected)); setDraftOwner({ id: selected.id, baseRevision: selected.revision }); setEditing(true); setDirty(false); setConflict(false); }}>{t('edit')}</Button>{selected.enabled ? <Button className="secondary" disabled={inputsFrozen || dirty || grantDirty || Boolean(tokenView)} onClick={() => void perform(() => disableMcpConnection(selected.id, selected.revision, csrfToken))}>{t('disable')}</Button> : <Button disabled={inputsFrozen || dirty || grantDirty || Boolean(tokenView) || !canEnable} onClick={() => void perform(() => enableMcpConnection(selected.id, selected.revision, csrfToken))}>{t('enable')}</Button>}</div></div>}
      {editing && draft && <div className="grid gap-3 sm:grid-cols-2" aria-label={t('connectionEditor')}>
        <div><Label htmlFor="mcp-name">{t('name')}</Label><Input disabled={inputsFrozen} id="mcp-name" value={draft.name} onChange={(e) => { setDraft({ ...draft, name: e.target.value }); setDirty(true); }} maxLength={120} /></div>
        <div><Label htmlFor="mcp-transport">{t('transport')}</Label><Select disabled={inputsFrozen} value={draft.transport} onValueChange={(value: Draft['transport']) => { setDraft({ ...draft, transport: value, endpoint: '', profile: '', auth: value === 'stdio' ? 'none' : draft.auth, credentialAction: value === 'stdio' && draft.auth === 'bearer' ? 'remove' : draft.credentialAction }); setDirty(true); }}><SelectTrigger id="mcp-transport"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="streamable_http">{t('transportStreamableHttp')}</SelectItem><SelectItem value="stdio">{t('transportStdio')}</SelectItem></SelectContent></Select></div>
        {draft.transport === 'streamable_http' ? <div><Label htmlFor="mcp-endpoint">{t('endpoint')}</Label><Input disabled={inputsFrozen} id="mcp-endpoint" value={draft.endpoint} onChange={(e) => { setDraft({ ...draft, endpoint: e.target.value }); setDirty(true); }} maxLength={2048} /></div> : <div><Label htmlFor="mcp-profile">{t('deploymentProfile')}</Label><Input disabled={inputsFrozen} id="mcp-profile" value={draft.profile} onChange={(e) => { setDraft({ ...draft, profile: e.target.value }); setDirty(true); }} maxLength={80} /><p className="muted">{t('profileAdminOnly')}</p></div>}
        <div><Label htmlFor="mcp-auth">{t('authentication')}</Label><Select disabled={inputsFrozen || draft.transport === 'stdio'} value={draft.auth} onValueChange={(value: Draft['auth']) => { setDraft({ ...draft, auth: value, credentialAction: value === 'none' ? 'remove' : draft.credentialAction === 'remove' ? 'replace' : draft.credentialAction }); setDirty(true); }}><SelectTrigger id="mcp-auth"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">{t('authNone')}</SelectItem><SelectItem value="bearer">{t('authBearer')}</SelectItem></SelectContent></Select></div>
        {draft.auth === 'bearer' && <><div><Label htmlFor="mcp-credential-action">{t('credentialAction')}</Label><Select disabled={inputsFrozen} value={draft.credentialAction} onValueChange={(value: Draft['credentialAction']) => { setDraft({ ...draft, credentialAction: value, credential: '' }); setDirty(true); }}><SelectTrigger id="mcp-credential-action"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="retain">{t('retain')}</SelectItem><SelectItem value="replace">{t('replace')}</SelectItem><SelectItem value="remove" disabled>{t('removeUnavailable')}</SelectItem></SelectContent></Select></div>{draft.credentialAction === 'replace' && <div><Label htmlFor="mcp-secret">{t('credential')}</Label><Input disabled={inputsFrozen} id="mcp-secret" type="password" autoComplete="new-password" value={draft.credential} onChange={(e) => { setDraft({ ...draft, credential: e.target.value }); setDirty(true); }} /></div>}</>}
        <div><Label htmlFor="mcp-timeout">{t('timeout')}</Label><Input disabled={inputsFrozen} id="mcp-timeout" type="number" min={1} max={60} value={draft.timeout} onChange={(e) => { setDraft({ ...draft, timeout: e.target.value }); setDirty(true); }} /></div>
        {draft.transport === 'stdio' && selected?.credential_configured && <p className="muted sm:col-span-2">{t('stdioCredentialMustRemove')} <label className="flex items-center gap-2"><Checkbox disabled={inputsFrozen} checked={draft.credentialAction === 'remove'} onCheckedChange={(checked) => { setDraft({ ...draft, credentialAction: checked ? 'remove' : 'retain' }); setDirty(true); }} />{t('removeStoredCredential')}</label></p>}
        <p className="muted sm:col-span-2">{t('saveDisables')}</p><div className="flex gap-2 sm:col-span-2"><Button disabled={inputsFrozen || !dirty} onClick={() => void save()}>{t('save')}</Button><Button className="secondary" disabled={busy} onClick={() => { setDraft(selected ? fromConnection(selected) : null); setDraftOwner({ id: selected?.id ?? null, baseRevision: selected?.revision ?? null }); setDirty(false); setEditing(false); setConflict(false); }}>{t('cancel')}</Button></div>
      </div>}
      {grants.length > 0 && <div><h4>{t('savedGrants')}</h4><ul className="record-list">{grants.map((grant) => <li key={grant.id} className="record-row"><strong>{t(grantPurposeMessageKey(grant.purpose))} · {t(grantRiskMessageKey(grant.risk))} · {grant.revoked_at ? t('revoked') : t('grantLive')}</strong><p className="muted">{t('reviewIdentity')}: r{grant.reviewed_connection_revision} · {grant.descriptor_hash} · {grant.destinations.join(', ')}</p></li>)}</ul><p className="muted">{t('rediscoverToReview')}</p></div>}
      {discovery && <div className="space-y-3"><h4>{t('descriptorReview')}</h4><p className="muted">{t('discoveryIdentity', { revision: discovery.connection_revision, schema: discovery.schema_set_hash, profile: discovery.deployment_profile_hash ?? t('notApplicable') })}</p>
        <p className="muted">{t('sourceScopeNote')} {t('scopeBoundsHelp')}</p>
        {discovery.capabilities.map((item) => { const value = grantDrafts[item.id] ?? { selected: false, sources: [], destinations: '', expires: '' }; return <article key={item.id} className="rounded-md border border-border p-3 space-y-2"><div className="flex items-start gap-2"><Checkbox id={`grant-${item.id}`} checked={value.selected} disabled={inputsFrozen || !supported(item)} onCheckedChange={(checked) => updateGrant(item.id, { selected: Boolean(checked) })} /><Label htmlFor={`grant-${item.id}`}>{item.kind}: {item.remote_key}</Label><span className="muted">{supported(item) ? t('readOnlyGrant') : t('unsupportedKind')}</span></div><p className="muted">{t('descriptorHash')}: <code>{item.descriptor_hash}</code></p><pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2 text-xs">{JSON.stringify(item.descriptor, null, 2)}</pre>{value.selected && supported(item) && <div className="grid gap-2 sm:grid-cols-2"><fieldset><legend>{t('sourceScopes')}</legend>{sources.map((source) => <label key={source.id} className="flex items-center gap-2"><Checkbox disabled={inputsFrozen} checked={value.sources.includes(source.id)} onCheckedChange={(checked) => updateGrant(item.id, { sources: checked ? [...value.sources, source.id] : value.sources.filter((id) => id !== source.id) })} />{source.name}</label>)}</fieldset><div><Label htmlFor={`dest-${item.id}`}>{t('destinations')}</Label><Input disabled={inputsFrozen} id={`dest-${item.id}`} value={value.destinations} placeholder={t('destinationPlaceholder')} onChange={(e) => updateGrant(item.id, { destinations: e.target.value })} /><Label htmlFor={`expire-${item.id}`}>{t('optionalExpiry')}</Label><Input disabled={inputsFrozen} id={`expire-${item.id}`} type="datetime-local" value={value.expires} onChange={(e) => updateGrant(item.id, { expires: e.target.value })} /><p className="muted">{t('inputTimeZone', { timezone: browserTimezone })}</p><p className="muted">{t('destinationPrivacy')}</p></div></div>}</article>; })}
        <Button disabled={inputsFrozen || !selected || Boolean(tokenView)} onClick={saveGrants}>{t('saveGrantReview')}</Button>
      </div>}
      <p className="muted">{t('collectionUnavailable')}</p>
      </div>}
    </section>

    <section className="sub-panel space-y-4" aria-labelledby="mcp-inbound-title"><h3 id="mcp-inbound-title">{t('inboundClients')}</h3><p className="muted">{t('inboundDescription')}</p>
      <div className="grid gap-3 sm:grid-cols-2"><div><Label htmlFor="inbound-name">{t('name')}</Label><Input disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)} id="inbound-name" value={clientName} onChange={(e) => setClientName(e.target.value)} maxLength={120} /></div><div><Label htmlFor="inbound-audience">{t('audience')}</Label><Input id="inbound-audience" value="/api/v1/mcp/" readOnly /><p className="muted">{t('audienceServerNote')}</p></div><div><Label htmlFor="inbound-expiry">{t('clientExpiry')}</Label><Input disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)} id="inbound-expiry" type="datetime-local" value={clientExpiry} onChange={(e) => setClientExpiry(e.target.value)} /><p className="muted">{t('inputTimeZone', { timezone: browserTimezone })}</p></div><div><Label htmlFor="inbound-capabilities">{t('capabilityLabels')}</Label><Input disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)} id="inbound-capabilities" value={capabilities} onChange={(e) => setCapabilities(e.target.value)} placeholder={t('emptyAllowed')} /><p className="muted">{t('capabilityNoInference')}</p></div></div>
      <p className="muted">{t('inboundBounds')}</p>
      <fieldset disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)}><legend>{t('nativeToolBindings')}</legend><div className="grid gap-2 sm:grid-cols-2">{tools.map((tool) => <label key={`${tool.name}@${tool.version}`} className="flex items-start gap-2"><Checkbox checked={chosenTools.includes(`${tool.name}@${tool.version}`)} onCheckedChange={(checked) => setChosenTools((values) => checked ? [...values, `${tool.name}@${tool.version}`] : values.filter((entry) => entry !== `${tool.name}@${tool.version}`))} /><span>{tool.name} · {tool.version}<small className="block muted">{tool.schema_fingerprint} · {tool.permissions.join(', ') || t('noPermissions')}</small></span></label>)}</div></fieldset>
      <fieldset disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView)}><legend>{t('remoteSourceScope')}</legend>{sources.length === 0 && <p className="muted">{t('noRemoteSources')}</p>}{sources.map((source) => <label key={source.id} className="flex items-center gap-2"><Checkbox checked={clientSourceIds.includes(source.id)} onCheckedChange={(checked) => setClientSourceIds((values) => checked ? [...values, source.id] : values.filter((id) => id !== source.id))} />{source.name}</label>)}{sourcesTruncated && <p className="muted">{t('sourceListBound')}</p>}</fieldset>
      <Button disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView) || Boolean(confirm)} onClick={issue}>{t('issueClient')}</Button>
      {clients.length > 0 && <ul className="record-list">{clients.map((client) => <li key={client.id} className="record-row flex flex-wrap justify-between gap-3"><div><strong>{client.name}</strong><p className="muted">{client.token_prefix} · {client.audience} · {t(client.revoked_at ? 'revoked' : 'clientActive')} · {t('revision', { revision: client.revision })}</p><p className="muted">{t('clientScopeSummary', { tools: client.bindings.map((binding) => binding.name).join(', '), sources: client.source_ids.length })}</p></div><div className="flex gap-2"><Button className="secondary" disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView) || Boolean(client.revoked_at)} onClick={() => setConfirm({ action: 'rotate', client })}>{t('rotate')}</Button><Button className="secondary" disabled={busy || loading || reconciliationRequired || conflict || Boolean(tokenView) || Boolean(client.revoked_at)} onClick={() => setConfirm({ action: 'revoke', client })}>{t('revoke')}</Button></div></li>)}</ul>}
      {tokenView && <aside className="rounded-md border border-border p-4" aria-live="polite"><h4>{t('oneTimeToken')}</h4><p className="muted">{t('tokenOnce', { name: tokenView.client.name, prefix: tokenView.client.token_prefix })}</p><Input readOnly type="text" autoComplete="off" spellCheck={false} value={tokenView.token} aria-label={t('oneTimeToken')} /><div className="flex gap-2"><Button className="secondary" disabled={busy} onClick={() => void copyToken()}>{t('copyToken')}</Button>{copied && <span role="status">{t('copied')}</span>}<Button className="secondary" disabled={busy} onClick={() => setConfirm({ action: 'dismiss', client: tokenView.client })}>{t('dismissToken')}</Button></div><label className="flex items-center gap-2"><Checkbox checked={tokenAcknowledged} onCheckedChange={(checked) => { const acknowledged = Boolean(checked); setTokenAcknowledged(acknowledged); tokenState.current = { exists: Boolean(tokenView), acknowledged }; }} />{t('copyAcknowledgement')}</label><p className="muted">{t('clipboardNotAcceptance')}</p></aside>}
    </section>

    <AlertDialog open={Boolean(confirm)} onOpenChange={(open) => { if (!open) setConfirm(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t(confirm?.action === 'rotate' ? 'confirmRotate' : confirm?.action === 'revoke' ? 'confirmRevoke' : confirm?.action === 'save-empty-grants' ? 'confirmEmptyGrant' : confirm?.action === 'discover' ? 'confirmDiscover' : 'confirmDismiss')}</AlertDialogTitle><AlertDialogDescription>{t(confirm?.action === 'rotate' ? 'rotateWarning' : confirm?.action === 'revoke' ? 'revokeWarning' : confirm?.action === 'save-empty-grants' ? 'emptyGrantWarning' : confirm?.action === 'discover' ? 'rediscoverConfirm' : 'dismissWarning')}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>{t('cancel')}</AlertDialogCancel><Button disabled={confirm?.action === 'dismiss' && !tokenAcknowledged} onClick={confirmClientAction}>{t('confirm')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </section>;
}
