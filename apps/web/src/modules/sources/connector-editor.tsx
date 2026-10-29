'use client';

import { CheckboxField, FieldGrid, SettingsActions, SettingsSection } from '@/modules/settings/settings-layout';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { PasswordInput } from '@/components/ui/password-input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { assignAuthorizationUrl } from '@/core/safe-url';
import { DisconnectDialog } from './disconnect-dialog';
import { SyncHistory } from './sync-history';
import { SourceAiToggles, type SourceAiOptions } from './source-ai-toggles';
import { collectAndWait, useSourceActions, waitForActivation } from './use-source-actions';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import { useWorkspace } from '@/core/workspace-context';
import { ProviderGuide, ProviderScope, ProviderTerms, type NativeProvider } from './provider-scope';
import { GitHubSummary } from './github-summary';
import { McpCollectionEditor } from './mcp-collection-editor';
import {
  activateConnector,
  ExecutionBackend,
  ConnectorCatalogEntry,
  ConnectorConfig,
  ConnectorConfiguration,
  ConnectorSettings,
  connectorConfigurationIsAtLeast,
  connectorKeys,
  createConnectorSource,
  ConnectorActivation,
  getConnectorActivation,
  getConnectorCatalog,
  listBuiltinSources,
  getProviderTerms,
  getMonotonicConnectorConfiguration,
  selectMonotonicConnectorConfiguration,
  removeProviderCredential,
  saveWorldProviderCredential,
  saveNativeRestCredential,
  saveConnectorConfiguration,
  Source,
  sourceKeys,
  updateSourceAiOptions,
  updateSourceStatus,
  validateDraftConnector,
  disconnectGitHubOAuth,
  acknowledgeGitHubReconnect,
  getGitHubPeers,
  getGitHubStatus,
  disconnectGoogleOAuth,
  getGoogleOAuthStatus,
  disconnectRedditOAuth,
  getRedditOAuthStatus,
  discoverGoogleResources,
  startGoogleOAuth,
  startRedditOAuth,
  resetGitHubSync,
  refreshGitHubOAuth,
  startGitHubOAuth,
  type CollectionRequest,
  type GitHubPeer,
  type GoogleResourceChoice,
} from './api';

const intervals = [15, 30, 60, 360, 1440] as const;
const genericProviderIds = new Set(['rss', 'web', 'rest', 'mcp']);
type Provider = string;

/** Uses the backend catalog to identify scheduled preset capabilities instead of duplicating provider IDs here. */
function isPreset(entry: ConnectorCatalogEntry | undefined): boolean {
  return entry?.kind === 'preset';
}

/** Identifies a registered native source from catalog type and adapter evidence. */
function isNativeEntry(entry: ConnectorCatalogEntry | undefined): boolean {
  return Boolean(entry && (entry.kind === 'connector' || entry.kind === 'preset')
    && entry.source_type && !genericProviderIds.has(entry.provider_id) && entry.code_available);
}

/** Reports credentials stored in the write-only provider slot; Alpha Vantage has its own established credential owner. */
function usesProviderCredential(entry: ConnectorCatalogEntry | undefined): boolean {
  return Boolean(entry && entry.provider_id !== 'alpha_vantage' && !genericProviderIds.has(entry.provider_id)
    && ((entry.key_fields ?? []).includes('api_key') || entry.auth_methods.includes('http_header')));
}

/** Reports API-key presets rendered with the catalog's key label and key-specific status. */
function usesApiKey(entry: ConnectorCatalogEntry | undefined): boolean {
  return Boolean((entry?.key_fields ?? []).includes('api_key') && entry?.provider_id !== 'alpha_vantage');
}

/** Header-only catalog authentication requires a stored bearer token; optional Mastodon tokens remain optional. */
function requiresHeaderCredential(entry: ConnectorCatalogEntry | undefined): boolean {
  return entry?.auth_methods.length === 1 && entry.auth_methods[0] === 'http_header';
}

/**
 * Reports a mandatory stored credential: API-key presets and header-only providers. Optional header tokens
 * (Mastodon) and generic sources (REST: `none` or a header) can be enabled without one.
 */
export function providerCredentialRequired(entry: ConnectorCatalogEntry | undefined): boolean {
  return usesProviderCredential(entry) && (usesApiKey(entry) || requiresHeaderCredential(entry));
}

const PROVIDER_GROUP_ORDER = ['news', 'economy', 'hazards', 'space', 'security', 'tech', 'other'] as const;
type ProviderGroup = (typeof PROVIDER_GROUP_ORDER)[number];
const PROVIDER_GROUP_KEY = {
  news: 'builtinCategoryNews', economy: 'builtinCategoryEconomy', hazards: 'builtinCategoryHazards',
  space: 'builtinCategorySpace', security: 'builtinCategorySecurity', tech: 'builtinCategoryTech', other: 'providerGroupOther',
} as const;

/** Groups catalog entries into display panels by category in a fixed order; uncategorised connectors (RSS, web, REST, accounts) fall into "other". Empty groups are dropped. */
function groupCatalog(entries: ConnectorCatalogEntry[]): [ProviderGroup, ConnectorCatalogEntry[]][] {
  return PROVIDER_GROUP_ORDER
    .map((group): [ProviderGroup, ConnectorCatalogEntry[]] => [group, entries.filter((entry) => (entry.category ?? 'other') === group)])
    .filter(([, rows]) => rows.length > 0);
}

/** True when the schedule/activation gate asks the owner to (re)enter a provider key. */
function keyNeeded(code: string | null | undefined): boolean {
  return code === 'credential_missing' || code === 'invalid_credential';
}

/** Checks whether a persisted provider identifier is present before using it as the editor selection. */
function isProvider(value: string): value is Provider {
  return value.length > 0;
}

/** Maps a provider identifier to its localized catalog key. */
function providerKey(providerId: string): string {
  return ({
    rss: 'providerRss', web: 'providerWeb', rest: 'providerRest', mcp: 'providerMcp', github: 'providerGithub',
    google_mail: 'providerGoogleMail', google_calendar: 'providerGoogleCalendar', google_drive: 'providerGoogleDrive',
    youtube: 'providerYoutube', arxiv: 'providerArxiv', huggingface: 'providerHuggingface', github_releases: 'providerGithubReleases',
    telegram: 'providerTelegram', alpha_vantage: 'providerAlphaVantage', coingecko: 'providerCoinGecko', open_meteo: 'providerOpenMeteo', google_news: 'providerGoogleNews', openalex: 'providerOpenAlex', reddit: 'providerReddit', hacker_news: 'providerHackerNews',
    mastodon: 'providerMastodon', bluesky: 'providerBluesky', x: 'providerX', vietnamese_press: 'providerVietnamesePress',
    gdelt_government: 'providerGdelt', finance: 'providerFinance', weather_disaster_climate: 'providerWeather', cyber_cve: 'providerCyber',
    map_osint: 'providerMapOsint', browser: 'providerBrowser', notes: 'providerNotes', health: 'providerHealth',
    personal_finance: 'providerPersonalFinance', iot: 'providerIot', notion: 'providerNotion', slack: 'providerSlack',
    home_assistant: 'providerHomeAssistant',
    bbc_world: 'providerBbcWorld', vnexpress_business: 'providerVnexpressBusiness', dantri_news: 'providerDantriNews', thanhnien_news: 'providerThanhnienNews', tuoitre_news: 'providerTuoitreNews', hn_top: 'providerHnTop', gdelt_economy: 'providerGdeltEconomy',
    world_bank: 'providerWorldBank', frankfurter: 'providerFrankfurter', ecb: 'providerEcb', binance: 'providerBinance',
    alternative_me: 'providerAlternativeMe', usgs: 'providerUsgs', coinpaprika: 'providerCoinpaprika', nasa_eonet: 'providerNasaEonet',
    cisa_kev: 'providerCisaKev', sec_edgar: 'providerSecEdgar',
  } as Record<string, string>)[providerId] ?? 'provider';
}

/** Builds the editable default configuration for the selected provider. */
function defaultConfiguration(provider: Provider, entry?: ConnectorCatalogEntry): ConnectorConfig {
  // Preset providers use catalog configuration so newly registered adapters need no parallel frontend list.
  // google_news is scoped: the owner types the query, so the example query is not preselected.
  if (provider === 'google_news' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), news_query: '' };
  // sec_edgar is scoped: the owner supplies their own CIK and contact email, so the example values are not preselected.
  if (provider === 'sec_edgar' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), sec_cik: '', sec_contact_email: '' };
  if (provider === 'openalex' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), scholarly_query: '' };
  // Keyed scoped presets: the owner supplies the scope, so the catalog example values are cleared.
  if (provider === 'fred' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), fred_series_id: '' };
  if (provider === 'openweathermap' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), owm_latitude: undefined, owm_longitude: undefined };
  if (provider === 'openaq' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), openaq_location_id: undefined };
  if (provider === 'nasa_firms' && entry?.example_config) return { ...(entry.example_config as ConnectorConfig), firms_country: '' };
  if (isPreset(entry) && entry?.example_config) return entry.example_config as ConnectorConfig;
  if (isNativeEntry(entry)) return {
    timeout_seconds: 30,
    timezone: 'Asia/Ho_Chi_Minh',
    schedule_interval_minutes: provider === 'alpha_vantage' ? 1440 : provider === 'youtube' || provider === 'arxiv' ? 15 : 30,
    history_mode: provider === 'telegram' ? 'pending_updates' : 'returned_snapshot',
    ...(provider === 'reddit' ? { reddit_subreddit: '' } : {}),
    ...(provider === 'open_meteo' ? { weather_metrics: ['temperature_2m'], weather_timezone: 'UTC', weather_latitude: 0, weather_longitude: 0 } : {}),
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
  if (error instanceof ApiError && error.status === 409 && (error.code === 'builtin_already_added' || error.message === 'builtin_already_added')) return 'builtinAlreadyAdded';
  if (error instanceof ApiError && error.status === 409) return 'conflict';
  if (error instanceof ApiError && error.status === 422) return 'configurationInvalid';
  if (error instanceof ApiError && error.status === 503) return 'serviceUnavailable';
  if (error instanceof Error && error.message === 'source_name_required') return 'sourceNameRequired';
  return 'actionFailed';
}

/** Pause and Disconnect controls for the editor header, sharing handlers and the confirmation dialog with the source list rows. */
function EditorSourceActions({ source, activation, disabled, onChanged, onPurgeStarted, onClose, onBusyChange }: {
  source: Source;
  activation?: ConnectorActivation;
  disabled: boolean;
  onChanged: () => void;
  onPurgeStarted?: (operationId: string) => void;
  onClose: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const t = useTranslations('sources');
  const actions = useSourceActions({ source, activation, onChanged: () => { onChanged(); onClose(); }, onPurgeStarted });
  const { busy: actionsBusy } = actions;
  useEffect(() => { onBusyChange(actionsBusy); return () => onBusyChange(false); }, [actionsBusy, onBusyChange]);
  const off = disabled || actions.busy;
  return <>
    {source.status === 'active' && <Button variant="outline" disabled={off} onClick={() => void actions.toggleStatus()}>{actions.busy ? t('pausing') : t('pause')}</Button>}
    <DisconnectDialog sourceId={source.id} name={source.name} archived={source.status === 'archived'} disabled={off} onConfirm={(deleteData) => void actions.disconnect(deleteData)}
      trigger={<Button variant="outline" aria-disabled={off}>{actions.busy ? t('disconnecting') : t(source.status === 'archived' ? 'disconnectDelete' : 'disconnectAction')}</Button>} />
    {actions.error && <p className="error" role="alert">{t('actionFailed')}</p>}
  </>;
}

/** Wizard step an embedded editor renders; every other section stays mounted but hidden so the draft survives step changes. */
export type EditorStep = 'provider' | 'connect' | 'choose' | 'collect' | 'verify';
/** Progress of the final "save and test fetch" run. */
export type VerifyState = { phase: 'idle' | 'saving' | 'activating' | 'collecting' | 'succeeded' | 'no_changes' | 'failed'; code?: string | null; runId?: string | null };
/** Read-only editor state the wizard shell uses to gate navigation. */
export type EditorSnapshot = { sourceId: string; provider: string; busy: boolean; providerOk: boolean; connectOk: boolean; chooseOk: boolean; verify: VerifyState };
/** Imperative actions the wizard shell triggers on the editor. */
export type EditorActions = {
  /** Creates the source for the chosen provider and name; resolves its id or null on failure. */
  createSource: () => Promise<string | null>;
  /** Runs work a step needs before the shell advances; resolves false to stay on the step. */
  prepareNext: (step: EditorStep) => Promise<boolean>;
  /** Saves, enables, waits for activation and runs one real collection, reporting through the snapshot. */
  verify: () => Promise<void>;
};
const IDLE_VERIFY: VerifyState = { phase: 'idle' };
const ACTIVATION_TIMEOUT_MS = 60_000;
const ACTIVATION_POLL_MS = 2_000;
/** Hard cap for the whole Save and test flow (activation plus collection); under the 120 s e2e/UX budget. */
const VERIFY_TOTAL_MS = 105_000;

/** Milliseconds left until `deadline` (epoch ms), never negative; kept outside the component so render purity lint ignores the clock read. */
function msUntil(deadline: number): number {
  return Math.max(deadline - Date.now(), 0);
}

/** Edit and activate connectors through owner APIs; local FHIR/Home Assistant scopes use encrypted bearer tokens, while GitHub uncertainty and retained history stay explicit. */
export function ConnectorEditor({
  source,
  onClose,
  onChanged,
  onPurgeStarted,
  registerTransitionGuard,
  step,
  actionsRef,
  onSnapshot,
}: {
  source?: Source | null;
  onClose: () => void;
  onChanged: () => void;
  onPurgeStarted?: (operationId: string) => void;
  registerTransitionGuard: (guard: (() => boolean) | null) => void;
  /** When set the editor is embedded in the wizard: it shows only this step and hides its own header, stepper and footer. */
  step?: EditorStep;
  /** Receives the editor's imperative actions (re-assigned every render). */
  actionsRef?: MutableRefObject<EditorActions | null>;
  /** Called whenever the gating snapshot changes. */
  onSnapshot?: (snapshot: EditorSnapshot) => void;
}) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const display = useDisplayPreferences();
  const { registerLeaveGuard } = useGuardedNavigation();
  const queryClient = useQueryClient();
  const initialProvider = source?.provider && isProvider(source.provider) ? source.provider : source?.type === 'mcp' ? 'mcp' : source?.type === 'rss' ? 'rss' : source?.type === 'web' ? 'web' : 'rest';
  const [provider, setProvider] = useState<Provider>(initialProvider);
  const [name, setName] = useState(source?.name ?? '');
  const [aiOptions, setAiOptions] = useState<SourceAiOptions>({ graph_enabled: source?.graph_enabled ?? true, rag_index_enabled: source?.rag_index_enabled ?? true });
  const [aiOptionsError, setAiOptionsError] = useState(false);
  const [sourceId, setSourceId] = useState(source?.id ?? '');
  const providerRef = useRef(provider);
  const nameRef = useRef(name);
  const sourceIdRef = useRef(sourceId);
  const sourceGenerationRef = useRef(source?.generation ?? 0);
  const [sourceGeneration, setSourceGeneration] = useState(source?.generation ?? 0);
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
  const [backendChoice, setBackendChoice] = useState<ExecutionBackend | null>(null);
  const workspace = useWorkspace();
  const revisionRef = useRef(revision);
  const [activationState, setActivationState] = useState(source?.status === 'paused' ? 'disabled' : 'saved_not_active');
  const [sourceStatusOverride, setSourceStatusOverride] = useState<Source['status'] | null>(null);
  const [activationError, setActivationError] = useState<string | null>(null);
  const [providerCredentialConfigured, setProviderCredentialConfigured] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<{ at: string; generation: number; revision: number; verifiedBotId?: string | null; scopeVerified?: boolean | null } | null>(null);
  const [busyAction, setBusyAction] = useState('');
  const [actionsBusy, setActionsBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState('');
  const [verify, setVerify] = useState<VerifyState>(IDLE_VERIFY);
  const verifyController = useRef<AbortController | null>(null);
  /** Stable server code of the last rejected save/enable (e.g. local_only_source), surfaced by the verify step instead of a generic save failure. */
  const saveFailCode = useRef<string | null>(null);
  const [githubPeers, setGithubPeers] = useState<GitHubPeer[]>([]);
  const [githubDisconnectOpen, setGithubDisconnectOpen] = useState(false);
  const [githubRecoveryReview, setGithubRecoveryReview] = useState<{ operationId: string; kind: 'authorization' | 'refresh'; originSourceId: string; originGeneration: number; originRevision: number; sourceId: string; generation: number; revision: number } | null>(null);
  const [githubRecoveryOpen, setGithubRecoveryOpen] = useState(false);
  const [googleResources, setGoogleResources] = useState<GoogleResourceChoice[]>([]);
  const [googleResourceParent, setGoogleResourceParent] = useState<string | undefined>();
  const [googleResourcePath, setGoogleResourcePath] = useState<GoogleResourceChoice[]>([]);
  const [googleResourcesTruncated, setGoogleResourcesTruncated] = useState(false);
  useEffect(() => {
    // Reset discovered Google resources whenever the edited source or provider changes (state is keyed to them).
    /* eslint-disable react-hooks/set-state-in-effect */
    setGoogleResources([]);
    setGoogleResourceParent(undefined);
    setGoogleResourcePath([]);
    setGoogleResourcesTruncated(false);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [sourceId, provider]);
  const [googleDiscoveryError, setGoogleDiscoveryError] = useState<'failed' | 'limit' | null>(null);
  const githubReviewRef = useRef<{ sourceId: string; sourceGeneration: number; revision: number; peers: GitHubPeer[] } | null>(null);
  const loadedFor = useRef('');
  const serverConfigurationRef = useRef<ConnectorConfiguration | null>(null);
  const dirtyRef = useRef(dirty);
  useLayoutEffect(() => {
    providerRef.current = provider;
    nameRef.current = name;
    sourceIdRef.current = sourceId;
    revisionRef.current = revision;
    dirtyRef.current = dirty || Boolean(secret);
  });
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
    setAuthMethod(baseline?.auth_method === 'http_header' ? 'http_header' : baseline?.auth_method === 'telegram_bot_token' ? 'telegram_bot_token' : 'none');
    setTelegramSecretAction('keep');
    setAuthHeaderName(baseline?.auth_header_name ?? 'Authorization');
    setRevision(baseline?.expected_revision ?? 0);
    revisionRef.current = baseline?.expected_revision ?? 0;
    if (baseline) {
      sourceGenerationRef.current = baseline.source_generation;
      setSourceGeneration(baseline.source_generation);
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
  const googleStatusQuery = useQuery({
    queryKey: ['google-oauth-status', sourceId],
    queryFn: ({ signal }) => getGoogleOAuthStatus(sourceId, signal),
    enabled: Boolean(sourceId) && ['google_mail', 'google_calendar', 'google_drive'].includes(provider),
    refetchInterval: sourceId && ['google_mail', 'google_calendar', 'google_drive'].includes(provider) ? 10_000 : false,
  });
  const redditStatusQuery = useQuery({
    queryKey: ['reddit-oauth-status', sourceId],
    queryFn: ({ signal }) => getRedditOAuthStatus(sourceId, signal),
    enabled: Boolean(sourceId) && provider === 'reddit',
    refetchInterval: sourceId && provider === 'reddit' ? 10_000 : false,
  });
  const catalogQuery = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal) });
  const currentEntry = (catalogQuery.data ?? []).find((entry) => entry.provider_id === provider);
  const termsGated = isPreset(currentEntry) && Boolean(sourceId);
  const termsQuery = useQuery({ queryKey: connectorKeys.terms(sourceId), queryFn: ({ signal }) => getProviderTerms(sourceId, signal), enabled: termsGated });
  const termsBlocked = isPreset(currentEntry) && (!termsGated || termsQuery.data?.eligible !== true);
  // Built-ins are switched on from the Built-in sources section, not created as tiles here.
  // Also loaded in edit mode: a built-in's review-class terms are settled by its Resume action, not this form.
  const builtinQuery = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  const builtinIds = useMemo(() => new Set((builtinQuery.data ?? []).map((item) => item.id)), [builtinQuery.data]);
  const builtinReview = (builtinQuery.data ?? []).some((item) => item.id === provider && item.eligibility === 'review');
  // One source per built-in: a provider whose built-in already has a source cannot be created again from the catalog.
  const addedBuiltinIds = useMemo(() => new Set((builtinQuery.data ?? []).filter((item) => item.source_id !== null).map((item) => item.id)), [builtinQuery.data]);
  const catalog = useMemo(() => (catalogQuery.data ?? []).filter((entry) => !builtinIds.has(entry.provider_id)), [catalogQuery.data, builtinIds]);
  // Built-ins are dropped from the add grid (`catalog`) but edit mode still needs their guide and terms panel.
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
    setSourceGeneration(value.source_generation);
    setProvider(value.provider && isProvider(value.provider) ? value.provider : value.source_type === 'mcp' ? 'mcp' : value.source_type === 'rss' ? 'rss' : value.source_type === 'web' ? 'web' : 'rest');
    setConfiguration(value.configuration);
    setAuthMethod(value.auth_method === 'http_header' ? 'http_header' : value.auth_method === 'telegram_bot_token' ? 'telegram_bot_token' : 'none');
    setAuthHeaderName(value.auth_header_name ?? 'Authorization');
    setRevision(value.expected_revision);
    setBackendChoice(value.execution_backend ?? null);
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
      setSourceGeneration(0);
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
    verifyController.current?.abort();
  }, []);

  // Registered providers never use REST header auth (their key goes through a dedicated credential slot), so a stale
  // 'http_header' choice from a previously selected REST card must not reach the backend's provider auth check.
  const effectiveAuth = provider === 'telegram' ? 'telegram_bot_token' : provider === 'mastodon' ? (secret.trim() ? 'http_header' : 'none') : requiresHeaderCredential(currentEntry) ? 'http_header' : isNativeEntry(currentEntry) ? 'none' : authMethod;
  const settings = useMemo<ConnectorSettings>(() => ({
    expected_revision: revision,
    configuration,
    auth_method: effectiveAuth,
    ...(effectiveAuth === 'http_header' ? { auth_header_name: authHeaderName } : {}),
  }), [revision, configuration, effectiveAuth, authHeaderName]);

  /**
   * Switches the add-flow provider card and resets every provider-specific draft field (configuration, auth mode,
   * header name, secret, validation) so nothing from the previously selected card leaks into the new source.
   * Only valid before the source exists; the source name is replaced only when it is empty or still the previous preset label.
   */
  function selectProvider(next: Provider, entry: ConnectorCatalogEntry) {
    setProvider(next);
    setConfiguration(defaultConfiguration(next, entry));
    setAuthMethod('none');
    setAuthHeaderName('Authorization');
    setSecret('');
    // A new Telegram source has no stored token to keep, so it must start in replace mode.
    setTelegramSecretAction(next === 'telegram' ? 'replace' : 'keep');
    setBackendChoice(null);
    setValidation(null);
    setNotice('');
    setError('');
    if (isPreset(entry) && (!name.trim() || name === currentEntry?.label)) setName(entry.label);
    markDraftChanged();
  }

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
    // A finished test fetch no longer describes the edited draft.
    setVerify((current) => (current.phase === 'idle' ? current : IDLE_VERIFY));
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

  /** Starts one collection run for an active source, reports its progress, and resolves the final collection request (null when aborted, rejected or not started). */
  async function collectNow(): Promise<CollectionRequest | null> {
    const id = sourceId;
    if (!id) return null;
    const token = beginRequest('collect');
    if (!token) return null;
    setError('');
    setNotice('');
    let outcome: CollectionRequest | null = null;
    try {
      const final = await collectAndWait(id, csrfToken, {
        signal: token.controller.signal,
        onAccepted: (result) => { if (requestIsCurrent(token)) setNotice(t('collectResult', { status: result.status })); },
        onUpdate: (update) => { if (requestIsCurrent(token)) setNotice(t('collectResult', { status: update.status })); },
      });
      if (!final || !requestIsCurrent(token)) return null;
      outcome = final;
      if (final.status === 'failed' || final.status === 'cancelled') { setError('actionFailed'); setNotice(t('collectFailedCode', { code: final.error_code ?? final.status })); }
      await queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(id) });
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
    } catch (cause) {
      if (requestIsCurrent(token)) setError(errorText(cause));
    } finally {
      endRequest(token);
    }
    return outcome;
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

  /** Edit mode: applies one toggle immediately with a PATCH and rolls the checkbox back if the save fails. */
  async function changeAiOptions(next: SourceAiOptions) {
    const previous = aiOptions;
    setAiOptions(next);
    setAiOptionsError(false);
    if (!source) return; // create flow: the values are sent with the create request
    try {
      const patch: Partial<SourceAiOptions> = {};
      if (next.graph_enabled !== previous.graph_enabled) patch.graph_enabled = next.graph_enabled;
      if (next.rag_index_enabled !== previous.rag_index_enabled) patch.rag_index_enabled = next.rag_index_enabled;
      await updateSourceAiOptions(source.id, patch, csrfToken);
      onChanged();
    } catch {
      setAiOptions(previous);
      setAiOptionsError(true);
    }
  }

  /** Creates a connector source on first save when necessary and returns its source identifier. */
  async function ensureSource(token: RequestToken): Promise<string> {
    if (sourceId) return sourceId;
    if (!name.trim()) throw new Error('source_name_required');
    const sourceType = currentEntry?.source_type;
    if (!sourceType || sourceType === 'manual') throw new Error('provider_source_type_unavailable');
    const created = await createConnectorSource(sourceType, name.trim(), csrfToken, token.controller.signal, genericProviderIds.has(provider) ? undefined : provider, aiOptions);
    if (!requestIsCurrent(token)) throw new DOMException('editor_closed', 'AbortError');
    sourceGenerationRef.current = created.generation;
    setSourceGeneration(created.generation);
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
      setSourceGeneration(acknowledged.source_generation);
      loadedFor.current = id;
      setRevision(acknowledged.expected_revision);
      revisionRef.current = acknowledged.expected_revision;
      setActivationState(result.state);
      setActivationError(result.error_code);
      setProviderCredentialConfigured(acknowledged.provider_credential_configured);
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

  /** Saves pending connector settings before activation and refreshes owner state after success or failure; resolves the accepted activation (state may still be queued) or null when nothing was activated. */
  async function saveAndEnable(): Promise<{ id: string; state: string; errorCode: string | null } | null> {
    const token = beginRequest('save_enable');
    if (!token) return null;
    let actionSourceId: string | null = sourceId || null;
    // Save persists scope first; only the following activation request may carry replacement secret bytes.
    const activationDraft = { authMethod, secret, telegramSecretAction };
    let nextRevision = revision;
    setError('');
    setNotice('');
    try {
      if (dirty || revision === 0) {
        const saved = await saveConfiguration(token);
        if (saved === null || !requestIsCurrent(token)) return null;
        actionSourceId = saved.id;
        nextRevision = saved.revision;
      }
      const id = actionSourceId;
      if (!id || !requestIsCurrent(token)) return null;
      const replacingWorldKey = provider === 'alpha_vantage' && Boolean(activationDraft.secret);
      if (provider === 'alpha_vantage' && !replacingWorldKey && !providerCredentialConfigured) {
        setError('providerSecretRequired');
        return null;
      }
      const replacingNativeKey = usesProviderCredential(currentEntry) && Boolean(activationDraft.secret);
      if (providerCredentialRequired(currentEntry) && !replacingNativeKey && !providerCredentialConfigured) {
        setError('providerSecretRequired');
        return null;
      }
      if (replacingNativeKey) {
        await saveNativeRestCredential(id, nextRevision, activationDraft.secret, csrfToken, token.controller.signal);
      }
      if (replacingWorldKey) {
        await saveWorldProviderCredential(id, sourceGenerationRef.current, nextRevision, activationDraft.secret, csrfToken, token.controller.signal);
      }
      const replacingSecret = provider === 'alpha_vantage' || usesProviderCredential(currentEntry) ? false : activationDraft.authMethod === 'telegram_bot_token' ? activationDraft.telegramSecretAction === 'replace' : Boolean(activationDraft.secret);
      const result = await activateConnector(id, nextRevision, replacingSecret ? 'replace' : 'keep', replacingSecret ? activationDraft.secret : undefined, csrfToken, token.controller.signal, backendChoice ?? undefined);
      if (!requestIsCurrent(token)) return null;
      setActivationState(result.state);
      setActivationError(result.error_code);
      setProviderCredentialConfigured(replacingWorldKey || replacingNativeKey || activationDraft.authMethod === 'http_header' || activationDraft.authMethod === 'telegram_bot_token' || providerCredentialConfigured);
      const replacementAccepted = replacingSecret || replacingWorldKey || replacingNativeKey;
      if (replacementAccepted) { setSecret(''); setTelegramSecretAction('keep'); }
      if (draftVersion.current === token.draftVersion) {
        setDirty(false);
        dirtyRef.current = replacementAccepted ? false : Boolean(activationDraft.secret);
      }
      queryClient.setQueryData(connectorKeys.activation(id), result);
      await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(id) });
      if (!requestIsCurrent(token)) return null;
      await queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(id) });
      if (!requestIsCurrent(token)) return null;
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (!requestIsCurrent(token)) return null;
      onChanged();
      if (result.state !== 'active') setNotice(t('savedInactive'));
      return { id, state: result.state, errorCode: result.error_code };
    } catch (cause) {
      if (!requestIsCurrent(token)) return null;
      // Local-only sources are refused activation by design; report that code rather than a generic conflict.
      if (cause instanceof ApiError && cause.code === 'local_only_source') {
        saveFailCode.current = 'local_only_source';
        setError('');
      } else setError(errorText(cause));
      setNotice(t('savedInactive'));
      if (actionSourceId) await refreshOwnerState(actionSourceId, token);
      if (!requestIsCurrent(token)) return null;
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (!requestIsCurrent(token)) return null;
      onChanged();
      return null;
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
      const replacingNativeKey = usesProviderCredential(currentEntry) && Boolean(requestedSecret);
      if (replacingNativeKey) await saveNativeRestCredential(id, revision, requestedSecret, csrfToken, token.controller.signal);
      if (providerCredentialRequired(currentEntry) && !replacingNativeKey && !providerCredentialConfigured) {
        setError('providerSecretRequired');
        return;
      }
      const replacingSecret = authMethod === 'telegram_bot_token' ? telegramSecretAction === 'replace' : usesProviderCredential(currentEntry) ? false : Boolean(requestedSecret);
      const result = await activateConnector(id, revision, replacingSecret ? 'replace' : 'keep', replacingSecret ? requestedSecret : undefined, csrfToken, token.controller.signal, backendChoice ?? undefined);
      if (!requestIsCurrent(token)) return;
      setActivationState(result.state);
      setActivationError(result.error_code);
      setProviderCredentialConfigured(replacingNativeKey || authMethod === 'http_header' || authMethod === 'telegram_bot_token' || providerCredentialConfigured);
      const replacementAccepted = replacingSecret || replacingNativeKey;
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
      assignAuthorizationUrl(result.authorization_url);
    } catch {
      if (requestIsCurrent(token)) setError('githubConnectionFailed');
    } finally {
      endRequest(token);
    }
  }

  /** Saves the selected Google scope, then starts the source-revision-bound read-only OAuth flow. */
  async function connectGoogle(savedConfiguration?: { id: string; revision: number }) {
    if (!['google_mail', 'google_calendar', 'google_drive'].includes(provider)) return;
    const saved = savedConfiguration ?? (dirty || revision === 0 ? await saveConfiguration() : null);
    const id = saved?.id ?? sourceId;
    const expectedRevision = saved?.revision ?? revisionRef.current;
    if (!id || dirtyRef.current || expectedRevision < 1) return;
    const token = beginRequest('google-connect');
    if (!token) return;
    const generation = sourceGenerationRef.current;
    setError('');
    try {
      const result = await startGoogleOAuth(id, provider, generation, expectedRevision, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token) || sourceIdRef.current !== id || sourceGenerationRef.current !== generation
          || revisionRef.current !== expectedRevision || dirtyRef.current) return;
      assignAuthorizationUrl(result.authorization_url);
    } catch {
      if (requestIsCurrent(token)) setError('googleConnectionFailed');
    } finally {
      endRequest(token);
    }
  }

  /** Saves the selected subreddit, then starts the source-bound read-only Reddit OAuth flow. */
  async function connectReddit(savedConfiguration?: { id: string; revision: number }) {
    if (provider !== 'reddit') return;
    const saved = savedConfiguration ?? (dirty || revision === 0 ? await saveConfiguration() : null);
    const id = saved?.id ?? sourceId;
    const expectedRevision = saved?.revision ?? revisionRef.current;
    if (!id || dirtyRef.current || expectedRevision < 1) return;
    const token = beginRequest('reddit-connect');
    if (!token) return;
    const generation = sourceGenerationRef.current;
    setError('');
    try {
      const result = await startRedditOAuth(id, generation, expectedRevision, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token) || sourceIdRef.current !== id || sourceGenerationRef.current !== generation
          || revisionRef.current !== expectedRevision || dirtyRef.current) return;
      assignAuthorizationUrl(result.authorization_url);
    } catch {
      if (requestIsCurrent(token)) setError('redditConnectionFailed');
    } finally {
      endRequest(token);
    }
  }

  /** Revokes only the current Reddit grant revision and refreshes its secret-free status. */
  async function disconnectReddit() {
    const status = redditStatusQuery.data;
    if (!sourceId || !status || status.state !== 'ready' || dirtyRef.current) return;
    const token = beginRequest('reddit-disconnect');
    if (!token) return;
    try {
      await disconnectRedditOAuth(sourceId, sourceGenerationRef.current, revisionRef.current,
        status.token_revision, csrfToken, token.controller.signal);
      if (requestIsCurrent(token)) {
        await Promise.all([
          redditStatusQuery.refetch(),
          queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(sourceId) }),
        ]);
      }
    } catch {
      if (requestIsCurrent(token)) setError('redditDisconnectFailed');
    } finally {
      endRequest(token);
    }
  }

  /** Loads owner-visible Google resource choices under the current source, configuration and OAuth grant revisions. */
  async function loadGoogleResources(parentId?: string, path: GoogleResourceChoice[] = []) {
    const id = sourceId;
    const selectedProvider = provider;
    const status = googleStatusQuery.data;
    if (!id || !status || status.state !== 'ready' || !['google_mail', 'google_calendar', 'google_drive'].includes(selectedProvider)) return;
    const token = beginRequest('google-discovery');
    if (!token) return;
    const generation = sourceGenerationRef.current;
    const expectedRevision = revisionRef.current;
    const tokenRevision = status.token_revision;
    setGoogleDiscoveryError(null);
    try {
      const result = await discoverGoogleResources(id, selectedProvider as "google_mail" | "google_calendar" | "google_drive", generation, expectedRevision, tokenRevision, parentId, token.controller.signal);
      if (!requestIsCurrent(token) || sourceIdRef.current !== id || sourceGenerationRef.current !== generation
          || providerRef.current !== selectedProvider || revisionRef.current !== expectedRevision || result.token_revision < tokenRevision) return;
      setGoogleResources(result.items);
      setGoogleResourceParent(parentId);
      setGoogleResourcePath(path);
      setGoogleResourcesTruncated(result.truncated);
      if (result.token_revision !== tokenRevision) await googleStatusQuery.refetch();
    } catch {
      if (requestIsCurrent(token)) setGoogleDiscoveryError('failed');
    } finally {
      endRequest(token);
    }
  }

  /** Adds or removes a discovered provider resource while enforcing the backend's saved selection bound. */
  function toggleGoogleResource(resourceId: string, checked: boolean) {
    const field = provider === 'google_mail' ? 'google_mail_label_ids'
      : provider === 'google_calendar' ? 'google_calendar_ids' : 'google_drive_ids';
    const selected = new Set(configuration[field] ?? []);
    if (checked) selected.add(resourceId); else selected.delete(resourceId);
    const values = [...selected];
    const maximum = provider === 'google_mail' ? 10 : provider === 'google_calendar' ? 25 : 100;
    if (values.length > maximum) {
      setGoogleDiscoveryError('limit');
      return;
    }
    setGoogleDiscoveryError(null);
    if (field === 'google_mail_label_ids') changeConfiguration(field, values);
    else if (field === 'google_calendar_ids') changeConfiguration(field, values);
    else changeConfiguration(field, values);
  }

  /** Revokes only the current Google grant revision and refreshes its secret-free status. */
  async function disconnectGoogle() {
    const status = googleStatusQuery.data;
    if (!sourceId || !status || status.state !== 'ready' || dirtyRef.current) return;
    const token = beginRequest('google-disconnect');
    if (!token) return;
    try {
      await disconnectGoogleOAuth(sourceId, sourceGenerationRef.current, revisionRef.current,
        status.token_revision, csrfToken, token.controller.signal);
      if (requestIsCurrent(token)) {
        await Promise.all([
          googleStatusQuery.refetch(),
          queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(sourceId) }),
        ]);
      }
    } catch {
      if (requestIsCurrent(token)) setError('googleDisconnectFailed');
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
      assignAuthorizationUrl(result.authorization_url);
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

  /** Creates the connector source from the current provider and name, then advances the editor to that source; resolves the new id or null on failure. */
  async function createSource(): Promise<string | null> {
    const token = beginRequest('create');
    if (!token) return null;
    setError('');
    try {
      return await ensureSource(token);
    } catch (cause) {
      if (requestIsCurrent(token)) setError(errorText(cause));
      return null;
    } finally {
      endRequest(token);
    }
  }

  /**
   * Saves and enables the source, waits for activation to become active, then runs one real collection.
   * Progress and the terminal outcome are published through `verify`; failures keep the dialog open with an error code.
   * MCP sources are activated by their own editor, so only the activation wait and collection run here.
   */
  async function runVerify() {
    verifyController.current?.abort();
    const controller = new AbortController();
    verifyController.current = controller;
    const stillCurrent = () => verifyController.current === controller && !controller.signal.aborted;
    const fail = (code: string) => { if (stillCurrent()) setVerify({ phase: 'failed', code }); };
    saveFailCode.current = null;
    const verifyDeadline = Date.now() + VERIFY_TOTAL_MS; // eslint-disable-line react-hooks/purity -- event handler, not render
    setVerify({ phase: 'saving' });
    try {
      let id = sourceId;
      if (provider !== 'mcp') {
        const saved = await saveAndEnable();
        if (!stillCurrent()) return;
        if (!saved) { fail(saveFailCode.current ?? 'save_failed'); return; }
        id = saved.id;
      }
      if (!id) { fail('source_missing'); return; }
      setVerify({ phase: 'activating' });
      // Activation is asynchronous (queued/provisioning); poll the owner state until active, a hard failure, or timeout.
      const activation = await waitForActivation(id, controller.signal, { timeoutMs: Math.min(ACTIVATION_TIMEOUT_MS, msUntil(verifyDeadline)), intervalMs: ACTIVATION_POLL_MS });
      if (!activation || !stillCurrent()) return;
      if (!activation.ok) { fail(activation.code ?? 'activation_failed'); return; }
      setVerify({ phase: 'collecting' });
      const final = await collectAndWait(id, csrfToken, { signal: controller.signal, timeoutMs: Math.max(msUntil(verifyDeadline), 5_000) });
      if (!final || !stillCurrent()) return;
      void queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(id) });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      onChanged();
      if (final.status === 'succeeded' || final.status === 'no_changes') setVerify({ phase: final.status, runId: final.ingestion_run_id });
      else fail(final.error_code ?? final.status);
    } catch (cause) {
      fail(cause instanceof ApiError ? cause.message || `http_${cause.status}` : 'request_failed');
    }
  }

  /** Saves Google resource selection and reconnects for the new configuration revision; GitHub saves before OAuth. */
  async function prepareNext(from: EditorStep): Promise<boolean> {
    if (from === 'choose' && ['google_mail', 'google_calendar', 'google_drive'].includes(provider) && (dirty || revision === 0)) {
      const saved = await saveConfiguration();
      if (!saved) return false;
      setNotice(t('googleReconnectForScope'));
      await connectGoogle(saved);
      return false;
    }
    if (from === 'connect' && provider === 'github' && revision === 0) return (await saveConfiguration()) !== null;
    return true;
  }

  const locked = busyAction !== '' || actionsBusy;
  const activationLabel = t((({ queued: 'stateQueued', provisioning: 'stateProvisioning', saved_not_active: 'stateSavedNotActive', reconciliation_required: 'stateReconciliationRequired', disabled: 'stateDisabled', active: 'statusActive' } as Record<string, string>)[visibleActivationState] ?? 'stateUnknown') as 'stateUnknown');
  const currentStep = revision > 0 ? 'collect' : validation ? 'choose' : 'connect';
  const steps = [
    { id: 'connect', title: t('stepConnect'), status: validation ? t('stepValidated') : providerCredentialConfigured ? t('stepCredentialSaved') : t('stepNotValidated') },
    { id: 'choose', title: t('stepChooseData'), status: t(providerKey(provider) as 'provider') },
    { id: 'collect', title: t('stepCollect'), status: t('everyMinutes', { minutes: configuration.schedule_interval_minutes }) },
  ] as const;

  const embedded = step !== undefined;
  /** True when the section tagged with one of these wizard steps should be visible (always true outside the wizard). */
  const showStep = (...visible: EditorStep[]) => !embedded || visible.includes(step);
  const urlOk = provider === 'rss' ? Boolean(configuration.feed_url?.trim()) : provider === 'web' || provider === 'rest' ? Boolean(configuration.url?.trim()) : true;
  const keyedOrAlpha = provider === 'alpha_vantage' || (usesApiKey(currentEntry) && (backendChoice ?? 'native') === 'native');
  // Credentialed providers must prove the credential (validation) or provide the key before the wizard moves on.
  // Validation needs the scope/URL entered in the later "choose data" step, so it cannot gate Connect; credentials can.
  const connectOk = provider === 'reddit' ? redditStatusQuery.data?.state === 'ready'
    : ['google_mail', 'google_calendar', 'google_drive'].includes(provider) ? googleStatusQuery.data?.state === 'ready' && providerCredentialConfigured
    : provider === 'telegram' ? (telegramSecretAction === 'replace' && Boolean(secret)) || providerCredentialConfigured
    : provider === 'mastodon' ? true
      : requiresHeaderCredential(currentEntry) ? Boolean(secret) || providerCredentialConfigured
    : provider === 'rest' && authMethod === 'http_header' ? Boolean(secret) || providerCredentialConfigured
      : keyedOrAlpha ? Boolean(secret) || providerCredentialConfigured : true;
  const providerOk = Boolean(name.trim() && currentEntry?.source_type && currentEntry.code_available && ['available', 'implemented', 'requires_credentials'].includes(currentEntry.availability));
  const googleScopeOk = provider === 'reddit' ? Boolean(configuration.reddit_subreddit?.trim())
    : provider === 'google_mail' ? Boolean(configuration.google_mail_label_ids?.length)
    : provider === 'google_calendar' ? Boolean(configuration.google_calendar_ids?.length)
      : provider === 'google_drive' ? Boolean(configuration.google_drive_ids?.length)
        : provider === 'x' ? Boolean(configuration.x_account?.trim()) !== Boolean(configuration.x_query?.trim())
          : provider === 'health' ? Boolean(configuration.fhir_base_url?.trim() && configuration.fhir_patient_id?.trim())
          : provider === 'home_assistant' ? Boolean(configuration.ha_base_url?.trim() && configuration.ha_entity_ids?.length) : true;
  const chooseOk = !termsBlocked && urlOk && googleScopeOk;
  const snapshot = useMemo<EditorSnapshot>(() => ({ sourceId, provider, busy: locked, providerOk, connectOk, chooseOk, verify }),
    [sourceId, provider, locked, providerOk, connectOk, chooseOk, verify]);
  useEffect(() => { onSnapshot?.(snapshot); }, [snapshot, onSnapshot]);
  // Re-assigned each render so the wizard always calls closures over the latest draft.
  useLayoutEffect(() => { if (actionsRef) actionsRef.current = { createSource, prepareNext, verify: runVerify }; });
  const verifyBusy = verify.phase === 'saving' || verify.phase === 'activating' || verify.phase === 'collecting';

  /** Formats provider availability and unsupported-operation information for the connector catalog. */
  const catalogStatus = (entry: ConnectorCatalogEntry) => entry.availability === 'available' || entry.availability === 'implemented'
    ? t('available') : entry.availability === 'requires_credentials' ? t('credentialsRequired') : entry.availability === 'planned' ? t('planned') : t('unavailable');

  return <section className={embedded ? 'source-editor flex flex-col gap-4' : 'source-editor flex flex-col gap-4 rounded-lg border border-border bg-card p-5'} aria-label={sourceId ? t('editTitle') : t('setupTitle')}>
    {(!sourceId || embedded) && <div hidden={embedded && step !== 'provider'} className="flex flex-col gap-4">
      {!embedded && <header className="section-heading"><div><h2>{t('setupTitle')}</h2><p className="text-muted-foreground">{t('stepConnect')}</p></div></header>}
      {catalogQuery.isPending && <p className="text-muted-foreground">{t('loading')}</p>}
      {catalogQuery.isError && <p className="error" role="alert">{t('loadFailed')} <Button variant="outline" onClick={() => catalogQuery.refetch()}>{t('retry')}</Button></p>}
      {catalog.length > 0 && groupCatalog(catalog.filter((entry) => entry.kind !== 'manual_import' && entry.kind !== 'group')).map(([group, entries]) => <SettingsSection key={group} title={t(PROVIDER_GROUP_KEY[group])}>
        <div className="source-provider-grid !m-0" role="group" aria-label={t(PROVIDER_GROUP_KEY[group])}>
        {entries.map((entry) => {
          const localProvider = entry.provider_id;
          const supported = entry.kind !== 'manual_import' && Boolean(entry.source_type && entry.code_available) && ['available', 'implemented', 'requires_credentials'].includes(entry.availability) && isProvider(localProvider);
          const added = addedBuiltinIds.has(localProvider);
          return <Button key={entry.provider_id} type="button" variant="outline" className={`source-provider h-auto min-w-0 flex-col items-start justify-start overflow-hidden whitespace-normal break-words text-left font-normal${provider === localProvider ? ' is-selected' : ''}`} disabled={!supported || locked || added || Boolean(sourceId)} onClick={() => { if (!supported || !isProvider(localProvider)) return; selectProvider(localProvider, entry); }}>
            <strong>{providerKey(entry.provider_id) === 'provider' ? entry.label : t(providerKey(entry.provider_id))}</strong>
            <span>{catalogStatus(entry)} · {entry.code_available ? t('codeAvailable') : t('codeUnavailable')}</span>
            {entry.requires_service === 'browser' && <small>{t('browserServiceRequired')}</small>}
            {added && <Badge variant="secondary">{t('builtinAdded')}</Badge>}
            {!supported && <small>{t('providerUnavailableReason')}</small>}
          </Button>;
        })}
        </div>
      </SettingsSection>)}
      <SettingsSection title={t('panelIdentity')} description={t('panelIdentityHint')}>
        <div className="form source-editor-form">
          <div className="field"><Label htmlFor="new-source-name">{t('sourceName')}</Label><Input id="new-source-name" maxLength={200} disabled={locked || Boolean(sourceId)} value={name} onChange={(event) => { setName(event.target.value); markDraftChanged(); }} /></div>
          {!sourceId && error && <p className="error" role="alert">{t(error as 'conflict' | 'configurationInvalid' | 'serviceUnavailable' | 'sourceNameRequired' | 'actionFailed' | 'builtinAlreadyAdded')}</p>}
          {!embedded && <div className="form-actions"><Button disabled={locked || !providerOk} onClick={() => void createSource()}>{busyAction === 'create' ? t('saving') : t('createAndContinue')}</Button><Button variant="outline" onClick={closeEditor}>{t('cancel')}</Button></div>}
        </div>
      </SettingsSection>
      {!source && <SettingsSection title={t('aiOptionsTitle')}><SourceAiToggles idPrefix="new-source" value={aiOptions} onChange={(next) => void changeAiOptions(next)} disabled={locked || Boolean(sourceId)} /></SettingsSection>}
    </div>}
    {sourceId && <>
      {!embedded && <header className="section-heading">
        <div>
          <h2>{name || source?.name || t('editTitle')}</h2>
          <p className="text-muted-foreground"><span className="badge">{sourceStatus === 'paused' ? t('statusPaused') : activationLabel}</span> {revision > 0 ? t('editorRevision', { revision }) : t('editorRevisionNew')}{(dirty || secret) ? ` · ${t('editorUnsaved')}` : ''}</p>
        </div>
        <div className="form-actions">
          {sourceStatus === 'active' && visibleActivationState === 'active' && !dirty && <Button variant="outline" disabled={locked} onClick={collectNow}>{busyAction === 'collect' ? t('collecting') : t('collectNow')}</Button>}
          {sourceStatus === 'paused' && provider !== 'mcp' && <Button variant="outline" disabled={locked} onClick={resumeSource}>{busyAction === 'resume' ? t('resuming') : t('resume')}</Button>}
          {source && <EditorSourceActions source={{ ...source, status: sourceStatus }} activation={activationQuery.data} disabled={locked || dirty || Boolean(secret)} onChanged={onChanged} onPurgeStarted={onPurgeStarted} onClose={onClose} onBusyChange={setActionsBusy} />}
          <Button variant="outline" onClick={closeEditor}>{t('close')}</Button>
        </div>
      </header>}
      {source && showStep('connect') && <SettingsSection title={t('aiOptionsTitle')}><SourceAiToggles idPrefix="edit-source" value={aiOptions} onChange={(next) => void changeAiOptions(next)} disabled={locked} />{aiOptionsError && <p className="error" role="alert">{t('aiOptionsSaveFailed')}</p>}</SettingsSection>}
      {configurationQuery.isPending && <p className="text-muted-foreground">{t('loading')}</p>}
      {configurationQuery.isError && <div className="error" role="alert">{t(errorText(configurationQuery.error) as 'conflict' | 'configurationInvalid' | 'serviceUnavailable' | 'sourceNameRequired' | 'actionFailed')} <Button variant="outline" onClick={() => configurationQuery.refetch()}>{t('retry')}</Button></div>}
      {configurationQuery.isSuccess && <>
        {provider === 'mcp' ? <div hidden={!showStep('connect')}><McpCollectionEditor sourceId={sourceId} sourceStatus={sourceStatus} onDraftChange={setDirty} onChanged={() => { void queryClient.invalidateQueries({ queryKey: connectorKeys.configuration(sourceId) }); void queryClient.invalidateQueries({ queryKey: connectorKeys.activation(sourceId) }); onChanged(); }} /></div> : <>
        {showStep('verify') && revision > 0 && (visibleActivationState === 'saved_not_active' || visibleActivationState === 'reconciliation_required' || Boolean(visibleActivationError)) && <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive p-4" role="status">
          <p><strong>{t('activationIssueTitle')}</strong> {t('savedInactive')}{visibleActivationError ? ` ${t('activationError')}: ${visibleActivationError}` : ''}</p>
          {!dirty && <Button variant="outline" disabled={locked || termsBlocked} onClick={retryActivation}>{busyAction === 'activate' ? t('enabling') : t('retryActivation')}</Button>}
        </div>}
        {!embedded && <ol aria-label={t('stepperLabel')} className="m-0 grid list-none grid-cols-3 gap-3 p-0 max-[720px]:grid-cols-1">
          {steps.map((step) => <li key={step.id}>
            <a href={`#source-step-${step.id}`} aria-current={currentStep === step.id ? 'step' : undefined} className={`flex min-h-11 flex-col rounded-lg border p-3 no-underline ${currentStep === step.id ? 'border-primary' : 'border-border'}`}>
              <strong>{step.title}</strong><span className="text-muted-foreground text-sm">{step.status}</span>
            </a>
          </li>)}
        </ol>}
        <fieldset className="source-editor-fields" disabled={locked}>
        <div className="form source-editor-form">
          <div id="source-step-connect" hidden={!showStep('connect')} className="scroll-mt-4"><SettingsSection title={t('panelCredentials')} description={t('panelCredentialsHint')}>
            {['google_mail', 'google_calendar', 'google_drive'].includes(provider) && <div className="grid gap-3">
              <p className="text-muted-foreground">{googleStatusQuery.data?.state === 'ready' && providerCredentialConfigured ? t('googleConnected') : t('googleReadOnlyConsent')}</p>
              {googleStatusQuery.data?.error_code && <p className="error" role="alert">{t('googleGrantNeedsAttention')}</p>}
              <div className="flex flex-wrap gap-2">
                <Button type="button" variant="outline" disabled={locked || !sourceId || dirty || revision < 1} onClick={() => void connectGoogle()}>{t('googleConnect')}</Button>
                {googleStatusQuery.data?.state === 'ready' && <Button type="button" variant="outline" disabled={locked || dirty} onClick={() => void disconnectGoogle()}>{t('googleDisconnect')}</Button>}
              </div>
            </div>}
            {provider === 'reddit' && <div className="grid gap-3">
              <p className="text-muted-foreground">{redditStatusQuery.data?.state === 'ready' ? t('redditConnected') : t('redditReadOnlyConsent')}</p>
              {redditStatusQuery.data?.error_code && <p className="error" role="alert">{t('redditGrantNeedsAttention')}</p>}
              <div className="flex flex-wrap gap-2">
                <Button type="button" variant="outline" disabled={locked || !sourceId || dirty || revision < 1} onClick={() => void connectReddit()}>{t('redditConnect')}</Button>
                {redditStatusQuery.data?.state === 'ready' && <Button type="button" variant="outline" disabled={locked || dirty} onClick={() => void disconnectReddit()}>{t('redditDisconnect')}</Button>}
              </div>
            </div>}
            {currentEntry?.auth_methods.includes('http_header') && <div className="field">
              <Label htmlFor="source-local-bearer-token">{t(provider === 'notion' ? 'notionIntegrationToken' : provider === 'slack' ? 'slackBotToken' : 'localBearerToken')}</Label>
              <PasswordInput id="source-local-bearer-token" autoComplete="new-password" maxLength={512} required={!providerCredentialConfigured} value={secret} onChange={(event) => { setSecret(event.target.value); dirtyRef.current = true; setDirty(true); draftVersion.current += 1; setValidation(null); setNotice(''); }} />
              <small className="text-muted-foreground">{providerCredentialConfigured ? t('credentialSavedWriteOnly') : t('secretHelp')}</small>
            </div>}
            {!['telegram', 'alpha_vantage', 'rest', 'google_mail', 'google_calendar', 'google_drive', 'reddit'].includes(provider) && !usesProviderCredential(currentEntry) && currentEntry && (currentEntry.auth_methods.every((method) => method === 'none')
              ? <p className="text-muted-foreground">{t('connectNoCredential')}</p>
              : currentEntry.auth_methods.includes('oauth2') && <p className="text-muted-foreground">{t('connectOAuth')}</p>)}
            {provider === 'telegram' && <>
              <div className="field"><Label htmlFor="source-telegram-secret-action">{t('telegramCredentialAction')}</Label><Select value={telegramSecretAction} onValueChange={(value) => { setTelegramSecretAction(value as 'keep' | 'replace'); setSecret(''); dirtyRef.current = dirty; draftVersion.current += 1; setValidation(null); setNotice(''); }}><SelectTrigger id="source-telegram-secret-action"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="keep">{t('keepCredential')}</SelectItem><SelectItem value="replace">{t('replaceCredential')}</SelectItem></SelectContent></Select></div>
              {telegramSecretAction === 'replace' && <div className="field"><Label htmlFor="source-telegram-token">{t('telegramBotToken')}</Label><PasswordInput id="source-telegram-token" autoComplete="new-password" maxLength={512} required value={secret} onChange={(event) => { setSecret(event.target.value); dirtyRef.current = true; setDirty(true); draftVersion.current += 1; setValidation(null); setNotice(''); }} /><small className="text-muted-foreground">{t('secretHelp')}</small></div>}
            </>}
            {(provider === 'alpha_vantage' || (usesApiKey(currentEntry) && (backendChoice ?? 'native') === 'native')) && <div className="field"><Label htmlFor="source-provider-key">{t(provider === 'coingecko' ? 'coingeckoKey' : provider === 'alpha_vantage' ? 'alphaVantageKey' : 'providerKey')}</Label><PasswordInput id="source-provider-key" autoComplete="new-password" maxLength={256} required={!providerCredentialConfigured} value={secret} onChange={(event) => { setSecret(event.target.value); dirtyRef.current = true; setDirty(true); draftVersion.current += 1; setValidation(null); setNotice(''); }} /><small className="text-muted-foreground">{providerCredentialConfigured ? t('credentialSavedWriteOnly') : t('secretHelp')}</small>{usesApiKey(currentEntry) && keyNeeded(visibleActivationError) && <small className="error" role="alert">{t('providerKeyNeeded')}</small>}</div>}
          {provider === 'rest' && <>
            <div className="field"><Label htmlFor="source-auth-method">{t('auth')}</Label><Select value={authMethod} onValueChange={(value) => { setAuthMethod(value as typeof authMethod); markDraftChanged(); setValidation(null); }}><SelectTrigger id="source-auth-method"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">{t('noAuthentication')}</SelectItem><SelectItem value="http_header">{t('headerAuthentication')}</SelectItem></SelectContent></Select></div>
            {authMethod === 'http_header' && <>
              <div className="field"><Label htmlFor="source-header-name">{t('headerName')}</Label><Input id="source-header-name" maxLength={128} value={authHeaderName} onChange={(event) => { setAuthHeaderName(event.target.value); markDraftChanged(); setValidation(null); }} /></div>
              <div className="field"><Label htmlFor="source-provider-secret">{t('providerSecret')}</Label><PasswordInput id="source-provider-secret" autoComplete="new-password" value={secret} onChange={(event) => { setSecret(event.target.value); dirtyRef.current = dirty || Boolean(event.target.value); draftVersion.current += 1; setValidation(null); }} /><small className="text-muted-foreground">{t('secretHelp')}</small></div>
            </>}
          </>}
          </SettingsSection></div>
          <div id="source-step-choose" hidden={!showStep('choose')} className="scroll-mt-4"><SettingsSection title={t('panelScope')} description={t('panelScopeHint')}>
          {currentEntry && <ProviderGuide entry={currentEntry} source={source ? { ...source, status: sourceStatus } : null} onTest={sourceId ? validateDraft : undefined} testBusy={locked} testPassed={Boolean(validation)} />}
          {isPreset(currentEntry) && currentEntry && (sourceId
            ? builtinReview ? <p className="text-muted-foreground" role="status">{t('builtinUseToggle')}</p> : <ProviderTerms sourceId={sourceId} entry={currentEntry} csrfToken={csrfToken} disabled={locked} />
            : <p className="text-muted-foreground" role="status">{t('termsRequired')}</p>)}
          {['google_mail', 'google_calendar', 'google_drive'].includes(provider) ? <div className="grid gap-3">
            <p className="m-0 text-sm text-muted-foreground">{t(provider === 'google_mail' ? 'googleMailLabelsHelp' : provider === 'google_calendar' ? 'googleCalendarIdsHelp' : 'googleDriveIdsHelp')}</p>
            {googleStatusQuery.data?.state === 'ready' ? <>
              <p className="m-0 text-sm" role="status">{t('googleReconnectForScope')}</p>
              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" variant="outline" disabled={locked} onClick={() => void loadGoogleResources(provider === 'google_drive' ? googleResourceParent : undefined)}>{busyAction === 'google-discovery' ? t('googleLoadingResources') : t('googleLoadResources')}</Button>
                {provider === 'google_drive' && googleResourcePath.length > 0 && <Button type="button" variant="outline" disabled={locked} onClick={() => {
                  const path = googleResourcePath.slice(0, -1);
                  void loadGoogleResources(path.at(-1)?.id, path);
                }}>{t('googleDriveBack')}</Button>}
                {provider === 'google_drive' && <span className="text-sm text-muted-foreground">{t('googleDriveLocation', { path: googleResourcePath.map((item) => item.name).join(' / ') || t('googleDriveRoot') })}</span>}
              </div>
              <p className="m-0 text-sm text-muted-foreground">{t('googleResourceCount', {
                selected: provider === 'google_mail' ? configuration.google_mail_label_ids?.length ?? 0 : provider === 'google_calendar' ? configuration.google_calendar_ids?.length ?? 0 : configuration.google_drive_ids?.length ?? 0,
                maximum: provider === 'google_mail' ? 10 : provider === 'google_calendar' ? 25 : 100,
              })}</p>
              {googleDiscoveryError && <p className="error" role="alert">{t(googleDiscoveryError === 'limit' ? 'googleSelectionLimit' : 'googleDiscoveryFailed')}</p>}
              {googleResourcesTruncated && <p className="text-muted-foreground" role="status">{t('googleResourcesTruncated')}</p>}
              {googleResources.length > 0 && <div className="grid gap-2" role="group" aria-label={t('googleResourceList')}>
                {googleResources.map((resource) => {
                  const selectedIds = provider === 'google_mail' ? configuration.google_mail_label_ids ?? []
                    : provider === 'google_calendar' ? configuration.google_calendar_ids ?? [] : configuration.google_drive_ids ?? [];
                  const checkboxId = `google-resource-${resource.id}`;
                  return <div key={resource.id} className="flex min-h-11 items-center justify-between gap-3 rounded-md border border-border p-2">
                    <CheckboxField>
                      <Checkbox id={checkboxId} disabled={locked} checked={selectedIds.includes(resource.id)} onCheckedChange={(checked) => toggleGoogleResource(resource.id, checked === true)} />
                      <Label htmlFor={checkboxId}>{resource.name}{resource.mime_type ? <span className="ml-2 text-xs text-muted-foreground">{resource.kind === 'folder' ? t('googleDriveFolder') : resource.mime_type}</span> : null}</Label>
                    </CheckboxField>
                    {resource.kind === 'folder' && <Button type="button" variant="outline" disabled={locked} onClick={() => void loadGoogleResources(resource.id, [...googleResourcePath, resource])}>{t('googleOpenFolder')}</Button>}
                  </div>;
                })}
              </div>}
            </> : <p className="m-0 text-sm text-muted-foreground">{t('googleConnectBeforeDiscovery')}</p>}
          </div>
          : provider === 'health' ? <FieldGrid>
            <div className="field"><Label htmlFor="fhir-base-url">{t('fhirBaseUrl')}</Label><Input id="fhir-base-url" type="url" maxLength={512} value={configuration.fhir_base_url ?? ''} onChange={(event) => changeConfiguration('fhir_base_url', event.target.value)} /><small className="text-muted-foreground">{t('localEndpointHelp')}</small></div>
            <div className="field"><Label htmlFor="fhir-patient-id">{t('fhirPatientId')}</Label><Input id="fhir-patient-id" maxLength={128} value={configuration.fhir_patient_id ?? ''} onChange={(event) => changeConfiguration('fhir_patient_id', event.target.value)} /></div>
          </FieldGrid>
          : provider === 'home_assistant' ? <FieldGrid>
            <div className="field"><Label htmlFor="ha-base-url">{t('homeAssistantBaseUrl')}</Label><Input id="ha-base-url" type="url" maxLength={512} value={configuration.ha_base_url ?? ''} onChange={(event) => changeConfiguration('ha_base_url', event.target.value)} /><small className="text-muted-foreground">{t('localEndpointHelp')}</small></div>
            <div className="field"><Label htmlFor="ha-entity-ids">{t('homeAssistantEntityIds')}</Label><Input id="ha-entity-ids" value={(configuration.ha_entity_ids ?? []).join(', ')} onChange={(event) => changeConfiguration('ha_entity_ids', event.target.value.split(',').map((value) => value.trim()).filter(Boolean).slice(0, 500))} /><small className="text-muted-foreground">{t('homeAssistantEntityIdsHelp')}</small></div>
          </FieldGrid>
          : currentEntry && isPreset(currentEntry) && currentEntry.scope_fields.length === 0 ? <p className="text-muted-foreground">{t('fixedScope')}</p> : isNativeEntry(currentEntry)
            ? <ProviderScope key={`${sourceId}:${provider}:${revision}:${sourceGeneration}:${scopeResetEpoch}`} provider={provider as NativeProvider} configuration={configuration} disabled={locked} resetEpoch={scopeResetEpoch} onChange={changeConfiguration} />
            : provider === 'rss' ? <div className="field"><Label htmlFor="source-feed-url">{t('sourceUrl')}</Label><Input id="source-feed-url" type="url" value={configuration.feed_url ?? ''} onChange={(event) => changeConfiguration('feed_url', event.target.value)} /></div> : <div className="field"><Label htmlFor="source-config-url">{provider === 'rest' ? t('apiUrl') : t('pageUrl')}</Label><Input id="source-config-url" type="url" value={configuration.url ?? ''} onChange={(event) => changeConfiguration('url', event.target.value)} /></div>}
          {provider === 'rest' && <FieldGrid>
            <div className="field"><Label htmlFor="source-items-path">{t('itemsPath')}</Label><Input id="source-items-path" maxLength={256} value={configuration.items_path ?? ''} onChange={(event) => changeConfiguration('items_path', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-id-field">{t('idField')}</Label><Input id="source-id-field" maxLength={128} value={configuration.id_field ?? ''} onChange={(event) => changeConfiguration('id_field', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-title-field">{t('titleField')}</Label><Input id="source-title-field" maxLength={128} value={configuration.title_field ?? ''} onChange={(event) => changeConfiguration('title_field', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-content-field">{t('contentField')}</Label><Input id="source-content-field" maxLength={128} value={configuration.content_field ?? ''} onChange={(event) => changeConfiguration('content_field', event.target.value)} /></div>
            <div className="field"><Label htmlFor="source-updated-field">{t('updatedField')}</Label><Input id="source-updated-field" maxLength={128} value={configuration.updated_field ?? ''} onChange={(event) => changeConfiguration('updated_field', event.target.value)} /></div>
          </FieldGrid>}
          {/* Validation lives with the scope/URL it checks; running it earlier always fails for scoped and generic sources. */}
          <div className="form-actions"><Button variant="outline" disabled={locked} onClick={validateDraft}>{busyAction === 'validate' ? t('validating') : t('validateDraft')}</Button></div>
          {validation && <p className="status-panel" role="status">{t('validationPassed', { time: formatDate(validation.at, display.locale, display.timezone) })}{validation.verifiedBotId ? ` ${t('verifiedBot')}: ${validation.verifiedBotId}` : ''}{validation.scopeVerified ? ` · ${t('scopeVerified')}` : ''}</p>}
          </SettingsSection></div>
          <div id="source-step-collect" hidden={!showStep('collect')} className="scroll-mt-4"><SettingsSection title={t('panelSchedule')} description={t('panelScheduleHint')}>
          <FieldGrid>
          <div className="field"><Label htmlFor="source-schedule">{t('schedule')}</Label><Select value={String(configuration.schedule_interval_minutes)} onValueChange={(value) => changeConfiguration('schedule_interval_minutes', Number(value) as ConnectorConfig['schedule_interval_minutes'])}><SelectTrigger id="source-schedule"><SelectValue /></SelectTrigger><SelectContent>{(provider === 'alpha_vantage' ? [1440] : intervals).map((minutes) => <SelectItem key={minutes} value={String(minutes)}>{t('everyMinutes', { minutes })}</SelectItem>)}</SelectContent></Select></div>
          <div className="field"><Label htmlFor="source-timezone">{t('timezone')}</Label><Input id="source-timezone" value={configuration.timezone} onChange={(event) => changeConfiguration('timezone', event.target.value)} /></div>
          </FieldGrid>
            {isNativeEntry(currentEntry) && <>
            <div className="field"><Label htmlFor="source-history-mode">{t('historyMode')}</Label><Input id="source-history-mode" readOnly aria-readonly="true" value={t(provider === 'telegram' ? 'pendingUpdatesOnly' : 'returnedSnapshot')} /></div>
            </>}
          </SettingsSection></div>
          {showStep('collect') && (provider === 'web' || isNativeEntry(currentEntry) || workspace.isOwner) && <SettingsSection title={t('panelAdvanced')} description={t('panelAdvancedHint')}>
          {(provider === 'web' || isNativeEntry(currentEntry)) && <details className="rounded-lg border border-border p-4">
            <summary className="cursor-pointer font-semibold">{t('advancedTitle')}</summary>
            <div className="mt-3"><FieldGrid>
              {provider === 'web' && <>
            <div className="field"><Label htmlFor="source-max-pages">{t('maxPages')}</Label><Input id="source-max-pages" type="number" min={1} max={10} value={configuration.max_pages} onChange={(event) => changeConfiguration('max_pages', Number(event.target.value))} /></div>
            <div className="field"><Label htmlFor="source-max-depth">{t('maxDepth')}</Label><Input id="source-max-depth" type="number" min={0} max={2} value={configuration.max_depth} onChange={(event) => changeConfiguration('max_depth', Number(event.target.value))} /></div>
            <div className="field"><Label htmlFor="source-timeout">{t('timeout')}</Label><Input id="source-timeout" type="number" min={1} max={60} value={configuration.timeout_seconds} onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))} /></div>
            <CheckboxField><Checkbox id="source-js-render" checked={configuration.js_render} onCheckedChange={(checked) => changeConfiguration('js_render', checked === true)} /><Label htmlFor="source-js-render">{t('renderJavascript')}</Label></CheckboxField>
              </>}
              {isNativeEntry(currentEntry) && <>
            <div className="field"><Label htmlFor="source-native-timeout">{t('timeout')}</Label><Input id="source-native-timeout" type="number" min={1} max={30} value={configuration.timeout_seconds} onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))} /></div>
              </>}
            </FieldGrid></div>
          </details>}
        {showStep('collect') && workspace.isOwner && <details className="rounded-lg border border-border p-4">
          <summary className="cursor-pointer font-semibold">{t('backendAdvanced')}</summary>
          <div className="mt-3 grid gap-3">
            <div className="field"><Label htmlFor="source-backend">{t('backendLabel')}</Label>
              <Select value={backendChoice ?? 'native'} onValueChange={(value) => { setBackendChoice(value as ExecutionBackend); }}>
                <SelectTrigger id="source-backend"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="native">{t('backendNative')}</SelectItem><SelectItem value="n8n">{t('backendN8n')}</SelectItem></SelectContent>
              </Select></div>
            {(backendChoice ?? 'native') === 'n8n' && <p className="text-muted-foreground" role="note">{t('backendN8nPrereq')}</p>}
          </div>
        </details>}
          </SettingsSection>}
        </div>
        </fieldset>
        {showStep('choose') && historyDescription && <SettingsSection title={t('history')}><p className="m-0 text-sm">{historyDescription}</p>{isNativeEntry(currentEntry) && <p className="m-0 text-sm text-muted-foreground">{t('providerHistoryCaveat')}</p>}</SettingsSection>}
        {showStep('choose') && provider === 'open_meteo' && <SettingsSection title={t('providerTerms')}><p className="m-0 text-sm">{t('openMeteoTerms')}</p></SettingsSection>}
        {showStep('connect', 'verify') && provider === 'github' && <SettingsSection title={githubStatusQuery.data?.state === 'ready' ? t('githubConnected') : t('githubNotConnected')}>
          {githubStatusQuery.data?.expires_at && <p>{t('githubExpires')}: {formatDate(githubStatusQuery.data.expires_at, display.locale, display.timezone)}</p>}
          {githubStatusQuery.data && <p>{t('githubScanWindow', { days: githubStatusQuery.data.sync.history_days })}</p>}
          {(githubStatusQuery.data?.sync.unverified_hints ?? 0) > 0 && <p className="error">{t('githubEvidenceUnverified', { count: githubStatusQuery.data?.sync.unverified_hints ?? 0 })}</p>}
          {source?.provider === 'github' && source.status === 'paused' && <p className="error">{t('githubSourcePaused')}</p>}
          {(githubStatusQuery.data?.sync.reconcile_exhausted ?? 0) > 0 && <p className="error">{t('githubReconcileExhausted', { count: githubStatusQuery.data?.sync.reconcile_exhausted ?? 0 })}</p>}
          <p className="text-muted-foreground">{t('githubDeletionUnsupported')}</p>
          {githubStatusQuery.data?.sync.last_reset_at && <p className="text-muted-foreground">{t('githubGapRecorded')}: {formatDate(githubStatusQuery.data.sync.last_reset_at, display.locale, display.timezone)}</p>}
          {githubStatusQuery.data?.sync.resources.map((item) => <p key={item.resource} className={item.incomplete ? 'error' : 'muted'}>
            {t('githubResourceProgress', { resource: item.resource, phase: item.phase, page: item.page, sweep: item.sweep_revision })}
            {item.floor && item.upper ? ` · ${formatDate(item.floor, display.locale, display.timezone)} – ${formatDate(item.upper, display.locale, display.timezone)}` : ''}
            {item.completed_upper ? ` · ${formatDate(item.completed_upper, display.locale, display.timezone)}` : ''}
          </p>)}
          {githubStatusQuery.data?.sync.cursor_invalid && <p className="error">{t('githubCursorInvalid')}</p>}
          {(githubStatusQuery.data?.state === 'reconciliation_required' || githubStatusQuery.data?.coordinator_state === 'reconciliation_required' || githubStatusQuery.data?.recovery_available) && <p className="error">{t('githubReconciliation')}</p>}
          {githubStatusQuery.data?.recovery_available && githubStatusQuery.data.operation_source_id && <p className="text-muted-foreground">{githubStatusQuery.data.operation_source_id}{githubStatusQuery.data.operation_source_generation ? ` · generation ${githubStatusQuery.data.operation_source_generation}` : ''}{githubStatusQuery.data.operation_configuration_revision !== null ? ` · revision ${githubStatusQuery.data.operation_configuration_revision}` : ''}</p>}
          {['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(githubStatusQuery.data?.coordinator_error_code ?? '') && <p className="error">{t('githubRevokeUnknown')}</p>}
          {githubStatusQuery.isError && <p className="error">{t('githubConnectionFailed')}</p>}
          {sourceId && <GitHubSummary sourceId={sourceId} />}
          <div className="form-actions">
            {provider === 'github' && githubStatusQuery.data?.sync.scope_sha256 && <Button variant="outline" disabled={locked || dirty || sourceStatus !== 'active' || revision < 1} onClick={resetGitHubHistory}>{busyAction === 'github-reset' ? t('saving') : t('githubResetHistory')}</Button>}
            {githubStatusQuery.data?.recovery_available && ['provider_revoke_outcome_unknown', 'provider_revoke_pending'].includes(githubStatusQuery.data.coordinator_error_code ?? '')
              ? <Button variant="outline" disabled={locked || dirty} onClick={reviewGitHubDisconnect}>{busyAction === 'github-peers' ? t('loading') : t('githubRetryRevoke')}</Button>
              : githubStatusQuery.data?.recovery_available && ['authorization', 'refresh'].includes(githubStatusQuery.data.operation_kind ?? '')
              ? <Button variant="outline" disabled={locked || dirty || sourceStatus !== 'active' || revision < 1 || !githubStatusQuery.data.operation_source_id} onClick={reviewGitHubRecovery}>{t('githubReviewRecovery')}</Button>
              : githubStatusQuery.data?.coordinator_state !== 'idle' && githubStatusQuery.data?.coordinator_state !== undefined
              ? <p className="text-muted-foreground" role="status">{t('githubRecoveryWait')}</p>
              : githubStatusQuery.data?.state === 'ready'
              ? <>
                <Button variant="outline" disabled={locked || dirty} onClick={refreshGitHub}>{busyAction === 'github-refresh' ? t('saving') : t('githubRefresh')}</Button>
                <Button variant="outline" disabled={locked || dirty} onClick={reviewGitHubDisconnect}>{busyAction === 'github-peers' ? t('loading') : t('githubDisconnect')}</Button>
              </>
              : <Button variant="outline" disabled={locked || dirty || sourceStatus !== 'active' || revision < 1} onClick={connectGitHub}>{busyAction === 'github-connect' ? t('loading') : t('githubConnect')}</Button>}
          </div>
        </SettingsSection>}
        {showStep('choose') && currentEntry?.quota_limits && <SettingsSection title={t('quota')}><p className="m-0 text-sm">{Object.entries(currentEntry.quota_limits).map(([key, value]) => `${key}: ${value}`).join(' · ')}</p></SettingsSection>}
        {error && <p className="error" role="alert">{t(error as 'conflict' | 'configurationInvalid' | 'serviceUnavailable' | 'validationOutcomeUnknown' | 'sourceNameRequired' | 'actionFailed' | 'reloadFailed' | 'builtinAlreadyAdded' | 'redditConnectionFailed' | 'redditDisconnectFailed')}</p>}
        {notice && <p className="text-muted-foreground" role="status">{notice}</p>}
        {conflict && <SettingsSection title={t('conflict')}>
          <div role="group" aria-label={t('conflict')}><Button variant="outline" disabled={locked} onClick={reloadServerConfiguration}>{t('reloadDiscard')}</Button></div>
        </SettingsSection>}
        {showStep('choose', 'verify') && (termsBlocked || visibleActivationError === 'terms_not_accepted') && <p className="error" role="status">{t('termsActivationBlocked')}</p>}
        {!embedded && <SettingsActions>
          <Button variant="outline" onClick={closeEditor}>{t('cancel')}</Button>
          <Button variant="outline" disabled={locked || sourceStatus !== 'active'} onClick={() => saveConfiguration()}>{busyAction === 'save' ? t('saving') : t('save')}</Button>
          <Button disabled={locked || sourceStatus !== 'active' || termsBlocked} onClick={() => void saveAndEnable()}>{busyAction === 'activate' ? t('enabling') : t('saveEnable')}</Button>
        </SettingsActions>}
        <AlertDialog open={githubDisconnectOpen} onOpenChange={setGithubDisconnectOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t('githubDisconnect')}</AlertDialogTitle>
              <AlertDialogDescription>{t('githubPeerReview', { count: githubPeers.length })}</AlertDialogDescription>
            </AlertDialogHeader>
            <div><strong>{t('githubPeerList')}</strong><ul>{githubPeers.map((peer) => <li key={peer.source_id}>{peer.source_id}</li>)}</ul></div>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={locked}>{t('cancel')}</AlertDialogCancel>
              <Button disabled={locked} onClick={confirmGitHubDisconnect}>{busyAction === 'github-disconnect' ? t('saving') : t('githubDisconnect')}</Button>
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
              <AlertDialogCancel disabled={locked}>{t('cancel')}</AlertDialogCancel>
              <Button disabled={locked || !githubRecoveryReview} onClick={confirmGitHubRecovery}>{busyAction === 'github-recovery' ? t('saving') : t('githubAcknowledgeConnect')}</Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        {showStep('verify') && revision > 0 && <SettingsSection title={<>{t('activationState')}: {t(({ queued: 'stateQueued', provisioning: 'stateProvisioning', saved_not_active: 'stateSavedNotActive', reconciliation_required: 'stateReconciliationRequired', disabled: 'stateDisabled', active: 'active' } as Record<string, string>)[visibleActivationState] ?? 'stateUnknown')}</>}>
          {activationQuery.isError && <p className="error">{t('activationRefreshFailed')}</p>}
          {visibleActivationError && <p className="error">{t('activationError')}: {visibleActivationError}</p>}
          {visibleActivationState === 'saved_not_active' && <p className="text-muted-foreground">{t('savedInactive')}</p>}
          {visibleActivationError === 'credential_delete_pending' && <p className="text-muted-foreground" role="status">{t('credentialPending')}</p>}
          {providerCredentialConfigured && sourceStatus === 'paused' && visibleActivationError !== 'deactivation_pending' && visibleActivationError !== 'credential_delete_pending' && <Button variant="outline" disabled={locked || dirty || Boolean(secret)} onClick={removeCredential}>{busyAction === 'remove' ? t('saving') : t('removeCredential')}</Button>}
        </SettingsSection>}
        {!embedded && source && <SyncHistory source={{ ...source, status: sourceStatus }} intervalMinutes={configuration.schedule_interval_minutes} />}
        </>}
        {step === 'verify' && <SettingsSection title={t('verifyTitle')} description={t('verifyIntro')}>
          <div><Button disabled={locked || verifyBusy || sourceStatus !== 'active' || termsBlocked} onClick={() => void runVerify()}>{verify.phase === 'failed' ? t('verifyRetry') : t('verifyRun')}</Button></div>
          {verify.phase === 'saving' && <p role="status">{t('saving')}</p>}
          {verify.phase === 'activating' && <p role="status">{t('verifyWaitingActivation')}</p>}
          {verify.phase === 'collecting' && <p role="status">{t('verifyCollecting')}</p>}
          {verify.phase === 'succeeded' && <p role="status">{t('verifySucceeded')}</p>}
          {verify.phase === 'no_changes' && <p role="status">{t('verifyNoChanges')}</p>}
          {verify.phase === 'failed' && <p className="error" role="alert">{verify.code === 'local_only_source' ? t('verifyLocalOnly') : t('verifyFailed', { code: verify.code ?? 'unknown' })}</p>}
          {verify.runId && <p className="text-muted-foreground">{t('verifyRunId', { id: verify.runId })}</p>}
        </SettingsSection>}
      </>}
    </>}
  </section>;
}
