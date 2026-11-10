import { ApiError } from '@/core/api';
import { formatDateTime as formatDate } from '@/core/format';
import {
  collectionTooSoonRetryAt,
  connectorConfigurationIsAtLeast,
  connectorKeys,
  createConnectorSource,
  getConnectorActivation,
  getMonotonicConnectorConfiguration,
  removeProviderCredential,
  saveConnectorConfiguration,
  selectMonotonicConnectorConfiguration,
  sourceKeys,
  updateSourceStatus,
  validateDraftConnector,
  type CollectionRequest,
} from './api';
import { clearOutcome, failureCodeOf, publishFailure, type EditorContext } from './connector-editor-context';
import { errorText, genericProviderIds } from './connector-editor-model';
import { collectErrorKey } from './builtin-sources';
import { collectAndWait } from './use-source-actions';
import type { RequestToken } from './use-connector-requests';

/** Confirms navigation away, invalidates in-flight requests, and closes the connector editor. */
export function closeEditor(ctx: EditorContext) {
  if (!ctx.sync.confirmTransition()) return;
  ctx.requests.dropInFlight();
  ctx.onClose();
}

/** Resumes a paused source and refreshes connector and source queries while the request remains current. */
export async function resumeSource(ctx: EditorContext) {
  const { draft, requests, queryClient } = ctx;
  const id = draft.sourceId;
  if (!id) return;
  const token = requests.beginRequest('resume');
  if (!token) return;
  draft.setError('');
  try {
    const resumed = await updateSourceStatus(id, 'active', ctx.csrfToken, token.controller.signal);
    if (!requests.requestIsCurrent(token)) return;
    ctx.activation.setSourceStatusOverride(resumed.status);
    draft.loadedFor.current = '';
    await queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(id) });
    if (!requests.requestIsCurrent(token)) return;
    await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
    if (!requests.requestIsCurrent(token)) return;
    ctx.onChanged();
  } catch (cause) {
    if (requests.requestIsCurrent(token)) draft.setError(errorText(cause));
  } finally {
    requests.endRequest(token);
  }
}

/** Shows when the next manual collection is allowed for a 429 cooldown, else publishes the failure. */
function publishCollectFailure(ctx: EditorContext, cause: unknown) {
  // 429 collection_too_soon is a cooldown, not a failure: show when the next manual collection is allowed.
  const retryAt = collectionTooSoonRetryAt(cause);
  if (retryAt === undefined) {
    ctx.draft.setError(errorText(cause));
    return;
  }
  const { t, display } = ctx;
  ctx.draft.setNotice(
    retryAt ? t('collectTooSoonAt', { time: formatDate(retryAt.toISOString(), display.locale, display.timezone) }) : t('collectTooSoon'),
  );
}

/** Starts one collection run for an active source, reports its progress, and resolves the final collection request (null when aborted, rejected or not started). */
export async function collectNow(ctx: EditorContext): Promise<CollectionRequest | null> {
  const { draft, requests, queryClient, t } = ctx;
  const id = draft.sourceId;
  if (!id) return null;
  const token = requests.beginRequest('collect');
  if (!token) return null;
  draft.setError('');
  draft.setNotice('');
  let outcome: CollectionRequest | null = null;
  try {
    const final = await collectAndWait(id, ctx.csrfToken, {
      signal: token.controller.signal,
      onAccepted: (result) => {
        if (requests.requestIsCurrent(token)) draft.setNotice(t('collectResult', { status: result.status }));
      },
      onUpdate: (update) => {
        if (requests.requestIsCurrent(token)) draft.setNotice(t('collectResult', { status: update.status }));
      },
    });
    if (!final || !requests.requestIsCurrent(token)) return null;
    outcome = final;
    if (final.status === 'failed' || final.status === 'cancelled') {
      draft.setError('actionFailed');
      const friendly = collectErrorKey(final.error_code);
      draft.setNotice(friendly ? t(friendly) : t('collectFailedCode', { code: final.error_code ?? final.status }));
    }
    await queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(id) });
    await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  } catch (cause) {
    if (requests.requestIsCurrent(token)) publishCollectFailure(ctx, cause);
  } finally {
    requests.endRequest(token);
  }
  return outcome;
}

/** Reloads server-owned connector and activation state while preserving newer local drafts and rejecting stale revisions. */
export async function refreshOwnerState(ctx: EditorContext, id: string, token: RequestToken): Promise<boolean> {
  const { draft, requests, queryClient, sync } = ctx;
  if (!id) return false;
  await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
  if (!requests.requestIsCurrent(token)) return false;
  const [configResult, activationResult] = await Promise.allSettled([
    getMonotonicConnectorConfiguration(id, token.controller.signal, () => sync.configurationFences(id)),
    getConnectorActivation(id, token.controller.signal),
  ]);
  if (!requests.requestIsCurrent(token)) return false;
  if (configResult.status !== 'fulfilled' || activationResult.status !== 'fulfilled') {
    draft.setError('reloadFailed');
    return false;
  }
  const latestConfiguration = configResult.value;
  if (latestConfiguration.source_id !== id) return false;
  await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
  if (!requests.requestIsCurrent(token)) return false;
  const acceptedConfiguration = selectMonotonicConnectorConfiguration(latestConfiguration, sync.configurationFences(id));
  if (!acceptedConfiguration) return false;
  const acceptedIsNewer =
    acceptedConfiguration.expected_revision !== draft.revisionRef.current ||
    acceptedConfiguration.source_generation !== draft.sourceGenerationRef.current;
  draft.serverConfigurationRef.current = acceptedConfiguration;
  if (!draft.dirtyRef.current && draft.draftVersion.current === token.draftVersion) {
    sync.applyServerConfiguration(acceptedConfiguration);
  } else if (acceptedIsNewer) {
    draft.setConflict(true);
    draft.setError('conflict');
  }
  ctx.activation.setActivationState(activationResult.value.state);
  ctx.activation.setActivationError(activationResult.value.error_code);
  queryClient.setQueryData(connectorKeys.configuration(id), acceptedConfiguration);
  queryClient.setQueryData(connectorKeys.activation(id), activationResult.value);
  return true;
}

/** Fetches the newest accepted server configuration and activation for a reload; throws `reload_failed` when none is acceptable. */
async function fetchReloadedState(ctx: EditorContext, id: string, token: RequestToken) {
  const { requests, queryClient, sync } = ctx;
  await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
  if (!requests.requestIsCurrent(token)) return null;
  const [configurationResult, activationResult] = await Promise.all([
    getMonotonicConnectorConfiguration(id, token.controller.signal, () => sync.configurationFences(id)),
    getConnectorActivation(id, token.controller.signal),
  ]);
  if (!requests.requestIsCurrent(token)) return null;
  if (configurationResult.source_id !== id) throw new Error('reload_failed');
  await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
  if (!requests.requestIsCurrent(token)) return null;
  const acceptedConfiguration = selectMonotonicConnectorConfiguration(configurationResult, sync.configurationFences(id));
  if (!acceptedConfiguration) throw new Error('reload_failed');
  return { acceptedConfiguration, activationResult };
}

/** Confirms discarding the current draft, then reloads and applies the newest accepted server configuration. */
export async function reloadServerConfiguration(ctx: EditorContext) {
  const { draft, requests, queryClient, t } = ctx;
  const id = draft.sourceId;
  if (!id || !window.confirm(t('reloadDiscardConfirmation'))) return;
  const token = requests.beginRequest('reload');
  if (!token) return;
  draft.setError('');
  try {
    const reloaded = await fetchReloadedState(ctx, id, token);
    if (!reloaded) return;
    const { acceptedConfiguration, activationResult } = reloaded;
    draft.serverConfigurationRef.current = acceptedConfiguration;
    draft.draftVersion.current += 1;
    draft.setSecret('');
    ctx.sync.applyServerConfiguration(acceptedConfiguration);
    draft.setDirty(false);
    draft.setSecret('');
    draft.dirtyRef.current = false;
    draft.setConflict(false);
    draft.setError('');
    queryClient.setQueryData(connectorKeys.configuration(id), acceptedConfiguration);
    queryClient.setQueryData(connectorKeys.activation(id), activationResult);
    ctx.activation.setActivationState(activationResult.state);
    ctx.activation.setActivationError(activationResult.error_code);
  } catch {
    if (requests.requestIsCurrent(token)) {
      draft.setConflict(true);
      draft.setError('reloadFailed');
    }
  } finally {
    requests.endRequest(token);
  }
}

/** Creates a connector source on first save when necessary and returns its source identifier. */
export async function ensureSource(ctx: EditorContext, token: RequestToken): Promise<string> {
  const { draft, currentEntry } = ctx;
  if (draft.sourceId) return draft.sourceId;
  if (!draft.name.trim()) throw new Error('source_name_required');
  const sourceType = currentEntry?.source_type;
  if (!sourceType || sourceType === 'manual') throw new Error('provider_source_type_unavailable');
  const created = await createConnectorSource(
    sourceType,
    draft.name.trim(),
    ctx.csrfToken,
    token.controller.signal,
    genericProviderIds.has(draft.provider) ? undefined : draft.provider,
    draft.aiOptions,
  );
  if (!ctx.requests.requestIsCurrent(token)) throw new DOMException('editor_closed', 'AbortError');
  draft.sourceGenerationRef.current = created.generation;
  draft.setSourceGeneration(created.generation);
  draft.setSourceId(created.id);
  draft.setNotice(ctx.t('sourceCreated'));
  ctx.onChanged();
  return created.id;
}

/** Validation request body: the settings, the bound source generation and, for Telegram, the secret action (the token exists only here). */
function buildValidationDraft(ctx: EditorContext) {
  const { draft, settings, configuration } = ctx;
  const isTelegram = draft.provider === 'telegram';
  const replaceTelegramToken = isTelegram && draft.telegramSecretAction === 'replace';
  // A Telegram replacement exists only in this direct validation body; config state and query caches never hold it.
  return {
    ...settings,
    configuration: { ...configuration },
    expected_source_generation: draft.sourceGenerationRef.current,
    ...(isTelegram ? { secret_action: draft.telegramSecretAction, ...(replaceTelegramToken ? { secret: draft.secret } : {}) } : {}),
  };
}

/** Validates the current connector draft and accepts the result only if its source, revision, and draft version still match. */
export async function validateDraft(ctx: EditorContext) {
  const { draft, requests } = ctx;
  if (!draft.sourceId) return;
  const token = requests.beginRequest('validate');
  if (!token) return;
  const id = draft.sourceId;
  const body = buildValidationDraft(ctx);
  const submittedVersion = draft.draftVersion.current;
  clearOutcome(ctx);
  try {
    const result = await validateDraftConnector(id, body, token.controller.signal);
    const stale =
      !requests.requestIsCurrent(token) ||
      draft.draftVersion.current !== submittedVersion ||
      result.source_generation !== body.expected_source_generation ||
      result.expected_revision !== body.expected_revision;
    if (stale) return;
    draft.setValidation({
      at: result.validated_at,
      generation: result.source_generation,
      revision: result.expected_revision,
      verifiedBotId: result.verified_bot_id,
      scopeVerified: result.scope_verified,
    });
  } catch (cause) {
    if (requests.requestIsCurrent(token)) publishValidationFailure(ctx, cause);
  } finally {
    requests.endRequest(token);
  }
}

/** Publishes a rejected validation: 503 means the outcome is unknown, 409 freezes the draft as conflicting. */
function publishValidationFailure(ctx: EditorContext, cause: unknown) {
  const { draft } = ctx;
  const outcomeUnknown = cause instanceof ApiError && cause.status === 503;
  publishFailure(ctx, cause);
  if (outcomeUnknown) draft.setError('validationOutcomeUnknown');
  if (cause instanceof ApiError && cause.status === 409) draft.setConflict(true);
}

/** Writes the draft, re-reads the owner state and verifies the acknowledged revision; resolves null (after flagging a conflict) when it is not acceptable. */
async function writeAndAcknowledge(ctx: EditorContext, token: RequestToken, body: ReturnType<typeof buildSaveBody>) {
  const { draft, requests, queryClient, sync } = ctx;
  const signal = token.controller.signal;
  const id = await ensureSource(ctx, token);
  if (!requests.requestIsCurrent(token)) return null;
  await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
  if (!requests.requestIsCurrent(token)) return null;
  const result = await saveConnectorConfiguration(id, body, ctx.csrfToken, signal);
  if (!requests.requestIsCurrent(token)) return null;
  await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
  if (!requests.requestIsCurrent(token)) return null;
  // Re-read owner state after the write; the mutation response alone may not be the newest revision.
  const acknowledged = await getMonotonicConnectorConfiguration(id, signal, () => sync.configurationFences(id));
  if (!requests.requestIsCurrent(token)) return null;
  const conflicting = () => {
    draft.setConflict(true);
    draft.setError('conflict');
    return null;
  };
  if (acknowledged.source_id !== id || acknowledged.expected_revision !== result.desired_revision) return conflicting();
  await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
  if (!requests.requestIsCurrent(token)) return null;
  const outdated =
    !connectorConfigurationIsAtLeast(acknowledged, sync.configurationFences(id)) ||
    acknowledged.source_generation < draft.sourceGenerationRef.current;
  return outdated ? conflicting() : { id, result, acknowledged };
}

/** Settings payload of a save: the effective settings with a private copy of the configuration. */
function buildSaveBody(ctx: EditorContext) {
  return { ...ctx.settings, configuration: { ...ctx.configuration } };
}

/** Publishes an acknowledged save into the draft and caches, then notifies the host; resolves the saved id and revision, or null when stale. */
async function publishSaved(
  ctx: EditorContext,
  token: RequestToken,
  saved: NonNullable<Awaited<ReturnType<typeof writeAndAcknowledge>>>,
  submittedVersion: number,
) {
  const { draft, requests, queryClient } = ctx;
  const { id, result, acknowledged } = saved;
  draft.serverConfigurationRef.current = acknowledged;
  draft.sourceGenerationRef.current = acknowledged.source_generation;
  draft.setSourceGeneration(acknowledged.source_generation);
  draft.loadedFor.current = id;
  draft.setRevision(acknowledged.expected_revision);
  draft.revisionRef.current = acknowledged.expected_revision;
  ctx.activation.setActivationState(result.state);
  ctx.activation.setActivationError(result.error_code);
  draft.setProviderCredentialConfigured(acknowledged.provider_credential_configured);
  if (draft.draftVersion.current === submittedVersion) {
    draft.setDirty(false);
    draft.dirtyRef.current = Boolean(draft.secret);
  }
  draft.setValidation(null);
  queryClient.setQueryData(connectorKeys.configuration(id), acknowledged);
  queryClient.setQueryData(connectorKeys.activation(id), result);
  if (!requests.requestIsCurrent(token)) return null;
  await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(id) });
  if (!requests.requestIsCurrent(token)) return null;
  await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  if (!requests.requestIsCurrent(token)) return null;
  ctx.onChanged();
  return { id, revision: acknowledged.expected_revision };
}

/** Records a rejected save: message, detail, issues, the stable failure code, and the conflict flag for 409 and server errors. */
function publishSaveFailure(ctx: EditorContext, cause: unknown) {
  const { draft } = ctx;
  publishFailure(ctx, cause);
  draft.saveFailCode.current = failureCodeOf(cause);
  if (cause instanceof ApiError && cause.status === 409) draft.setConflict(true);
  if (cause instanceof ApiError && cause.status >= 500) {
    draft.setConflict(true);
    draft.setError('reloadFailed');
  }
}

/** Persists the connector draft, verifies the acknowledged server revision, and updates cached owner state. */
export async function saveConfiguration(
  ctx: EditorContext,
  token: RequestToken | null = ctx.requests.beginRequest('save'),
): Promise<{ id: string; revision: number } | null> {
  if (!token) return null;
  const { draft, requests } = ctx;
  const body = buildSaveBody(ctx);
  const submittedVersion = draft.draftVersion.current;
  clearOutcome(ctx);
  try {
    const saved = await writeAndAcknowledge(ctx, token, body);
    return saved ? await publishSaved(ctx, token, saved, submittedVersion) : null;
  } catch (cause) {
    if (requests.requestIsCurrent(token)) publishSaveFailure(ctx, cause);
    return null;
  } finally {
    if (requests.busyRef.current === 'save') requests.endRequest(token);
  }
}

/** Removes a stored provider credential only when there is no unsaved draft or secret input. */
export async function removeCredential(ctx: EditorContext) {
  const { draft, requests } = ctx;
  if (draft.dirtyRef.current || draft.secret) return;
  const token = requests.beginRequest('remove');
  const id = draft.sourceId;
  if (!token || !id) return;
  const submittedVersion = draft.draftVersion.current;
  draft.setError('');
  try {
    const result = await removeProviderCredential(id, draft.revision, ctx.csrfToken, token.controller.signal);
    if (!requests.requestIsCurrent(token)) return;
    draft.setRevision(result.desired_revision);
    draft.revisionRef.current = result.desired_revision;
    draft.setAuthMethod('none');
    draft.setAuthHeaderName('Authorization');
    draft.setProviderCredentialConfigured(false);
    ctx.activation.setActivationState(result.state);
    ctx.activation.setActivationError(result.error_code);
    draft.setConfiguration((current) => ({ ...current }));
    if (draft.draftVersion.current === submittedVersion) {
      draft.setDirty(false);
      draft.dirtyRef.current = false;
    }
    draft.setSecret('');
    await refreshOwnerState(ctx, id, token);
  } catch (cause) {
    if (requests.requestIsCurrent(token)) draft.setError(errorText(cause));
  } finally {
    requests.endRequest(token);
  }
}

/** Creates the connector source from the current provider and name, then advances the editor to that source; resolves the new id or null on failure. */
export async function createSource(ctx: EditorContext): Promise<string | null> {
  const { draft, requests } = ctx;
  const token = requests.beginRequest('create');
  if (!token) return null;
  draft.setError('');
  try {
    return await ensureSource(ctx, token);
  } catch (cause) {
    if (requests.requestIsCurrent(token)) publishFailure(ctx, cause);
    return null;
  } finally {
    requests.endRequest(token);
  }
}
