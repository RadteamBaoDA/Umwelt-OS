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
  PAIRED_CREDENTIAL_PROVIDERS,
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
  apiSecret?: string;
  telegramSecretAction: 'keep' | 'replace';
  authMethod: string;
  backendChoice: string | null;
};

/** Paired provider credentials are both required when entering or replacing the stored pair. */
function podcastIndexCredentialReady({ secret, apiSecret, credentialConfigured }: ConnectGateInput): boolean {
  const hasKey = Boolean(secret);
  return hasKey === Boolean(apiSecret) && (hasKey || credentialConfigured);
}

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
  if (PAIRED_CREDENTIAL_PROVIDERS.has(provider)) return podcastIndexCredentialReady(input);
  if (provider === 'reddit') return input.redditReady;
  if (isGoogleProvider(provider)) return input.googleReady && credentialConfigured;
  if (provider === 'telegram') return (input.telegramSecretAction === 'replace' && Boolean(input.secret)) || credentialConfigured;
  if (provider === 'mastodon') return true;
  if (requiresHeaderCredential(entry)) return hasKey;
  if (provider === 'rest' && input.authMethod === 'http_header') return hasKey;
  return needsKey(provider, entry, input.backendChoice) ? hasKey : true;
}

/** Returns whether a source with alternative identifiers has exactly one configured target. */
function alternativeScopeComplete(provider: string, configuration: ConnectorConfig): boolean {
  if (provider === 'x') return scopeFilled(configuration.x_account) !== scopeFilled(configuration.x_query);
  if (provider === 'mastodon') return scopeFilled(configuration.mastodon_account) !== scopeFilled(configuration.mastodon_hashtag);
  if (provider === 'podcast_rss') return scopeFilled(configuration.feed_url) !== scopeFilled(configuration.itunes_podcast_id);
  if (provider === 'youtube' || provider === 'youtube_api') {
    // The most-popular chart takes no channel, handle or playlist target; region and category are optional.
    if (provider === 'youtube_api' && configuration.youtube_api_mode === 'most_popular') return true;
    return (
      [
        configuration.youtube_channel_id,
        configuration.youtube_channels,
        configuration.youtube_handles,
        configuration.youtube_playlist_ids,
      ].filter(scopeFilled).length === 1 &&
      (provider !== 'youtube_api' ||
        !configuration.youtube_playlist_ids ||
        !configuration.youtube_filter ||
        configuration.youtube_filter === 'all')
    );
  }
  return true;
}

/** Requires a query or the endpoint target selected by a mode-dependent media provider. */
function modeSelectedScopeComplete(provider: string, configuration: ConnectorConfig): boolean {
  if (provider === 'steam_releases' || provider === 'igdb_games') return gameScopeComplete(provider, configuration);
  if (provider === 'openlibrary_books' || provider === 'google_books') return bookScopeComplete(provider, configuration);
  if (provider === 'tvmaze_schedule') return tvmazeScopeComplete(configuration);
  if (provider === 'mal_anime' && configuration.mal_mode === 'season') {
    return scopeFilled(configuration.mal_season_year) && scopeFilled(configuration.mal_season);
  }
  if (provider !== 'omdb_lookup') return true;
  const omdbMode = configuration.omdb_mode ?? 'title';
  return scopeFilled(
    omdbMode === 'imdb_id' ? configuration.omdb_imdb_id : omdbMode === 'search' ? configuration.omdb_search : configuration.omdb_title,
  );
}

/** Mirrors the server's TVMaze rule: followed shows need show IDs, schedules take none, and global channels need the country-less web schedule. */
function tvmazeScopeComplete(configuration: ConnectorConfig): boolean {
  const mode = configuration.tvmaze_mode ?? 'schedule_country';
  const hasIds = Boolean(configuration.tvmaze_show_ids?.length);
  if (mode === 'followed_shows') return hasIds && !configuration.tvmaze_global;
  return !hasIds && (!configuration.tvmaze_global || (mode === 'schedule_web' && !scopeFilled(configuration.media_country)));
}

/** Requires the selected Open Library target or at least one Google Books query. */
function bookScopeComplete(provider: string, configuration: ConnectorConfig): boolean {
  if (provider === 'google_books') return scopeFilled(configuration.google_books_query) || scopeFilled(configuration.google_books_subject);
  const mode = configuration.openlibrary_mode ?? 'subject';
  return mode === 'recent' || scopeFilled(mode === 'search' ? configuration.openlibrary_query : configuration.openlibrary_subject);
}

/** Requires Steam app IDs for news and at least one bounded IGDB release selector. */
function gameScopeComplete(provider: string, configuration: ConnectorConfig): boolean {
  if (provider === 'steam_releases')
    return (configuration.steam_mode ?? 'featured') !== 'news' || Boolean(configuration.steam_appids?.length);
  return Boolean(configuration.igdb_game_types?.length || configuration.igdb_region || configuration.igdb_platforms?.length);
}

/** Requires identifying scopes and the active target for every mode-dependent source. */
export function scopeGate(provider: string, configuration: ConnectorConfig): boolean {
  const required = (REQUIRED_SCOPE[provider] ?? []).every((key) => scopeFilled(configuration[key]));
  return required && alternativeScopeComplete(provider, configuration) && modeSelectedScopeComplete(provider, configuration);
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
    draft.setNotice(ctx.t('google.reconnectForScope'));
    await connectGoogle(saved);
    return false;
  }
  if (from === 'connect' && draft.provider === 'github' && draft.revision === 0) return (await saveConfiguration(ctx)) !== null;
  return true;
}

/** Message key of the localized activation state label. */
const ACTIVATION_LABEL_KEYS = {
  queued: 'state.queued',
  provisioning: 'state.provisioning',
  saved_not_active: 'state.savedNotActive',
  reconciliation_required: 'state.reconciliationRequired',
  disabled: 'state.disabled',
  active: 'status.active',
} as const;

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
  const activationLabel = t(
    ACTIVATION_LABEL_KEYS[activation.visibleActivationState as keyof typeof ACTIVATION_LABEL_KEYS] ?? 'state.unknown',
  );
  const currentStep = draft.revision > 0 ? 'collect' : draft.validation ? 'choose' : 'connect';
  const steps = [
    {
      id: 'connect',
      title: t('step.connect'),
      status: draft.validation
        ? t('step.validated')
        : draft.providerCredentialConfigured
          ? t('step.credentialSaved')
          : t('step.notValidated'),
    },
    { id: 'choose', title: t('step.chooseData'), status: t(providerKey(draft.provider)) },
    { id: 'collect', title: t('step.collect'), status: t('every.minutes', { minutes: configuration.schedule_interval_minutes }) },
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
    apiSecret: draft.apiSecret,
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
