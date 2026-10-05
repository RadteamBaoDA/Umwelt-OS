'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { ProviderScope, type NativeProvider } from './provider-scope';
import { GitHubSummary } from './github-summary';
import { McpCollectionEditor } from './mcp-collection-editor';
import {
  activateConnector,
  ConnectorCatalogEntry,
  ConnectorConfig,
  ConnectorConfiguration,
  ConnectorSettings,
  connectorConfigurationIsAtLeast,
  connectorKeys,
  createConnectorSource,
  getConnectorActivation,
  getConnectorCatalog,
  getMonotonicConnectorConfiguration,
  selectMonotonicConnectorConfiguration,
  removeProviderCredential,
  saveConnectorConfiguration,
  Source,
  sourceKeys,
  updateSourceStatus,
  validateDraftConnector,
  disconnectGitHubOAuth,
  acknowledgeGitHubReconnect,
  getGitHubPeers,
  getGitHubStatus,
  resetGitHubSync,
  refreshGitHubOAuth,
  startGitHubOAuth,
  type GitHubPeer,
} from './api';

const intervals = [15, 30, 60, 360, 1440] as const;
const sourceTypes = { rss: 'rss', web: 'web', rest: 'api', mcp: 'mcp', youtube: 'rss', arxiv: 'rss', huggingface: 'api', github: 'api', github_releases: 'api', telegram: 'api' } as const;
type Provider = keyof typeof sourceTypes;
const nativeProviders = new Set<Provider>(['youtube', 'arxiv', 'huggingface', 'github', 'github_releases', 'telegram']);

/** Checks whether a provider identifier belongs to the supported provider set. */
function isProvider(value: string): value is Provider {
  return Object.hasOwn(sourceTypes, value);
}

/** Maps a provider identifier to its localized catalog key. */
function providerKey(providerId: string): string {
  return ({
    rss: 'providerRss', web: 'providerWeb', rest: 'providerRest', mcp: 'providerMcp', github: 'providerGithub',
    google_mail: 'providerGoogleMail', google_calendar: 'providerGoogleCalendar', google_drive: 'providerGoogleDrive',
    youtube: 'providerYoutube', arxiv: 'providerArxiv', huggingface: 'providerHuggingface', github_releases: 'providerGithubReleases',
    telegram: 'providerTelegram', google_news: 'providerGoogleNews', reddit: 'providerReddit', hacker_news: 'providerHackerNews',
    mastodon: 'providerMastodon', bluesky: 'providerBluesky', x: 'providerX', vietnamese_press: 'providerVietnamesePress',
    gdelt_government: 'providerGdelt', finance: 'providerFinance', weather_disaster_climate: 'providerWeather', cyber_cve: 'providerCyber',
    map_osint: 'providerMapOsint', browser: 'providerBrowser', notes: 'providerNotes', health: 'providerHealth',
    personal_finance: 'providerPersonalFinance', iot: 'providerIot', notion: 'providerNotion', slack: 'providerSlack',
    home_assistant: 'providerHomeAssistant',
  } as Record<string, string>)[providerId] ?? 'provider';
}

/** Builds the editable default configuration for the selected provider. */
function defaultConfiguration(provider: Provider): ConnectorConfig {
  if (nativeProviders.has(provider)) return {
    timeout_seconds: 30,
    timezone: 'Asia/Ho_Chi_Minh',
    schedule_interval_minutes: provider === 'youtube' || provider === 'arxiv' ? 15 : 30,
    history_mode: provider === 'telegram' ? 'pending_updates' : 'returned_snapshot',
    ...(provider === 'github' ? { include_issues: true, include_pulls: true, include_commits: true, include_releases: true, github_history_days: 90 } : {}),
  };
  return {
    js_render: false,
    max_pages: 10,
    max_depth: 2,
    timeout_seconds: 60,
    timezone: 'Asia/Ho_Chi_Minh',
    schedule_interval_minutes: provider === 'rss' ? 15 : 30,
    ...(provider === 'rest' ? { items_path: 'items', id_field: 'id', content_field: 'content' } : {}),
  };
}

/** Formats an ISO timestamp with the supplied application locale and time zone. */
function formatDate(value: string, locale: AppLocaleId, timezone: string): string {
  return new Intl.DateTimeFormat(normalizeFormattingLocale(locale), {
    dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
  }).format(new Date(value));
}

/** Converts a request or validation failure into the editor’s supported error message key. */
function errorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) return 'conflict';
  if (error instanceof ApiError && error.status === 422) return 'configurationInvalid';
  if (error instanceof ApiError && error.status === 503) return 'serviceUnavailable';
  if (error instanceof Error && error.message === 'source_name_required') return 'sourceNameRequired';
  return 'actionFailed';
}

/** Edit and activate a connector using owner APIs; show uncertain GitHub visibility, actual source status, and retained owner history separately. */
export function ConnectorEditor({
  source,
  onClose,
  onChanged,
  registerTransitionGuard,
}: {
  source?: Source | null;
  onClose: () => void;
  onChanged: () => void;
  registerTransitionGuard: (guard: (() => boolean) | null) => void;
}) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const display = useDisplayPreferences();
  const { registerLeaveGuard } = useGuardedNavigation();
  const queryClient = useQueryClient();
  const initialProvider = source?.provider && isProvider(source.provider) ? source.provider : source?.type === 'mcp' ? 'mcp' : source?.type === 'rss' ? 'rss' : source?.type === 'web' ? 'web' : 'rest';
  const [provider, setProvider] = useState<Provider>(initialProvider);
  const [name, setName] = useState(source?.name ?? '');
  const [sourceId, setSourceId] = useState(source?.id ?? '');
  const providerRef = useRef(provider);
  providerRef.current = provider;
  const nameRef = useRef(name);
  nameRef.current = name;
  const sourceIdRef = useRef(sourceId);
  sourceIdRef.current = sourceId;
  const sourceGenerationRef = useRef(source?.generation ?? 0);
  const editorGenerationId = useId();
  const editorGenerationRef = useRef(editorGenerationId);
  const requestGeneration = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const busyRef = useRef('');
  const draftVersion = useRef(0);
  const [configuration, setConfiguration] = useState<ConnectorConfig>(() => defaultConfiguration(initialProvider));
  const [scopeResetEpoch, setScopeResetEpoch] = useState(0);
  const [authMethod, setAuthMethod] = useState<'none' | 'http_header' | 'telegram_bot_token'>(source?.provider === 'telegram' ? 'telegram_bot_token' : 'none');
  const [authHeaderName, setAuthHeaderName] = useState('Authorization');
  const [secret, setSecret] = useState('');
  const [telegramSecretAction, setTelegramSecretAction] = useState<'keep' | 'replace'>('keep');
  const [revision, setRevision] = useState(0);
  const revisionRef = useRef(revision);
  revisionRef.current = revision;
  const [activationState, setActivationState] = useState(source?.status === 'paused' ? 'disabled' : 'saved_not_active');
  const [sourceStatusOverride, setSourceStatusOverride] = useState<Source['status'] | null>(null);
  const [activationError, setActivationError] = useState<string | null>(null);
  const [providerCredentialConfigured, setProviderCredentialConfigured] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<{ at: string; generation: number; revision: number; verifiedBotId?: string | null; scopeVerified?: boolean | null } | null>(null);
  const [busyAction, setBusyAction] = useState('');
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState('');
  const [githubPeers, setGithubPeers] = useState<GitHubPeer[]>([]);
  const [githubDisconnectOpen, setGithubDisconnectOpen] = useState(false);
  const [githubRecoveryReview, setGithubRecoveryReview] = useState<{ operationId: string; kind: 'authorization' | 'refresh'; originSourceId: string; originGeneration: number; originRevision: number; sourceId: string; generation: number; revision: number } | null>(null);
  const [githubRecoveryOpen, setGithubRecoveryOpen] = useState(false);
  const githubReviewRef = useRef<{ sourceId: string; sourceGeneration: number; revision: number; peers: GitHubPeer[] } | null>(null);
  const loadedFor = useRef('');
  const serverConfigurationRef = useRef<ConnectorConfiguration | null>(null);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty || Boolean(secret);
  /** Asks before discarding the current unsaved draft. */
  const confirmDiscard = useCallback(() => !dirtyRef.current || window.confirm(t('draftLeave')), [t]);
  /** Invalidates the current draft session and accepts leaving the editor. */
  const acceptLeave = useCallback(() => {
    requestGeneration.current += 1;
    requestController.current?.abort();
    requestController.current = null;
    setScopeResetEpoch((current) => current + 1);
    busyRef.current = '';
    setBusyAction('');
    dirtyRef.current = false;
    setDirty(false);
    setSecret('');
    const baseline = serverConfigurationRef.current;
    const resetProvider = baseline
      ? baseline.provider && isProvider(baseline.provider) ? baseline.provider : baseline.source_type === 'rss' ? 'rss' : baseline.source_type === 'web' ? 'web' : 'rest'
      : source?.type === 'rss' ? 'rss' : source?.type === 'web' ? 'web' : source ? 'rest' : sourceIdRef.current ? providerRef.current : 'rss';
    setProvider(resetProvider);
    setName(source?.name ?? (sourceIdRef.current ? nameRef.current : ''));
    setConfiguration(baseline?.configuration ?? defaultConfiguration(resetProvider));
    setAuthMethod(baseline?.auth_method ?? 'none');
    setTelegramSecretAction('keep');
    setAuthHeaderName(baseline?.auth_header_name ?? 'Authorization');
    setRevision(baseline?.expected_revision ?? 0);
    revisionRef.current = baseline?.expected_revision ?? 0;
    if (baseline) {
      sourceGenerationRef.current = baseline.source_generation;
      setActivationState(baseline.activation_state);
      setActivationError(baseline.activation_error_code);
      setProviderCredentialConfigured(baseline.provider_credential_configured);
      loadedFor.current = baseline.source_id;
    } else {
      setProviderCredentialConfigured(false);
      loadedFor.current = '';
    }
    setValidation(null);
    setConflict(false);
    setError('');
  }, [source]);
  /** Runs the registered navigation guard before replacing the selected source. */
  const confirmTransition = useCallback(() => {
    if (!confirmDiscard()) return false;
    acceptLeave();
    return true;
  }, [acceptLeave, confirmDiscard]);
  useEffect(() => {
    registerTransitionGuard(confirmTransition);
    return () => registerTransitionGuard(null);
  }, [confirmTransition, registerTransitionGuard]);
  useEffect(() => registerLeaveGuard({
    hasUnsavedChanges: () => dirtyRef.current,
    confirmDiscard,
    acceptLeave,
  }), [acceptLeave, confirmDiscard, registerLeaveGuard]);
  /** Collects the local source and revision bounds used to reject older server configuration. */
  function configurationFences(id: string) {
    return [serverConfigurationRef.current ?? undefined, queryClient.getQueryData<ConnectorConfiguration>(connectorKeys.configuration(id))] as const;
  }
  const configurationQuery = useQuery({
    queryKey: connectorKeys.configuration(sourceId),
    queryFn: ({ signal }) => getMonotonicConnectorConfiguration(sourceId, signal, () => configurationFences(sourceId)),
    enabled: Boolean(sourceId),
  });
  const activationQuery = useQuery({
    queryKey: connectorKeys.activation(sourceId),
    queryFn: async ({ signal }) => {
      const generation = display.authGeneration;
      const result = await getConnectorActivation(sourceId, signal);
      if (!display.isCurrentGeneration(generation)) throw new Error('stale_auth_generation');
      return result;
    },
    enabled: Boolean(sourceId),
    refetchInterval: sourceId ? 10_000 : false,
  });
  const githubStatusQuery = useQuery({
    queryKey: ['github-oauth-status', sourceId],
    queryFn: ({ signal }) => getGitHubStatus(sourceId, signal),
    enabled: Boolean(sourceId) && provider === 'github',
    refetchInterval: sourceId && provider === 'github' ? 10_000 : false,
  });
  const catalogQuery = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal) });
  const catalog = catalogQuery.data ?? [];
  const currentEntry = catalog.find((entry) => entry.provider_id === provider);
  const sourceStatus = sourceStatusOverride ?? source?.status ?? 'active';
  const visibleActivationState = activationQuery.isError ? 'unknown' : activationQuery.data?.state ?? activationState;
  const visibleActivationError = activationQuery.isError ? null : activationQuery.data?.error_code ?? activationError;
  const historyDescription = provider === 'youtube' ? t('youtubeHistory') : provider === 'arxiv' ? t('arxivHistory')
    : provider === 'huggingface' ? t('huggingfaceHistory') : provider === 'github' ? t('githubAppHistory') : provider === 'github_releases' ? t('githubReleasesHistory')
      : provider === 'telegram' ? t('telegramHistory') : currentEntry?.history_description;

  /** Applies an accepted server configuration to the draft and its associated revision state. */
  const applyServerConfiguration = useCallback((value: ConnectorConfiguration) => {
    setScopeResetEpoch((current) => current + 1);
    serverConfigurationRef.current = value;
    sourceGenerationRef.current = value.source_generation;
    setProvider(value.provider && isProvider(value.provider) ? value.provider : value.source_type === 'mcp' ? 'mcp' : value.source_type === 'rss' ? 'rss' : value.source_type === 'web' ? 'web' : 'rest');
    setConfiguration(value.configuration);
    setAuthMethod(value.auth_method);
    setAuthHeaderName(value.auth_header_name ?? 'Authorization');
    setRevision(value.expected_revision);
    revisionRef.current = value.expected_revision;
    setActivationState(value.activation_state);
    setActivationError(value.activation_error_code);
    setProviderCredentialConfigured(value.provider_credential_configured);
    setTelegramSecretAction('keep');
    setDirty(false);
    dirtyRef.current = Boolean(secret);
    setValidation(null);
    setConflict(false);
    setError('');
    loadedFor.current = value.source_id;
  }, [secret]);

  useEffect(() => {
    const incoming = configurationQuery.data;
    const baseline = serverConfigurationRef.current;
    if (incoming && !connectorConfigurationIsAtLeast(incoming, [baseline ?? undefined])) return;
    if (incoming) serverConfigurationRef.current = incoming;
    if (incoming && loadedFor.current !== incoming.source_id && !dirtyRef.current) {
      applyServerConfiguration(incoming);
    } else if (incoming && loadedFor.current === incoming.source_id) {
      const changedBaseline = incoming.expected_revision !== revision
        || incoming.source_generation !== sourceGenerationRef.current;
      if (changedBaseline && dirtyRef.current) {
        setConflict(true);
        setError('conflict');
      } else if (changedBaseline) {
        applyServerConfiguration(incoming);
      }
    }
  }, [configurationQuery.data, applyServerConfiguration, revision]);

  useEffect(() => {
    /** Discards the connector draft and resets source identity and configuration when authentication ends. */
    const endAuthSession = () => {
      acceptLeave();
      setSourceId('');
      sourceGenerationRef.current = 0;
      setConfiguration(defaultConfiguration('rss'));
      setName('');
      loadedFor.current = '';
    };
    window.addEventListener('bbd:auth-ending', endAuthSession);
    return () => window.removeEventListener('bbd:auth-ending', endAuthSession);
  }, [acceptLeave]);

  useEffect(() => () => {
    requestGeneration.current += 1;
    requestController.current?.abort();
  }, []);

  const settings = useMemo<ConnectorSettings>(() => ({
    expected_revision: revision,
    configuration,
    auth_method: provider === 'telegram' ? 'telegram_bot_token' : authMethod,
    ...(authMethod === 'http_header' ? { auth_header_name: authHeaderName } : {}),
  }), [revision, configuration, authMethod, authHeaderName, provider]);

  /** Updates one typed configuration field and marks the editor draft changed. */
  function changeConfiguration<K extends keyof ConnectorConfig>(key: K, value: ConnectorConfig[K]) {
    setConfiguration((current) => ({ ...current, [key]: value }));
    markDraftChanged();
    setValidation(null);
    setNotice('');
  }

  /** Advances the draft version and marks the current configuration dirty. */
  function markDraftChanged() {
    draftVersion.current += 1;
    dirtyRef.current = true;
    setDirty(true);
  }

  type RequestToken = { generation: number; editorGeneration: string; sourceId: string | null; authGeneration: number; controller: AbortController; draftVersion: number };
  /** Starts a serialized editor request and captures the generations needed to reject stale completion. */
  function beginRequest(action: string): RequestToken | null {
    if (busyRef.current) return null;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    busyRef.current = action;
    setBusyAction(action);
    return { generation: requestGeneration.current, editorGeneration: editorGenerationRef.current, sourceId: sourceId || null, authGeneration: display.authGeneration, controller, draftVersion: draftVersion.current };
  }
  /** Checks that a request still belongs to the active editor, source, and authenticated session. */
  function requestIsCurrent(token: RequestToken): boolean {
    // A response from an old editor, source, auth session, or aborted request must not publish state.
    return token.generation === requestGeneration.current
      && token.editorGeneration === editorGenerationRef.current
      && display.isCurrentGeneration(token.authGeneration)
      && !token.controller.signal.aborted;
  }
  /** Clears the busy state only when the completing request still owns the active editor generation. */
  function endRequest(token: RequestToken) {
    if (!requestIsCurrent(token)) return;
    busyRef.current = '';
    setBusyAction('');
  }
  /** Confirms navigation away, invalidates in-flight requests, and closes the connector editor. */
  function closeEditor() {
    if (!confirmTransition()) return;
    requestGeneration.current += 1;
    requestController.current?.abort();
    busyRef.current = '';
    setBusyAction('');
    onClose();
  }

  /** Resumes a paused source and refreshes connector and source queries while the request remains current. */
  async function resumeSource() {
    const id = sourceId;
    if (!id) return;
    const token = beginRequest('resume');
    if (!token) return;
    setError('');
    try {
      const resumed = await updateSourceStatus(id, 'active', csrfToken, token.controller.signal);
      if (!requestIsCurrent(token)) return;
      setSourceStatusOverride(resumed.status);
      loadedFor.current = '';
      await queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return;
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (!requestIsCurrent(token)) return;
      onChanged();
    } catch (cause) {
      if (requestIsCurrent(token)) setError(errorText(cause));
    } finally {
      endRequest(token);
    }
  }

  /** Reloads server-owned connector and activation state while preserving newer local drafts and rejecting stale revisions. */
  async function refreshOwnerState(id: string, token: RequestToken): Promise<boolean> {
    if (!id) return false;
    await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
    if (!requestIsCurrent(token)) return false;
    const [configResult, activationResult] = await Promise.allSettled([
      getMonotonicConnectorConfiguration(id, token.controller.signal, () => configurationFences(id)), getConnectorActivation(id, token.controller.signal),
    ]);
    if (!requestIsCurrent(token)) return false;
      if (configResult.status !== 'fulfilled' || activationResult.status !== 'fulfilled') {
      setError('reloadFailed');
      return false;
    }
    const latestConfiguration = configResult.value;
    if (latestConfiguration.source_id !== id) return false;
    await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
    if (!requestIsCurrent(token)) return false;
    const fences = configurationFences(id);
    const acceptedConfiguration = selectMonotonicConnectorConfiguration(latestConfiguration, fences);
    if (!acceptedConfiguration) return false;
    const acceptedIsNewer = acceptedConfiguration.expected_revision !== revisionRef.current
      || acceptedConfiguration.source_generation !== sourceGenerationRef.current;
    serverConfigurationRef.current = acceptedConfiguration;
    if (!dirtyRef.current && draftVersion.current === token.draftVersion) {
      applyServerConfiguration(acceptedConfiguration);
    } else if (acceptedIsNewer) {
      setConflict(true);
      setError('conflict');
    }
    setActivationState(activationResult.value.state);
    setActivationError(activationResult.value.error_code);
    queryClient.setQueryData(connectorKeys.configuration(id), acceptedConfiguration);
    queryClient.setQueryData(connectorKeys.activation(id), activationResult.value);
    return true;
  }

  /** Confirms discarding the current draft, then reloads and applies the newest accepted server configuration. */
  async function reloadServerConfiguration() {
    const id = sourceId;
    if (!id || !window.confirm(t('reloadDiscardConfirmation'))) return;
    const token = beginRequest('reload');
    if (!token) return;
    setError('');
    try {
      await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return;
      const [configurationResult, activationResult] = await Promise.all([
        getMonotonicConnectorConfiguration(id, token.controller.signal, () => configurationFences(id)),
        getConnectorActivation(id, token.controller.signal),
      ]);
      if (!requestIsCurrent(token)) return;
      if (configurationResult.source_id !== id) throw new Error('reload_failed');
      await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return;
      const fences = configurationFences(id);
      const acceptedConfiguration = selectMonotonicConnectorConfiguration(configurationResult, fences);
      if (!acceptedConfiguration) throw new Error('reload_failed');
      serverConfigurationRef.current = acceptedConfiguration;
      draftVersion.current += 1;
      setSecret('');
      applyServerConfiguration(acceptedConfiguration);
      setDirty(false);
      setSecret('');
      dirtyRef.current = false;
      setConflict(false);
      setError('');
      queryClient.setQueryData(connectorKeys.configuration(id), acceptedConfiguration);
      queryClient.setQueryData(connectorKeys.activation(id), activationResult);
      setActivationState(activationResult.state);
      setActivationError(activationResult.error_code);
    } catch {
      if (requestIsCurrent(token)) {
        setConflict(true);
        setError('reloadFailed');
      }
    } finally {
      endRequest(token);
    }
  }

  /** Creates a connector source on first save when necessary and returns its source identifier. */
  async function ensureSource(token: RequestToken): Promise<string> {
    if (sourceId) return sourceId;
    if (!name.trim()) throw new Error('source_name_required');
    const created = await createConnectorSource(sourceTypes[provider], name.trim(), csrfToken, token.controller.signal, nativeProviders.has(provider) ? provider : undefined);
    if (!requestIsCurrent(token)) throw new DOMException('editor_closed', 'AbortError');
    sourceGenerationRef.current = created.generation;
    setSourceId(created.id);
    setNotice(t('sourceCreated'));
    onChanged();
    return created.id;
  }

  /** Validates the current connector draft and accepts the result only if its source, revision, and draft version still match. */
  async function validateDraft() {
    if (!sourceId) return;
    const token = beginRequest('validate');
    if (!token) return;
    const id = sourceId;
    const replaceTelegramToken = provider === 'telegram' && telegramSecretAction === 'replace';
    // A Telegram replacement exists only in this direct validation body; config state and query caches never hold it.
    const draft = { ...settings, configuration: { ...configuration }, expected_source_generation: sourceGenerationRef.current,
      ...(provider === 'telegram' ? { secret_action: telegramSecretAction, ...(replaceTelegramToken ? { secret } : {}) } : {}) };
    const submittedVersion = draftVersion.current;
    setError('');
    setNotice('');
    try {
      const result = await validateDraftConnector(id, draft, token.controller.signal);
      if (!requestIsCurrent(token) || draftVersion.current !== submittedVersion || result.source_generation !== draft.expected_source_generation || result.expected_revision !== draft.expected_revision) return;
      setValidation({ at: result.validated_at, generation: result.source_generation, revision: result.expected_revision, verifiedBotId: result.verified_bot_id, scopeVerified: result.scope_verified });
    } catch (cause) {
      if (requestIsCurrent(token)) {
        setError(cause instanceof ApiError && cause.status === 503 ? 'validationOutcomeUnknown' : errorText(cause));
        if (cause instanceof ApiError && cause.status === 409) setConflict(true);
      }
    } finally {
      endRequest(token);
    }
  }

  /** Persists the connector draft, verifies the acknowledged server revision, and updates cached owner state. */
  async function saveConfiguration(token = beginRequest('save')): Promise<{ id: string; revision: number } | null> {
    if (!token) return null;
    const draft = { ...settings, configuration: { ...configuration } };
    const submittedVersion = draftVersion.current;
    setError('');
    setNotice('');
    try {
      const id = await ensureSource(token);
      if (!requestIsCurrent(token)) return null;
      await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return null;
      const result = await saveConnectorConfiguration(id, draft, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token)) return null;
      await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return null;
      // Re-read owner state after the write; the mutation response alone may not be the newest revision.
      const acknowledged = await getMonotonicConnectorConfiguration(id, token.controller.signal, () =>
        configurationFences(id));
      if (!requestIsCurrent(token)) return null;
      if (acknowledged.source_id !== id || acknowledged.expected_revision !== result.desired_revision) {
        setConflict(true);
        setError('conflict');
        return null;
      }
      await queryClient.cancelQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return null;
      const fences = configurationFences(id);
      if (!connectorConfigurationIsAtLeast(acknowledged, fences)
        || acknowledged.source_generation < sourceGenerationRef.current) {
        setConflict(true);
        setError('conflict');
        return null;
      }
      serverConfigurationRef.current = acknowledged;
      sourceGenerationRef.current = acknowledged.source_generation;
      loadedFor.current = id;
      setRevision(acknowledged.expected_revision);
      revisionRef.current = acknowledged.expected_revision;
      setActivationState(result.state);
      setActivationError(result.error_code);
      if (draftVersion.current === submittedVersion) {
        setDirty(false);
        dirtyRef.current = Boolean(secret);
      }
      setValidation(null);
      queryClient.setQueryData(connectorKeys.configuration(id), acknowledged);
      queryClient.setQueryData(connectorKeys.activation(id), result);
      if (!requestIsCurrent(token)) return null;
      await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(id) });
      if (!requestIsCurrent(token)) return null;
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (!requestIsCurrent(token)) return null;
      onChanged();
      return { id, revision: acknowledged.expected_revision };
    } catch (cause) {
      if (requestIsCurrent(token)) {
        setError(errorText(cause));
        if (cause instanceof ApiError && cause.status === 409) setConflict(true);
        if (cause instanceof ApiError && cause.status >= 500) { setConflict(true); setError('reloadFailed'); }
      }
      return null;
    } finally {
      if (busyRef.current === 'save') endRequest(token);
    }
  }

  /** Saves pending connector settings before activation and refreshes owner state after success or failure. */
  async function saveAndEnable() {
    const token = beginRequest('save_enable');
    if (!token) return;
    let actionSourceId: string | null = sourceId || null;
    // Save persists scope first; only the following activation request may carry replacement secret bytes.
    const activationDraft = { authMethod, secret, telegramSecretAction };
    let nextRevision = revision;
    setError('');
    setNotice('');
    try {
      if (dirty || revision === 0) {
        const saved = await saveConfiguration(token);
        if (saved === null || !requestIsCurrent(token)) return;
        actionSourceId = saved.id;
        nextRevision = saved.revision;
      }
      const id = actionSourceId;
      if (!id || !requestIsCurrent(token)) return;
      const replacingSecret = activationDraft.authMethod === 'telegram_bot_token' ? activationDraft.telegramSecretAction === 'replace' : Boolean(activationDraft.secret);
      const result = await activateConnector(id, nextRevision, replacingSecret ? 'replace' : 'keep', replacingSecret ? activationDraft.secret : undefined, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token)) return;
      setActivationState(result.state);
      setActivationError(result.error_code);
      setProviderCredentialConfigured(activationDraft.authMethod === 'http_header' || activationDraft.authMethod === 'telegram_bot_token' || providerCredentialConfigured);
      const replacementAccepted = replacingSecret;
      if (replacementAccepted) { setSecret(''); setTelegramSecretAction('keep'); }
      if (draftVersion.current === token.draftVersion) {
        setDirty(false);
        dirtyRef.current = replacementAccepted ? false : Boolean(activationDraft.secret);
      }
      queryClient.setQueryData(connectorKeys.activation(id), result);
      await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(id) });
      if (!requestIsCurrent(token)) return;
      await queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return;
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (!requestIsCurrent(token)) return;
      onChanged();
      if (result.state !== 'active') setNotice(t('savedInactive'));
    } catch (cause) {
      if (!requestIsCurrent(token)) return;
      setError(errorText(cause));
      setNotice(t('savedInactive'));
      if (actionSourceId) await refreshOwnerState(actionSourceId, token);
      if (!requestIsCurrent(token)) return;
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (!requestIsCurrent(token)) return;
      onChanged();
    } finally {
      endRequest(token);
    }
  }

  /** Retries connector activation and refreshes server-owned status without applying stale request results. */
  async function retryActivation() {
    const token = beginRequest('activate');
    const id = sourceId;
    if (!token || !id) return;
    const requestedSecret = secret;
    setError('');
    setNotice('');
    try {
      const replacingSecret = authMethod === 'telegram_bot_token' ? telegramSecretAction === 'replace' : Boolean(requestedSecret);
      const result = await activateConnector(id, revision, replacingSecret ? 'replace' : 'keep', replacingSecret ? requestedSecret : undefined, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token)) return;
      setActivationState(result.state);
      setActivationError(result.error_code);
      setProviderCredentialConfigured(authMethod === 'http_header' || authMethod === 'telegram_bot_token' || providerCredentialConfigured);
      const replacementAccepted = replacingSecret;
      if (replacementAccepted) { setSecret(''); setTelegramSecretAction('keep'); }
      if (!dirtyRef.current || draftVersion.current === token.draftVersion) {
        setDirty(false);
        dirtyRef.current = replacementAccepted ? false : Boolean(requestedSecret);
      }
      await refreshOwnerState(id, token);
      if (!requestIsCurrent(token)) return;
      await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(id) });
      if (!requestIsCurrent(token)) return;
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (!requestIsCurrent(token)) return;
      onChanged();
    } catch (cause) {
      if (!requestIsCurrent(token)) return;
      setError(errorText(cause));
      setNotice(t('savedInactive'));
      await refreshOwnerState(id, token);
    } finally {
      endRequest(token);
    }
  }

  /** Removes a stored provider credential only when there is no unsaved draft or secret input. */
  async function removeCredential() {
    if (dirtyRef.current || secret) return;
    const token = beginRequest('remove');
    const id = sourceId;
    if (!token || !id) return;
    const submittedVersion = draftVersion.current;
    setError('');
    try {
      const result = await removeProviderCredential(id, revision, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token)) return;
      setRevision(result.desired_revision);
      revisionRef.current = result.desired_revision;
      setAuthMethod('none');
      setAuthHeaderName('Authorization');
      setProviderCredentialConfigured(false);
      setActivationState(result.state);
      setActivationError(result.error_code);
      setConfiguration((current) => ({ ...current }));
      if (draftVersion.current === submittedVersion) { setDirty(false); dirtyRef.current = false; }
      setSecret('');
      await refreshOwnerState(id, token);
    } catch (cause) {
      if (requestIsCurrent(token)) setError(errorText(cause));
    } finally {
      endRequest(token);
    }
  }

  /** Checks that an async GitHub action still owns the exact saved editor source, generation, and configuration revision. */
  function githubRequestIsCurrent(token: RequestToken, id: string, generation: number, expectedRevision: number): boolean {
    return requestIsCurrent(token) && token.sourceId === id && sourceIdRef.current === id
      && sourceGenerationRef.current === generation && revisionRef.current === expectedRevision
      && !dirtyRef.current;
  }

  /** Starts OAuth only after the owner has separately resolved any pending operation, then follows its browser-bound redirect. */
  async function connectGitHub() {
    const id = sourceId;
    if (!id || provider !== 'github' || dirtyRef.current) return;
    const token = beginRequest('github-connect');
    if (!token) return;
    const generation = sourceGenerationRef.current;
    const expectedRevision = revisionRef.current;
    setError('');
    try {
      const result = await startGitHubOAuth(id, generation, expectedRevision, csrfToken, token.controller.signal);
      if (!githubRequestIsCurrent(token, id, generation, expectedRevision)) return;
      window.location.assign(result.authorization_url);
    } catch {
      if (requestIsCurrent(token)) setError('githubConnectionFailed');
    } finally {
      endRequest(token);
    }
  }

  /** Restarts GitHub history only against the current saved configuration and reviewed scope digest. */
  async function resetGitHubHistory() {
    const id = sourceId;
    const status = githubStatusQuery.data;
    if (!id || provider !== 'github' || dirtyRef.current || !status?.sync.scope_sha256) return;
    const token = beginRequest('github-reset');
    if (!token) return;
    const generation = sourceGenerationRef.current;
    const expectedRevision = revisionRef.current;
    try {
      await resetGitHubSync(id, generation, expectedRevision, status.sync.scope_sha256, csrfToken, token.controller.signal);
      if (!githubRequestIsCurrent(token, id, generation, expectedRevision)) return;
      await githubStatusQuery.refetch();
    } catch {
      if (requestIsCurrent(token)) setError('githubConnectionFailed');
    } finally {
      endRequest(token);
    }
  }

  /** Captures the owner-wide operation origin and selected clean source for a separate explicit recovery confirmation. */
  function reviewGitHubRecovery() {
    const status = githubStatusQuery.data;
    if (!status?.recovery_available || !status.operation_id || !status.operation_source_id
      || !['authorization', 'refresh'].includes(status.operation_kind ?? '')
      || !sourceId || sourceStatus !== 'active' || dirtyRef.current || revisionRef.current < 1) return;
    setGithubRecoveryReview({
      operationId: status.operation_id, kind: status.operation_kind as 'authorization' | 'refresh',
      originSourceId: status.operation_source_id, originGeneration: status.operation_source_generation ?? 0,
      originRevision: status.operation_configuration_revision ?? 0,
      sourceId, generation: sourceGenerationRef.current, revision: revisionRef.current,
    });
    setGithubRecoveryOpen(true);
  }

  /** Acknowledges only the captured unresolved operation, then starts consent for the still-current selected source. */
  async function confirmGitHubRecovery() {
    const reviewed = githubRecoveryReview;
    if (!reviewed || !githubStatusQuery.data || githubStatusQuery.data.operation_id !== reviewed.operationId
      || githubStatusQuery.data.operation_source_id !== reviewed.originSourceId
      || githubStatusQuery.data.operation_source_generation !== reviewed.originGeneration
      || githubStatusQuery.data.operation_configuration_revision !== reviewed.originRevision
      || sourceIdRef.current !== reviewed.sourceId || sourceGenerationRef.current !== reviewed.generation
      || revisionRef.current !== reviewed.revision || dirtyRef.current || sourceStatus !== 'active') {
      setGithubRecoveryOpen(false);
      setGithubRecoveryReview(null);
      setError('conflict');
      return;
    }
    const token = beginRequest('github-recovery');
    if (!token) return;
    setError('');
    try {
      await acknowledgeGitHubReconnect(reviewed.sourceId, reviewed.operationId, csrfToken, token.controller.signal);
      if (!githubRequestIsCurrent(token, reviewed.sourceId, reviewed.generation, reviewed.revision)) return;
      await githubStatusQuery.refetch();
      const result = await startGitHubOAuth(reviewed.sourceId, reviewed.generation, reviewed.revision, csrfToken, token.controller.signal);
      if (!githubRequestIsCurrent(token, reviewed.sourceId, reviewed.generation, reviewed.revision)) return;
      setGithubRecoveryOpen(false);
      setGithubRecoveryReview(null);
      window.location.assign(result.authorization_url);
    } catch {
      if (requestIsCurrent(token)) setError('githubConnectionFailed');
      void githubStatusQuery.refetch();
    } finally {
      endRequest(token);
    }
  }

  /** Rotates the stored GitHub grant and reloads its secret-free status. */
  async function refreshGitHub() {
    const id = sourceId;
    if (!id) return;
    const token = beginRequest('github-refresh');
    if (!token) return;
    const generation = sourceGenerationRef.current;
    const expectedRevision = revisionRef.current;
    setError('');
    try {
      await refreshGitHubOAuth(id, csrfToken, token.controller.signal);
      if (!githubRequestIsCurrent(token, id, generation, expectedRevision)) return;
      await githubStatusQuery.refetch();
    } catch {
      if (requestIsCurrent(token)) setError('githubConnectionFailed');
      void githubStatusQuery.refetch();
    } finally {
      endRequest(token);
    }
  }

  /** Loads a bounded peer inventory before showing the app-wide revoke confirmation. */
  async function reviewGitHubDisconnect() {
    const id = sourceId;
    if (!id) return;
    const token = beginRequest('github-peers');
    if (!token) return;
    const generation = sourceGenerationRef.current;
    const expectedRevision = revisionRef.current;
    setError('');
    try {
      const inventory = await getGitHubPeers(id, token.controller.signal);
      if (!githubRequestIsCurrent(token, id, generation, expectedRevision)) return;
      if (!inventory.complete || inventory.peers.length === 0) throw new Error('github_peer_inventory_incomplete');
      setGithubPeers(inventory.peers);
      githubReviewRef.current = { sourceId: id, sourceGeneration: generation, revision: expectedRevision, peers: inventory.peers };
      setGithubDisconnectOpen(true);
    } catch {
      if (requestIsCurrent(token)) setError('githubInventoryFailed');
    } finally {
      endRequest(token);
    }
  }

  /** Submits the exact peer revisions the owner reviewed and refreshes local status after revocation. */
  async function confirmGitHubDisconnect() {
    const reviewed = githubReviewRef.current;
    if (!reviewed || reviewed.peers.length === 0) return;
    const token = beginRequest('github-disconnect');
    if (!token) return;
    const id = reviewed.sourceId;
    const currentStatus = githubStatusQuery.data;
    const operationId = currentStatus?.recovery_available
      && ['reconciliation_required', 'revoking'].includes(currentStatus.coordinator_state)
      && ['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(currentStatus.coordinator_error_code ?? '') ? currentStatus.operation_id ?? undefined : undefined;
    if (sourceIdRef.current !== reviewed.sourceId || sourceGenerationRef.current !== reviewed.sourceGeneration
      || revisionRef.current !== reviewed.revision || dirtyRef.current) {
      githubReviewRef.current = null;
      setGithubPeers([]);
      setGithubDisconnectOpen(false);
      setError('conflict');
      endRequest(token);
      return;
    }
    setError('');
    try {
      await disconnectGitHubOAuth(id, reviewed.peers, csrfToken, operationId, token.controller.signal);
      if (!githubRequestIsCurrent(token, id, reviewed.sourceGeneration, reviewed.revision)) return;
      setGithubDisconnectOpen(false);
      githubReviewRef.current = null;
      await githubStatusQuery.refetch();
      await onChanged();
    } catch (cause) {
      if (requestIsCurrent(token)) {
        if (cause instanceof ApiError && cause.status === 409) {
          githubReviewRef.current = null;
          setGithubPeers([]);
          setGithubDisconnectOpen(false);
          setError('conflict');
        } else setError('githubConnectionFailed');
      }
      void githubStatusQuery.refetch();
    } finally {
      endRequest(token);
    }
  }

  /** Creates the connector source from the current provider and name, then advances the editor to that source. */
  async function createSource() {
    const token = beginRequest('create');
    if (!token) return;
    setError('');
    try {
      await ensureSource(token);
    } catch (cause) {
      if (requestIsCurrent(token)) setError(errorText(cause));
    } finally {
      endRequest(token);
    }
  }

  /** Formats provider availability and unsupported-operation information for the connector catalog. */
  const catalogStatus = (entry: ConnectorCatalogEntry) => entry.availability === 'available' || entry.availability === 'implemented'
    ? t('available') : entry.availability === 'requires_credentials' ? t('credentialsRequired') : entry.availability === 'planned' ? t('planned') : t('unavailable');

  return <section className="sub-panel source-editor" aria-label={sourceId ? t('editTitle') : t('setupTitle')}>
    {!sourceId ? <>
      <header className="section-heading"><div><h2>{t('setupTitle')}</h2><p className="muted">{t('stepConnect')}</p></div></header>
      {catalogQuery.isPending && <p className="muted">{t('loading')}</p>}
      {catalogQuery.isError && <p className="error" role="alert">{t('loadFailed')} <Button className="secondary" onClick={() => catalogQuery.refetch()}>{t('retry')}</Button></p>}
      {catalog.length > 0 && <div className="source-provider-grid" role="group" aria-label={t('provider')}>
        {catalog.map((entry) => {
          const localProvider = entry.provider_id;
          const supported = ['available', 'implemented', 'requires_credentials'].includes(entry.availability) && isProvider(localProvider);
          return <Button key={entry.provider_id} type="button" className={`source-provider${provider === localProvider ? ' is-selected' : ''}`} disabled={!supported || busyAction !== ''} onClick={() => { if (!supported || !isProvider(localProvider)) return; setProvider(localProvider); setConfiguration(defaultConfiguration(localProvider)); markDraftChanged(); }}>
            <strong>{t(providerKey(entry.provider_id))}</strong><span>{catalogStatus(entry)}</span>
            {!supported && <small>{t('providerUnavailableReason')}</small>}
          </Button>;
        })}
      </div>}
      <div className="form source-editor-form">
        <div className="field"><Label htmlFor="new-source-name">{t('sourceName')}</Label><Input id="new-source-name" maxLength={200} disabled={busyAction !== ''} value={name} onChange={(event) => { setName(event.target.value); markDraftChanged(); }} /></div>
        <div className="form-actions"><Button disabled={busyAction !== '' || !name.trim() || !currentEntry || !['available', 'implemented', 'requires_credentials'].includes(currentEntry.availability)} onClick={createSource}>{busyAction === 'create' ? t('saving') : t('createAndContinue')}</Button><Button className="secondary" onClick={closeEditor}>{t('cancel')}</Button></div>
      </div>
    </> : <>
      <header className="section-heading"><div><h2>{name || source?.name || t('editTitle')}</h2><p className="muted">{t('stepChooseData')} · {t('stepCollect')}</p></div><Button className="secondary" onClick={closeEditor}>{t('close')}</Button></header>
      {configurationQuery.isPending && <p className="muted">{t('loading')}</p>}
      {configurationQuery.isError && <div className="error" role="alert">{t(errorText(configurationQuery.error) as 'conflict' | 'configurationInvalid' | 'serviceUnavailable' | 'sourceNameRequired' | 'actionFailed')} <Button className="secondary" onClick={() => configurationQuery.refetch()}>{t('retry')}</Button></div>}
      {configurationQuery.isSuccess && <>
        {provider === 'mcp' ? <McpCollectionEditor sourceId={sourceId} sourceStatus={sourceStatus} onDraftChange={setDirty} onChanged={() => { void queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(sourceId) }); void queryClient.invalidateQueries({ queryKey: connectorKeys.activation(sourceId) }); onChanged(); }} /> : <>
        <fieldset className="source-editor-fields" disabled={busyAction !== ''}>
        <div className="form source-editor-form">
          {nativeProviders.has(provider)
            ? <ProviderScope key={`${sourceId}:${provider}:${revision}:${sourceGenerationRef.current}:${scopeResetEpoch}`} provider={provider as NativeProvider} configuration={configuration} disabled={busyAction !== ''} resetEpoch={scopeResetEpoch} onChange={changeConfiguration} />
            : provider === 'rss' ? <div className="field"><Label htmlFor="source-feed-url">{t('sourceUrl')}</Label><Input id="source-feed-url" type="url" value={configuration.feed_url ?? ''} onChange={(event) => changeConfiguration('feed_url', event.target.value)} /></div> : <div className="field"><Label htmlFor="source-config-url">{provider === 'rest' ? t('apiUrl') : t('pageUrl')}</Label><Input id="source-config-url" type="url" value={configuration.url ?? ''} onChange={(event) => changeConfiguration('url', event.target.value)} /></div>}
          {provider === 'rest' && <>
            <div className="field"><Label htmlFor="source-items-path">{t('itemsPath')}</Label><Input id="source-items-path" maxLength={256} value={configuration.items_path ?? ''} onChange={(event) => changeConfiguration('items_path', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-id-field">{t('idField')}</Label><Input id="source-id-field" maxLength={128} value={configuration.id_field ?? ''} onChange={(event) => changeConfiguration('id_field', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-title-field">{t('titleField')}</Label><Input id="source-title-field" maxLength={128} value={configuration.title_field ?? ''} onChange={(event) => changeConfiguration('title_field', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-content-field">{t('contentField')}</Label><Input id="source-content-field" maxLength={128} value={configuration.content_field ?? ''} onChange={(event) => changeConfiguration('content_field', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-updated-field">{t('updatedField')}</Label><Input id="source-updated-field" maxLength={128} value={configuration.updated_field ?? ''} onChange={(event) => changeConfiguration('updated_field', event.target.value)} /></div>
          </>}
          {provider === 'web' && <>
            <div className="field"><Label htmlFor="source-max-pages">{t('maxPages')}</Label><Input id="source-max-pages" type="number" min={1} max={10} value={configuration.max_pages} onChange={(event) => changeConfiguration('max_pages', Number(event.target.value))} /></div>
            <div className="field"><Label htmlFor="source-max-depth">{t('maxDepth')}</Label><Input id="source-max-depth" type="number" min={0} max={2} value={configuration.max_depth} onChange={(event) => changeConfiguration('max_depth', Number(event.target.value))} /></div>
            <div className="field"><Label htmlFor="source-timeout">{t('timeout')}</Label><Input id="source-timeout" type="number" min={1} max={60} value={configuration.timeout_seconds} onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))} /></div>
            <div className="field field-inline"><Label htmlFor="source-js-render">{t('renderJavascript')}</Label><Checkbox id="source-js-render" checked={configuration.js_render} onCheckedChange={(checked) => changeConfiguration('js_render', checked === true)} /></div>
          </>}
          {nativeProviders.has(provider) && <>
            <div className="field"><Label htmlFor="source-native-timeout">{t('timeout')}</Label><Input id="source-native-timeout" type="number" min={1} max={30} value={configuration.timeout_seconds} onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))} /></div>
            <div className="field"><Label htmlFor="source-history-mode">{t('historyMode')}</Label><Input id="source-history-mode" readOnly aria-readonly="true" value={t(provider === 'telegram' ? 'pendingUpdatesOnly' : 'returnedSnapshot')} /></div>
            {provider === 'telegram' && <>
              <div className="field"><Label htmlFor="source-telegram-secret-action">{t('telegramCredentialAction')}</Label><Select value={telegramSecretAction} onValueChange={(value) => { setTelegramSecretAction(value as 'keep' | 'replace'); setSecret(''); dirtyRef.current = dirty; draftVersion.current += 1; setValidation(null); setNotice(''); }}><SelectTrigger id="source-telegram-secret-action"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="keep">{t('keepCredential')}</SelectItem><SelectItem value="replace">{t('replaceCredential')}</SelectItem></SelectContent></Select></div>
              {telegramSecretAction === 'replace' && <div className="field"><Label htmlFor="source-telegram-token">{t('telegramBotToken')}</Label><Input id="source-telegram-token" type="password" autoComplete="new-password" maxLength={512} required value={secret} onChange={(event) => { setSecret(event.target.value); dirtyRef.current = true; setDirty(true); draftVersion.current += 1; setValidation(null); setNotice(''); }} /><small className="muted">{t('secretHelp')}</small></div>}
            </>}
          </>}
          <div className="field"><Label htmlFor="source-timezone">{t('timezone')}</Label><Input id="source-timezone" value={configuration.timezone} onChange={(event) => changeConfiguration('timezone', event.target.value)} /></div>
          <div className="field"><Label htmlFor="source-schedule">{t('schedule')}</Label><Select value={String(configuration.schedule_interval_minutes)} onValueChange={(value) => changeConfiguration('schedule_interval_minutes', Number(value) as ConnectorConfig['schedule_interval_minutes'])}><SelectTrigger id="source-schedule"><SelectValue /></SelectTrigger><SelectContent>{intervals.map((minutes) => <SelectItem key={minutes} value={String(minutes)}>{t('everyMinutes', { minutes })}</SelectItem>)}</SelectContent></Select></div>
          {provider === 'rest' && <>
            <div className="field"><Label htmlFor="source-auth-method">{t('auth')}</Label><Select value={authMethod} onValueChange={(value) => { setAuthMethod(value as typeof authMethod); markDraftChanged(); setValidation(null); }}><SelectTrigger id="source-auth-method"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">{t('noAuthentication')}</SelectItem><SelectItem value="http_header">{t('headerAuthentication')}</SelectItem></SelectContent></Select></div>
            {authMethod === 'http_header' && <>
              <div className="field"><Label htmlFor="source-header-name">{t('headerName')}</Label><Input id="source-header-name" maxLength={128} value={authHeaderName} onChange={(event) => { setAuthHeaderName(event.target.value); markDraftChanged(); setValidation(null); }} /></div>
              <div className="field"><Label htmlFor="source-provider-secret">{t('providerSecret')}</Label><Input id="source-provider-secret" type="password" autoComplete="new-password" value={secret} onChange={(event) => { setSecret(event.target.value); dirtyRef.current = dirty || Boolean(event.target.value); draftVersion.current += 1; setValidation(null); }} /><small className="muted">{t('secretHelp')}</small></div>
            </>}
          </>}
        </div>
        </fieldset>
        {historyDescription && <div className="source-capability"><strong>{t('history')}</strong><p>{historyDescription}</p>{nativeProviders.has(provider) && <p>{t('providerHistoryCaveat')}</p>}</div>}
        {provider === 'github' && <section className="source-capability" aria-live="polite">
          <strong>{githubStatusQuery.data?.state === 'ready' ? t('githubConnected') : t('githubNotConnected')}</strong>
          {githubStatusQuery.data?.expires_at && <p>{t('githubExpires')}: {formatDate(githubStatusQuery.data.expires_at, display.locale, display.timezone)}</p>}
          {githubStatusQuery.data && <p>{t('githubScanWindow', { days: githubStatusQuery.data.sync.history_days })}</p>}
          {(githubStatusQuery.data?.sync.unverified_hints ?? 0) > 0 && <p className="error">{t('githubEvidenceUnverified', { count: githubStatusQuery.data?.sync.unverified_hints ?? 0 })}</p>}
          {source?.provider === 'github' && source.status === 'paused' && <p className="error">{t('githubSourcePaused')}</p>}
          {(githubStatusQuery.data?.sync.reconcile_exhausted ?? 0) > 0 && <p className="error">{t('githubReconcileExhausted', { count: githubStatusQuery.data?.sync.reconcile_exhausted ?? 0 })}</p>}
          <p className="muted">{t('githubDeletionUnsupported')}</p>
          {githubStatusQuery.data?.sync.last_reset_at && <p className="muted">{t('githubGapRecorded')}: {formatDate(githubStatusQuery.data.sync.last_reset_at, display.locale, display.timezone)}</p>}
          {githubStatusQuery.data?.sync.resources.map((item) => <p key={item.resource} className={item.incomplete ? 'error' : 'muted'}>
            {t('githubResourceProgress', { resource: item.resource, phase: item.phase, page: item.page, sweep: item.sweep_revision })}
            {item.floor && item.upper ? ` · ${formatDate(item.floor, display.locale, display.timezone)} – ${formatDate(item.upper, display.locale, display.timezone)}` : ''}
            {item.completed_upper ? ` · ${formatDate(item.completed_upper, display.locale, display.timezone)}` : ''}
          </p>)}
          {githubStatusQuery.data?.sync.cursor_invalid && <p className="error">{t('githubCursorInvalid')}</p>}
          {(githubStatusQuery.data?.state === 'reconciliation_required' || githubStatusQuery.data?.coordinator_state === 'reconciliation_required' || githubStatusQuery.data?.recovery_available) && <p className="error">{t('githubReconciliation')}</p>}
          {githubStatusQuery.data?.recovery_available && githubStatusQuery.data.operation_source_id && <p className="muted">{githubStatusQuery.data.operation_source_id}{githubStatusQuery.data.operation_source_generation ? ` · generation ${githubStatusQuery.data.operation_source_generation}` : ''}{githubStatusQuery.data.operation_configuration_revision !== null ? ` · revision ${githubStatusQuery.data.operation_configuration_revision}` : ''}</p>}
          {['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(githubStatusQuery.data?.coordinator_error_code ?? '') && <p className="error">{t('githubRevokeUnknown')}</p>}
          {githubStatusQuery.isError && <p className="error">{t('githubConnectionFailed')}</p>}
          {sourceId && <GitHubSummary sourceId={sourceId} />}
          <div className="form-actions">
            {provider === 'github' && githubStatusQuery.data?.sync.scope_sha256 && <Button className="secondary" disabled={busyAction !== '' || dirty || sourceStatus !== 'active' || revision < 1} onClick={resetGitHubHistory}>{busyAction === 'github-reset' ? t('saving') : t('githubResetHistory')}</Button>}
            {githubStatusQuery.data?.recovery_available && ['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(githubStatusQuery.data.coordinator_error_code ?? '')
              ? <Button className="secondary" disabled={busyAction !== '' || dirty} onClick={reviewGitHubDisconnect}>{busyAction === 'github-peers' ? t('loading') : t('githubRetryRevoke')}</Button>
              : githubStatusQuery.data?.recovery_available && ['authorization', 'refresh'].includes(githubStatusQuery.data.operation_kind ?? '')
              ? <Button className="secondary" disabled={busyAction !== '' || dirty || sourceStatus !== 'active' || revision < 1 || !githubStatusQuery.data.operation_source_id} onClick={reviewGitHubRecovery}>{t('githubReviewRecovery')}</Button>
              : githubStatusQuery.data?.coordinator_state !== 'idle' && githubStatusQuery.data?.coordinator_state !== undefined
              ? <p className="muted" role="status">{t('githubRecoveryWait')}</p>
              : githubStatusQuery.data?.state === 'ready'
              ? <>
                <Button className="secondary" disabled={busyAction !== '' || dirty} onClick={refreshGitHub}>{busyAction === 'github-refresh' ? t('saving') : t('githubRefresh')}</Button>
                <Button className="secondary" disabled={busyAction !== '' || dirty} onClick={reviewGitHubDisconnect}>{busyAction === 'github-peers' ? t('loading') : t('githubDisconnect')}</Button>
              </>
              : <Button className="secondary" disabled={busyAction !== '' || dirty || sourceStatus !== 'active' || revision < 1} onClick={connectGitHub}>{busyAction === 'github-connect' ? t('loading') : t('githubConnect')}</Button>}
          </div>
        </section>}
        {currentEntry?.quota_limits && <div className="source-capability"><strong>{t('quota')}</strong><p>{Object.entries(currentEntry.quota_limits).map(([key, value]) => `${key}: ${value}`).join(' · ')}</p></div>}
        {validation && <p className="status-panel" role="status">{t('validationPassed', { time: formatDate(validation.at, display.locale, display.timezone) })}{validation.verifiedBotId ? ` ${t('verifiedBot')}: ${validation.verifiedBotId}` : ''}{validation.scopeVerified ? ` · ${t('scopeVerified')}` : ''}</p>}
        {error && <p className="error" role="alert">{t(error as 'conflict' | 'configurationInvalid' | 'serviceUnavailable' | 'validationOutcomeUnknown' | 'sourceNameRequired' | 'actionFailed' | 'reloadFailed')}</p>}
        {notice && <p className="muted" role="status">{notice}</p>}
        {conflict && <div className="source-conflict" role="group" aria-label={t('conflict')}>
          <p className="error">{t('conflict')}</p>
          <Button className="secondary" disabled={busyAction !== ''} onClick={reloadServerConfiguration}>{t('reloadDiscard')}</Button>
        </div>}
        <div className="form-actions">
          <Button className="secondary" disabled={busyAction !== ''} onClick={validateDraft}>{busyAction === 'validate' ? t('validating') : t('validateDraft')}</Button>
          {sourceStatus === 'paused' && <Button className="secondary" disabled={busyAction !== ''} onClick={resumeSource}>{busyAction === 'resume' ? t('resuming') : t('resume')}</Button>}
          <Button className="secondary" disabled={busyAction !== '' || sourceStatus !== 'active'} onClick={() => saveConfiguration()}>{busyAction === 'save' ? t('saving') : t('save')}</Button>
          <Button disabled={busyAction !== '' || sourceStatus !== 'active'} onClick={saveAndEnable}>{busyAction === 'activate' ? t('enabling') : t('saveEnable')}</Button>
          {revision > 0 && visibleActivationState !== 'active' && !dirty && <Button className="secondary" disabled={busyAction !== ''} onClick={retryActivation}>{t('retryActivation')}</Button>}
        </div>
        <AlertDialog open={githubDisconnectOpen} onOpenChange={setGithubDisconnectOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t('githubDisconnect')}</AlertDialogTitle>
              <AlertDialogDescription>{t('githubPeerReview', { count: githubPeers.length })}</AlertDialogDescription>
            </AlertDialogHeader>
            <div><strong>{t('githubPeerList')}</strong><ul>{githubPeers.map((peer) => <li key={peer.source_id}>{peer.source_id}</li>)}</ul></div>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={busyAction !== ''}>{t('cancel')}</AlertDialogCancel>
              <Button disabled={busyAction !== ''} onClick={confirmGitHubDisconnect}>{busyAction === 'github-disconnect' ? t('saving') : t('githubDisconnect')}</Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        <AlertDialog open={githubRecoveryOpen} onOpenChange={(open) => { setGithubRecoveryOpen(open); if (!open) setGithubRecoveryReview(null); }}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t('githubRecoveryTitle')}</AlertDialogTitle>
              {githubRecoveryReview && <AlertDialogDescription>{t('githubRecoveryDisclosure', {
                operationId: githubRecoveryReview.operationId,
                kind: t(githubRecoveryReview.kind === 'authorization' ? 'githubRecoveryAuthorization' : 'githubRecoveryRefresh'),
                sourceId: githubRecoveryReview.originSourceId,
                generation: githubRecoveryReview.originGeneration,
                revision: githubRecoveryReview.originRevision,
              })}</AlertDialogDescription>}
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={busyAction !== ''}>{t('cancel')}</AlertDialogCancel>
              <Button disabled={busyAction !== '' || !githubRecoveryReview} onClick={confirmGitHubRecovery}>{busyAction === 'github-recovery' ? t('saving') : t('githubAcknowledgeConnect')}</Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        {revision > 0 && <section className="source-activation" aria-live="polite">
          <strong>{t('activationState')}: {t(({ queued: 'stateQueued', provisioning: 'stateProvisioning', saved_not_active: 'stateSavedNotActive', reconciliation_required: 'stateReconciliationRequired', disabled: 'stateDisabled', active: 'active' } as Record<string, string>)[visibleActivationState] ?? 'stateUnknown')}</strong>
          {activationQuery.isError && <p className="error">{t('activationRefreshFailed')}</p>}
          {visibleActivationError && <p className="error">{t('activationError')}: {visibleActivationError}</p>}
          {visibleActivationState === 'saved_not_active' && <p className="muted">{t('savedInactive')}</p>}
          {visibleActivationError === 'credential_delete_pending' && <p className="muted" role="status">{t('credentialPending')}</p>}
          {providerCredentialConfigured && sourceStatus === 'paused' && visibleActivationError !== 'deactivation_pending' && visibleActivationError !== 'credential_delete_pending' && <Button className="secondary" disabled={busyAction !== '' || dirty || Boolean(secret)} onClick={removeCredential}>{busyAction === 'remove' ? t('saving') : t('removeCredential')}</Button>}
        </section>}
        </>}
      </>}
    </>}
  </section>;
}
