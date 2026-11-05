'use client';

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { ApiError } from '@/core/api';
import {
  activateConnector,
  connectorKeys,
  saveNativeRestCredential,
  saveWorldProviderCredential,
  sourceKeys,
  type ConnectorActivation,
  type Source,
} from './api';
import { getConnectorActivation } from './api';
import { providerCredentialRequired, errorText, usesProviderCredential } from './connector-editor-model';
import { failureCodeOf, publishFailure, type EditorContext } from './connector-editor-context';
import { refreshOwnerState, saveConfiguration } from './connector-persistence';
import type { AuthGenerations, RequestToken } from './use-connector-requests';

/** Activation polling interval: the owner state is re-read every ten seconds while a source exists. */
const ACTIVATION_REFETCH_MS = 10_000;

/**
 * Server-owned activation status of the edited connector: the locally mirrored state/error (updated by save, enable and retry),
 * the polled activation query, and the visible values (the query wins; a failed poll shows `unknown`). Also the paused/active
 * override set when the owner resumes the source.
 */
export function useConnectorActivation(source: Source | null | undefined, sourceId: string, display: AuthGenerations) {
  const [activationState, setActivationState] = useState(source?.status === 'paused' ? 'disabled' : 'saved_not_active');
  const [sourceStatusOverride, setSourceStatusOverride] = useState<Source['status'] | null>(null);
  const [activationError, setActivationError] = useState<string | null>(null);
  const activationQuery = useQuery({
    queryKey: connectorKeys.activation(sourceId),
    queryFn: async ({ signal }) => {
      const generation = display.authGeneration;
      const result = await getConnectorActivation(sourceId, signal);
      if (!display.isCurrentGeneration(generation)) throw new Error('stale_auth_generation');
      return result;
    },
    enabled: Boolean(sourceId),
    refetchInterval: sourceId ? ACTIVATION_REFETCH_MS : false,
  });
  const sourceStatus = sourceStatusOverride ?? source?.status ?? 'active';
  const visibleActivationState = activationQuery.isError ? 'unknown' : (activationQuery.data?.state ?? activationState);
  const visibleActivationError = activationQuery.isError ? null : (activationQuery.data?.error_code ?? activationError);
  return {
    activationState,
    setActivationState,
    activationError,
    setActivationError,
    setSourceStatusOverride,
    activationQuery,
    sourceStatus,
    visibleActivationState,
    visibleActivationError,
  };
}
export type ConnectorActivationState = ReturnType<typeof useConnectorActivation>;

/** Secret the owner typed plus the auth choice at the moment activation started; replacement bytes travel only with the activation request. */
type ActivationDraft = { authMethod: string; secret: string; telegramSecretAction: 'keep' | 'replace' };

/** Mirrors an accepted activation result into the draft: state, error, credential flag, and clears a replaced secret. */
function applyActivationResult(
  ctx: EditorContext,
  result: ConnectorActivation,
  credentialConfigured: boolean,
  replacementAccepted: boolean,
) {
  ctx.activation.setActivationState(result.state);
  ctx.activation.setActivationError(result.error_code);
  ctx.draft.setProviderCredentialConfigured(credentialConfigured);
  if (replacementAccepted) {
    ctx.draft.setSecret('');
    ctx.draft.setTelegramSecretAction('keep');
  }
}

/** Marks the draft clean after activation unless the owner edited it meanwhile; a kept secret keeps the draft dirty. */
function settleDirty(ctx: EditorContext, replacementAccepted: boolean, requestedSecret: string) {
  ctx.draft.setDirty(false);
  ctx.draft.dirtyRef.current = replacementAccepted ? false : Boolean(requestedSecret);
}

/** Revalidates the activation, then (optionally) configuration and source queries, stopping as soon as the request is stale. */
async function invalidateAfterActivation(
  ctx: EditorContext,
  token: RequestToken,
  id: string,
  includeConfiguration: boolean,
): Promise<boolean> {
  const { queryClient, requests } = ctx;
  await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(id) });
  if (!requests.requestIsCurrent(token)) return false;
  if (includeConfiguration) {
    await queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(id) });
    if (!requests.requestIsCurrent(token)) return false;
  }
  await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  return requests.requestIsCurrent(token);
}

/** Whether the activation sends replacement secret bytes: provider-credential sources never do, Telegram follows its keep/replace choice. */
function replacingSecretFor(ctx: EditorContext, activationDraft: ActivationDraft): boolean {
  if (ctx.draft.provider === 'alpha_vantage' || usesProviderCredential(ctx.currentEntry)) return false;
  if (activationDraft.authMethod === 'telegram_bot_token') return activationDraft.telegramSecretAction === 'replace';
  return Boolean(activationDraft.secret);
}

/** Stores the replacement native or world-provider key (before activation); each is skipped when not replacing. */
async function storeReplacementKeys(
  ctx: EditorContext,
  token: RequestToken,
  id: string,
  nextRevision: number,
  keys: { native: boolean; world: boolean },
  secret: string,
) {
  const signal = token.controller.signal;
  if (keys.native) await saveNativeRestCredential(id, nextRevision, secret, ctx.csrfToken, signal);
  if (keys.world) await saveWorldProviderCredential(id, ctx.draft.sourceGenerationRef.current, nextRevision, secret, ctx.csrfToken, signal);
}

/** True when the provider needs a stored key that neither exists nor is being replaced, so activation must not start. */
function providerKeyMissing(ctx: EditorContext, keys: { native: boolean; world: boolean }): boolean {
  const configured = ctx.draft.providerCredentialConfigured;
  const alphaMissing = ctx.draft.provider === 'alpha_vantage' && !keys.world && !configured;
  return alphaMissing || (providerCredentialRequired(ctx.currentEntry) && !keys.native && !configured);
}

/** Records a rejected save-and-enable: local-only sources report their code, others the generic failure; then reloads owner state. */
async function handleEnableFailure(ctx: EditorContext, token: RequestToken, cause: unknown, actionSourceId: string | null): Promise<null> {
  const { requests, draft, queryClient } = ctx;
  if (!requests.requestIsCurrent(token)) return null;
  // Local-only sources are refused activation by design; report that code rather than a generic conflict.
  if (cause instanceof ApiError && cause.code === 'local_only_source') {
    draft.saveFailCode.current = 'local_only_source';
    draft.setError('');
  } else {
    publishFailure(ctx, cause);
    draft.saveFailCode.current = failureCodeOf(cause);
  }
  draft.setNotice(ctx.t('savedInactive'));
  if (actionSourceId) await refreshOwnerState(ctx, actionSourceId, token);
  if (!requests.requestIsCurrent(token)) return null;
  await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  if (!requests.requestIsCurrent(token)) return null;
  ctx.onChanged();
  return null;
}

/** Accepted activation of a save-and-enable run. */
export type EnableOutcome = { id: string; state: string; errorCode: string | null };

/** Saves the pending draft before activation when it has unsaved changes; false means the run must stop (failed save or stale request). */
async function saveBeforeEnable(
  ctx: EditorContext,
  token: RequestToken,
  progress: { actionSourceId: string | null; nextRevision: number },
): Promise<boolean> {
  const { draft, requests } = ctx;
  if (!(draft.dirty || draft.revision === 0)) return true;
  const saved = await saveConfiguration(ctx, token);
  if (saved === null || !requests.requestIsCurrent(token)) return false;
  progress.actionSourceId = saved.id;
  progress.nextRevision = saved.revision;
  return true;
}

/** Whether a credential stays configured after activation: a replaced key, a header or Telegram auth, or one that already existed. */
function credentialStaysConfigured(ctx: EditorContext, authMethod: string, replacedKey: boolean): boolean {
  return replacedKey || ['http_header', 'telegram_bot_token'].includes(authMethod) || ctx.draft.providerCredentialConfigured;
}

/** Activates the saved revision, mirrors the result into the draft and caches, and notifies the host; null when the request went stale. */
async function activateAndPublish(
  ctx: EditorContext,
  token: RequestToken,
  id: string,
  nextRevision: number,
  activationDraft: ActivationDraft,
  keys: { native: boolean; world: boolean },
): Promise<EnableOutcome | null> {
  const { draft, requests, queryClient } = ctx;
  const replacingSecret = replacingSecretFor(ctx, activationDraft);
  const result = await activateConnector(
    id,
    nextRevision,
    replacingSecret ? 'replace' : 'keep',
    replacingSecret ? activationDraft.secret : undefined,
    ctx.csrfToken,
    token.controller.signal,
    draft.backendChoice ?? undefined,
  );
  if (!requests.requestIsCurrent(token)) return null;
  const replacementAccepted = replacingSecret || keys.world || keys.native;
  applyActivationResult(
    ctx,
    result,
    credentialStaysConfigured(ctx, activationDraft.authMethod, keys.world || keys.native),
    replacementAccepted,
  );
  if (draft.draftVersion.current === token.draftVersion) settleDirty(ctx, replacementAccepted, activationDraft.secret);
  queryClient.setQueryData(connectorKeys.activation(id), result);
  if (!(await invalidateAfterActivation(ctx, token, id, true))) return null;
  ctx.onChanged();
  if (result.state !== 'active') draft.setNotice(ctx.t('savedInactive'));
  return { id, state: result.state, errorCode: result.error_code };
}

/** Saves pending settings when needed, stores replacement keys and activates; resolves the outcome or null when nothing was activated. */
async function runSaveAndEnable(
  ctx: EditorContext,
  token: RequestToken,
  activationDraft: ActivationDraft,
  progress: { actionSourceId: string | null; nextRevision: number },
): Promise<EnableOutcome | null> {
  const { draft, requests } = ctx;
  if (!(await saveBeforeEnable(ctx, token, progress))) return null;
  const id = progress.actionSourceId;
  if (!id || !requests.requestIsCurrent(token)) return null;
  const keys = {
    world: draft.provider === 'alpha_vantage' && Boolean(activationDraft.secret),
    native: usesProviderCredential(ctx.currentEntry) && Boolean(activationDraft.secret),
  };
  if (providerKeyMissing(ctx, keys)) {
    draft.setError('providerSecretRequired');
    return null;
  }
  await storeReplacementKeys(ctx, token, id, progress.nextRevision, keys, activationDraft.secret);
  return activateAndPublish(ctx, token, id, progress.nextRevision, activationDraft, keys);
}

/**
 * Saves pending connector settings before activation and refreshes owner state after success or failure.
 * Resolves the accepted activation (state may still be queued) or null when nothing was activated.
 */
export async function saveAndEnable(ctx: EditorContext): Promise<EnableOutcome | null> {
  const { draft, requests } = ctx;
  const token = requests.beginRequest('save_enable');
  if (!token) return null;
  // Save persists scope first; only the following activation request may carry replacement secret bytes.
  const activationDraft = { authMethod: draft.authMethod, secret: draft.secret, telegramSecretAction: draft.telegramSecretAction };
  const progress = { actionSourceId: draft.sourceId || null, nextRevision: draft.revision };
  draft.setError('');
  draft.setNotice('');
  try {
    return await runSaveAndEnable(ctx, token, activationDraft, progress);
  } catch (cause) {
    return await handleEnableFailure(ctx, token, cause, progress.actionSourceId);
  } finally {
    requests.endRequest(token);
  }
}

/** Replacement-secret decision of a retry: Telegram follows keep/replace, provider-credential sources never replace here. */
function retryReplacingSecret(ctx: EditorContext, requestedSecret: string): boolean {
  if (ctx.draft.authMethod === 'telegram_bot_token') return ctx.draft.telegramSecretAction === 'replace';
  return usesProviderCredential(ctx.currentEntry) ? false : Boolean(requestedSecret);
}

/** Activates the saved revision with the typed secret, mirrors the result, refreshes owner state and notifies the host while current. */
async function activateAndRefresh(
  ctx: EditorContext,
  token: RequestToken,
  id: string,
  requestedSecret: string,
  replacingNativeKey: boolean,
) {
  const { draft, requests } = ctx;
  const replacingSecret = retryReplacingSecret(ctx, requestedSecret);
  const result = await activateConnector(
    id,
    draft.revision,
    replacingSecret ? 'replace' : 'keep',
    replacingSecret ? requestedSecret : undefined,
    ctx.csrfToken,
    token.controller.signal,
    draft.backendChoice ?? undefined,
  );
  if (!requests.requestIsCurrent(token)) return;
  const replacementAccepted = replacingSecret || replacingNativeKey;
  applyActivationResult(ctx, result, credentialStaysConfigured(ctx, draft.authMethod, replacingNativeKey), replacementAccepted);
  if (!draft.dirtyRef.current || draft.draftVersion.current === token.draftVersion) settleDirty(ctx, replacementAccepted, requestedSecret);
  await refreshOwnerState(ctx, id, token);
  if (!requests.requestIsCurrent(token)) return;
  if (await invalidateAfterActivation(ctx, token, id, false)) ctx.onChanged();
}

/** Re-activates the saved revision (storing a replaced native key first); stops quietly when a required key is missing. */
async function runRetryActivation(ctx: EditorContext, token: RequestToken, id: string) {
  const { draft } = ctx;
  const requestedSecret = draft.secret;
  const replacingNativeKey = usesProviderCredential(ctx.currentEntry) && Boolean(requestedSecret);
  if (replacingNativeKey) await saveNativeRestCredential(id, draft.revision, requestedSecret, ctx.csrfToken, token.controller.signal);
  if (providerCredentialRequired(ctx.currentEntry) && !replacingNativeKey && !draft.providerCredentialConfigured) {
    draft.setError('providerSecretRequired');
    return;
  }
  await activateAndRefresh(ctx, token, id, requestedSecret, replacingNativeKey);
}

/** Retries connector activation and refreshes server-owned status without applying stale request results. */
export async function retryActivation(ctx: EditorContext) {
  const { requests, draft } = ctx;
  const token = requests.beginRequest('activate');
  const id = draft.sourceId;
  if (!token || !id) return;
  draft.setError('');
  draft.setNotice('');
  try {
    await runRetryActivation(ctx, token, id);
  } catch (cause) {
    if (!requests.requestIsCurrent(token)) return;
    draft.setError(errorText(cause));
    draft.setNotice(ctx.t('savedInactive'));
    await refreshOwnerState(ctx, id, token);
  } finally {
    requests.endRequest(token);
  }
}
