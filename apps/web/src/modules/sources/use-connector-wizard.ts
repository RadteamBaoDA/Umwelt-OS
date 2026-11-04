'use client';

import { useEffect, useLayoutEffect, useMemo, type MutableRefObject } from 'react';
import { connectorKeys, sourceKeys, type ConnectorCatalogEntry, type ConnectorConfig } from './api';
import type { EditorContext } from './connector-editor-context';
import {
  ACTIVATION_POLL_MS,
  ACTIVATION_TIMEOUT_MS,
  msUntil,
  providerKey,
  REQUIRED_SCOPE,
  requiresHeaderCredential,
  scopeFilled,
  scopeIssues,
  sourceErrorDetail,
  usesApiKey,
  VERIFY_TOTAL_MS,
  type EditorActions,
  type EditorSnapshot,
  type EditorStep,
} from './connector-editor-model';
import { isGoogleProvider } from './connector-oauth';
import { createSource, saveConfiguration } from './connector-persistence';
import { failureCodeOf } from './connector-editor-context';
import { saveAndEnable } from './use-connector-activation';
import { collectAndWait, waitForActivation } from './use-source-actions';

/** Inputs of the Connect-step gate. */
type ConnectGateInput = {
  provider: string;
  entry: ConnectorCatalogEntry | undefined;
  redditReady: boolean;
  googleReady: boolean;
  credentialConfigured: boolean;
  secret: string;
  telegramSecretAction: 'keep' | 'replace';
  authMethod: string;
  backendChoice: string | null;
};

/** True for Alpha Vantage and for API-key presets running on the native backend: the owner must provide the key. */
function needsKey(provider: string, entry: ConnectorCatalogEntry | undefined, backendChoice: string | null): boolean {
  return provider === 'alpha_vantage' || (usesApiKey(entry) && (backendChoice ?? 'native') === 'native');
}

/**
 * True when the Connect step may advance. Credentialed providers must prove the credential (validation) or provide the key before
 * the wizard moves on; validation needs the scope/URL entered in the later "choose data" step, so it cannot gate Connect.
 */
export function connectGate(input: ConnectGateInput): boolean {
  const { provider, entry, credentialConfigured } = input;
  const hasKey = Boolean(input.secret) || credentialConfigured;
  if (provider === 'reddit') return input.redditReady;
  if (isGoogleProvider(provider)) return input.googleReady && credentialConfigured;
  if (provider === 'telegram') return (input.telegramSecretAction === 'replace' && Boolean(input.secret)) || credentialConfigured;
  if (provider === 'mastodon') return true;
  if (requiresHeaderCredential(entry)) return hasKey;
  if (provider === 'rest' && input.authMethod === 'http_header') return hasKey;
  return needsKey(provider, entry, input.backendChoice) ? hasKey : true;
}

/** True when every required scope field is filled and the either-or scopes (X, Mastodon) have exactly one side set. */
export function scopeGate(provider: string, configuration: ConnectorConfig): boolean {
  const required = (REQUIRED_SCOPE[provider] ?? []).every((key) => scopeFilled(configuration[key]));
  const xOk = provider !== 'x' || scopeFilled(configuration.x_account) !== scopeFilled(configuration.x_query);
  const mastodonOk = provider !== 'mastodon' || scopeFilled(configuration.mastodon_account) !== scopeFilled(configuration.mastodon_hashtag);
  return required && xOk && mastodonOk;
}

/** True when the provider's URL field (feed URL, page URL) is filled; other providers have none. */
function urlGate(provider: string, configuration: ConnectorConfig): boolean {
  if (provider === 'rss') return Boolean(configuration.feed_url?.trim());
  if (provider === 'web' || provider === 'rest') return Boolean(configuration.url?.trim());
  return true;
}

/** One verify run: its abort controller, the currency check that drops stale results, the failure reporter and the overall deadline. */
type VerifyRun = { controller: AbortController; stillCurrent: () => boolean; fail: (code: string) => void; deadline: number };

/** Saves and enables the source (MCP sources are activated by their own editor); resolves its id, or undefined when the run must stop. */
async function enableForVerify(ctx: EditorContext, run: VerifyRun): Promise<string | undefined> {
  if (ctx.draft.provider === 'mcp') return ctx.draft.sourceId;
  const saved = await saveAndEnable(ctx);
  if (!run.stillCurrent()) return undefined;
  if (!saved) {
    run.fail(ctx.draft.saveFailCode.current ?? 'save_failed');
    return undefined;
  }
  return saved.id;
}

/** Polls activation to a terminal state and then runs one real collection, publishing each phase and the final outcome. */
async function activateAndCollect(ctx: EditorContext, run: VerifyRun, id: string) {
  const { draft, queryClient } = ctx;
  draft.setVerify({ phase: 'activating' });
  // Activation is asynchronous (queued/provisioning); poll the owner state until active, a hard failure, or timeout.
  const activation = await waitForActivation(id, run.controller.signal, {
    timeoutMs: Math.min(ACTIVATION_TIMEOUT_MS, msUntil(run.deadline)),
    intervalMs: ACTIVATION_POLL_MS,
  });
  if (!activation || !run.stillCurrent()) return;
  if (!activation.ok) {
    run.fail(activation.code ?? 'activation_failed');
    return;
  }
  draft.setVerify({ phase: 'collecting' });
  const final = await collectAndWait(id, ctx.csrfToken, {
    signal: run.controller.signal,
    timeoutMs: Math.max(msUntil(run.deadline), 5_000),
  });
  if (!final || !run.stillCurrent()) return;
  void queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(id) });
  void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  ctx.onChanged();
  if (final.status === 'succeeded' || final.status === 'no_changes')
    draft.setVerify({ phase: final.status, runId: final.ingestion_run_id });
  else run.fail(final.error_code ?? final.status);
}

/**
 * Saves and enables the source, waits for activation to become active, then runs one real collection.
 * Progress and the terminal outcome are published through `verify`; failures keep the dialog open with an error code.
 * MCP sources are activated by their own editor, so only the activation wait and collection run here.
 */
export async function runVerify(ctx: EditorContext) {
  const { requests, draft } = ctx;
  requests.verifyController.current?.abort();
  const controller = new AbortController();
  requests.verifyController.current = controller;
  /** True while this verification run owns the controller and has not been aborted, so stale async results are dropped. */
  const stillCurrent = () => requests.verifyController.current === controller && !controller.signal.aborted;
  /** Records a failed verification with its error code, but only if this run is still current. */
  const fail = (code: string) => {
    if (stillCurrent()) draft.setVerify({ phase: 'failed', code });
  };
  const run: VerifyRun = { controller, stillCurrent, fail, deadline: Date.now() + VERIFY_TOTAL_MS };
  draft.saveFailCode.current = null;
  draft.setErrorDetail('');
  draft.setIssues([]);
  draft.setVerify({ phase: 'saving' });
  try {
    const id = await enableForVerify(ctx, run);
    if (id === undefined) return;
    if (!id) {
      fail('source_missing');
      return;
    }
    await activateAndCollect(ctx, run, id);
  } catch (cause) {
    if (!stillCurrent()) return;
    draft.setErrorDetail(sourceErrorDetail(cause));
    draft.setIssues(scopeIssues(cause));
    fail(failureCodeOf(cause));
  }
}

/**
 * Saves Google resource selection and reconnects for the new configuration revision; GitHub saves before OAuth.
 * Resolves false to keep the wizard on the current step.
 */
export async function prepareNext(
  ctx: EditorContext,
  connectGoogle: (saved: { id: string; revision: number }) => Promise<void>,
  from: EditorStep,
): Promise<boolean> {
  const { draft } = ctx;
  if (from === 'choose' && isGoogleProvider(draft.provider) && (draft.dirty || draft.revision === 0)) {
    const saved = await saveConfiguration(ctx);
    if (!saved) return false;
    draft.setNotice(ctx.t('googleReconnectForScope'));
    await connectGoogle(saved);
    return false;
  }
  if (from === 'connect' && draft.provider === 'github' && draft.revision === 0) return (await saveConfiguration(ctx)) !== null;
  return true;
}

/** Message key of the localized activation state label. */
const ACTIVATION_LABEL_KEYS: Record<string, string> = {
  queued: 'stateQueued',
  provisioning: 'stateProvisioning',
  saved_not_active: 'stateSavedNotActive',
  reconciliation_required: 'stateReconciliationRequired',
  disabled: 'stateDisabled',
  active: 'statusActive',
};

/** Connections the wizard gate reads (grant readiness) and the Google reconnect used by `prepareNext`. */
type WizardInputs = {
  redditReady: boolean;
  googleReady: boolean;
  connectGoogle: (saved: { id: string; revision: number }) => Promise<void>;
  step?: EditorStep;
  actionsRef?: MutableRefObject<EditorActions | null>;
  onSnapshot?: (snapshot: EditorSnapshot) => void;
  termsBlocked: boolean;
};

/**
 * Wizard gating and stepper of the connector editor: which sections show for the embedded step, whether Connect/Choose may
 * advance, the standalone stepper model, the snapshot published to the host, and the imperative actions the wizard calls.
 */
export function useConnectorWizard(ctx: EditorContext, inputs: WizardInputs) {
  const { t, draft, requests, currentEntry, configuration, activation } = ctx;
  const { step, actionsRef, onSnapshot } = inputs;
  const locked = requests.busyAction !== '' || requests.actionsBusy;
  const activationLabel = t((ACTIVATION_LABEL_KEYS[activation.visibleActivationState] ?? 'stateUnknown') as 'stateUnknown');
  const currentStep = draft.revision > 0 ? 'collect' : draft.validation ? 'choose' : 'connect';
  const steps = [
    {
      id: 'connect',
      title: t('stepConnect'),
      status: draft.validation ? t('stepValidated') : draft.providerCredentialConfigured ? t('stepCredentialSaved') : t('stepNotValidated'),
    },
    { id: 'choose', title: t('stepChooseData'), status: t(providerKey(draft.provider) as 'provider') },
    { id: 'collect', title: t('stepCollect'), status: t('everyMinutes', { minutes: configuration.schedule_interval_minutes }) },
  ] as const;
  const embedded = step !== undefined;
  /** True when the section tagged with one of these wizard steps should be visible (always true outside the wizard). */
  const showStep = (...visible: EditorStep[]) => !embedded || visible.includes(step);
  const connectOk = connectGate({
    provider: draft.provider,
    entry: currentEntry,
    redditReady: inputs.redditReady,
    googleReady: inputs.googleReady,
    credentialConfigured: draft.providerCredentialConfigured,
    secret: draft.secret,
    telegramSecretAction: draft.telegramSecretAction,
    authMethod: draft.authMethod,
    backendChoice: draft.backendChoice,
  });
  const providerOk = Boolean(
    draft.name.trim() &&
    currentEntry?.source_type &&
    currentEntry.code_available &&
    ['available', 'implemented', 'requires_credentials'].includes(currentEntry.availability),
  );
  // An empty required scope would only fail later as a 422 on save, so the wizard holds Choose until it is filled.
  const chooseOk = !inputs.termsBlocked && urlGate(draft.provider, configuration) && scopeGate(draft.provider, configuration);
  const { sourceId, provider, verify } = draft;
  const snapshot = useMemo<EditorSnapshot>(
    () => ({ sourceId, provider, busy: locked, providerOk, connectOk, chooseOk, verify }),
    [sourceId, provider, locked, providerOk, connectOk, chooseOk, verify],
  );
  useEffect(() => {
    onSnapshot?.(snapshot);
  }, [snapshot, onSnapshot]);
  // Re-assigned each render so the wizard always calls closures over the latest draft.
  useLayoutEffect(() => {
    if (actionsRef)
      actionsRef.current = {
        createSource: () => createSource(ctx),
        prepareNext: (from) => prepareNext(ctx, inputs.connectGoogle, from),
        verify: () => runVerify(ctx),
      };
  });
  const verifyBusy = verify.phase === 'saving' || verify.phase === 'activating' || verify.phase === 'collecting';
  return { locked, activationLabel, currentStep, steps, embedded, showStep, providerOk, connectOk, chooseOk, verifyBusy };
}
