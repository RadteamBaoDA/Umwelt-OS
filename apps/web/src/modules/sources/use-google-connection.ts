'use client';

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useResetOnChange } from '../shared/use-reset-on-change';
import { discoverGoogleResources, disconnectGoogleOAuth, getGoogleOAuthStatus, startGoogleOAuth, type GoogleResourceChoice } from './api';
import type { EditorContext } from './connector-editor-context';
import { isGoogleProvider, revokeOAuthConnection, startOAuthConnection, type GoogleProvider } from './connector-oauth';
import { changeConfiguration, type ConnectorDraft } from './use-connector-draft';
import type { RequestToken } from './use-connector-requests';

/** Status poll interval of an OAuth grant. */
const STATUS_REFETCH_MS = 10_000;

/** Per Google provider: the configuration field holding the selection and the server-side selection maximum. */
const SELECTION_LIMITS = {
  google_mail: { field: 'google_mail_label_ids', maximum: 10 },
  google_calendar: { field: 'google_calendar_ids', maximum: 25 },
  google_drive: { field: 'google_drive_ids', maximum: 100 },
} as const;

/**
 * Discovered Google resources of the edited source (labels, calendars or Drive folders), the Drive navigation path, truncation
 * and discovery error. The whole set is reset whenever the edited source or provider changes, because it is keyed to them.
 */
function useGoogleResourceState(sourceId: string, provider: string) {
  const [googleResources, setGoogleResources] = useState<GoogleResourceChoice[]>([]);
  const [googleResourceParent, setGoogleResourceParent] = useState<string | undefined>();
  const [googleResourcePath, setGoogleResourcePath] = useState<GoogleResourceChoice[]>([]);
  const [googleResourcesTruncated, setGoogleResourcesTruncated] = useState(false);
  const [googleDiscoveryError, setGoogleDiscoveryError] = useState<'failed' | 'limit' | null>(null);
  // Reset discovered Google resources whenever the edited source or provider changes (state is keyed to them).
  useResetOnChange([sourceId, provider], () => {
    setGoogleResources([]);
    setGoogleResourceParent(undefined);
    setGoogleResourcePath([]);
    setGoogleResourcesTruncated(false);
  });
  return {
    googleResources,
    setGoogleResources,
    googleResourceParent,
    setGoogleResourceParent,
    googleResourcePath,
    setGoogleResourcePath,
    googleResourcesTruncated,
    setGoogleResourcesTruncated,
    googleDiscoveryError,
    setGoogleDiscoveryError,
  };
}

/** Writes the selection into the provider-specific configuration field (one typed call per field keeps the key/value types exact). */
function setSelection(draft: ConnectorDraft, provider: GoogleProvider, values: string[]) {
  if (provider === 'google_mail') changeConfiguration(draft, 'google_mail_label_ids', values);
  else if (provider === 'google_calendar') changeConfiguration(draft, 'google_calendar_ids', values);
  else changeConfiguration(draft, 'google_drive_ids', values);
}

/** True when a discovery result no longer matches the live request, source, generation, provider and revision, or its grant went backwards. */
function discoveryIsStale(
  ctx: EditorContext,
  token: RequestToken,
  fence: { generation: number; revision: number; tokenRevision: number },
  provider: string,
  resultTokenRevision: number,
): boolean {
  const { draft } = ctx;
  return (
    !ctx.requests.requestIsCurrent(token) ||
    draft.sourceIdRef.current !== draft.sourceId ||
    draft.sourceGenerationRef.current !== fence.generation ||
    draft.providerRef.current !== provider ||
    draft.revisionRef.current !== fence.revision ||
    resultTokenRevision < fence.tokenRevision
  );
}

/**
 * Google read-only OAuth connection of the edited source: grant status, connect/disconnect, resource discovery and the
 * bounded selection. Discovery results are published only while source, generation, provider and revision still match.
 */
export function useGoogleConnection(ctx: EditorContext) {
  const { provider, sourceId } = ctx.draft;
  const state = useGoogleResourceState(sourceId, provider);
  const active = Boolean(sourceId) && isGoogleProvider(provider);
  const googleStatusQuery = useQuery({
    queryKey: ['google-oauth-status', sourceId],
    queryFn: ({ signal }) => getGoogleOAuthStatus(sourceId, signal),
    enabled: active,
    refetchInterval: active ? STATUS_REFETCH_MS : false,
  });

  /** Saves the selected Google scope, then starts the source-revision-bound read-only OAuth flow. */
  async function connectGoogle(saved?: { id: string; revision: number }) {
    if (!isGoogleProvider(provider)) return;
    await startOAuthConnection(ctx, {
      action: 'google-connect',
      failureKey: 'googleConnectionFailed',
      saved,
      start: (fence, token) =>
        startGoogleOAuth(fence.id, provider, fence.generation, fence.revision, ctx.csrfToken, token.controller.signal),
    });
  }

  /** Loads owner-visible Google resource choices under the current source, configuration and OAuth grant revisions. */
  async function loadGoogleResources(parentId?: string, path: GoogleResourceChoice[] = []) {
    const { draft, requests } = ctx;
    const status = googleStatusQuery.data;
    if (!sourceId || !status || status.state !== 'ready' || !isGoogleProvider(provider)) return;
    const token = requests.beginRequest('google-discovery');
    if (!token) return;
    const fence = {
      generation: draft.sourceGenerationRef.current,
      revision: draft.revisionRef.current,
      tokenRevision: status.token_revision,
    };
    state.setGoogleDiscoveryError(null);
    try {
      const result = await discoverGoogleResources(
        sourceId,
        provider,
        fence.generation,
        fence.revision,
        fence.tokenRevision,
        parentId,
        token.controller.signal,
      );
      if (discoveryIsStale(ctx, token, fence, provider, result.token_revision)) return;
      state.setGoogleResources(result.items);
      state.setGoogleResourceParent(parentId);
      state.setGoogleResourcePath(path);
      state.setGoogleResourcesTruncated(result.truncated);
      if (result.token_revision !== fence.tokenRevision) await googleStatusQuery.refetch();
    } catch {
      if (requests.requestIsCurrent(token)) state.setGoogleDiscoveryError('failed');
    } finally {
      requests.endRequest(token);
    }
  }

  /** Adds or removes a discovered provider resource while enforcing the backend's saved selection bound. */
  function toggleGoogleResource(resourceId: string, checked: boolean) {
    if (!isGoogleProvider(provider)) return;
    const { field, maximum } = SELECTION_LIMITS[provider];
    const selected = new Set(ctx.configuration[field] ?? []);
    if (checked) selected.add(resourceId);
    else selected.delete(resourceId);
    const values = [...selected];
    if (values.length > maximum) {
      state.setGoogleDiscoveryError('limit');
      return;
    }
    state.setGoogleDiscoveryError(null);
    setSelection(ctx.draft, provider, values);
  }

  /** Revokes only the current Google grant revision and refreshes its secret-free status. */
  async function disconnectGoogle() {
    await revokeOAuthConnection(ctx, {
      action: 'google-disconnect',
      failureKey: 'googleDisconnectFailed',
      status: googleStatusQuery.data,
      refetch: googleStatusQuery.refetch,
      revoke: (fence, tokenRevision, token) =>
        disconnectGoogleOAuth(sourceId, fence.generation, fence.revision, tokenRevision, ctx.csrfToken, token.controller.signal),
    });
  }

  return {
    googleResources: state.googleResources,
    googleResourceParent: state.googleResourceParent,
    googleResourcePath: state.googleResourcePath,
    googleResourcesTruncated: state.googleResourcesTruncated,
    googleDiscoveryError: state.googleDiscoveryError,
    googleStatusQuery,
    connectGoogle,
    loadGoogleResources,
    toggleGoogleResource,
    disconnectGoogle,
  };
}
