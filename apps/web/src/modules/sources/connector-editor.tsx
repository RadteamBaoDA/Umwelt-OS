'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useGuardedNavigation } from '@/core/guarded-navigation';
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
} from './api';

const intervals = [15, 30, 60, 360, 1440] as const;
const apiSourceTypes = { rss: 'rss', web: 'web', rest: 'api' } as const;
type Provider = keyof typeof apiSourceTypes;

/** Checks whether a provider identifier belongs to the supported provider set. */
function isProvider(value: string): value is Provider {
  return value === 'rss' || value === 'web' || value === 'rest';
}

/** Maps a provider identifier to its localized catalog key. */
function providerKey(providerId: string): string {
  return ({
    rss: 'providerRss', web: 'providerWeb', rest: 'providerRest', mcp: 'providerMcp', github: 'providerGithub',
    google_mail: 'providerGoogleMail', google_calendar: 'providerGoogleCalendar', google_drive: 'providerGoogleDrive',
  } as Record<string, string>)[providerId] ?? 'provider';
}

/** Builds the editable default configuration for the selected provider. */
function defaultConfiguration(provider: Provider): ConnectorConfig {
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
  if (error instanceof Error && error.message === 'source_name_required') return 'sourceNameRequired';
  return 'actionFailed';
}

/** Edits a connector draft, validates and saves revision-fenced configuration, and controls activation and credentials. */
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
  const [provider, setProvider] = useState<Provider>(source?.type === 'rss' ? 'rss' : source?.type === 'web' ? 'web' : 'rest');
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
  const [configuration, setConfiguration] = useState<ConnectorConfig>(() => defaultConfiguration(source?.type === 'rss' ? 'rss' : source?.type === 'web' ? 'web' : 'rest'));
  const [authMethod, setAuthMethod] = useState<'none' | 'http_header'>('none');
  const [authHeaderName, setAuthHeaderName] = useState('Authorization');
  const [secret, setSecret] = useState('');
  const [revision, setRevision] = useState(0);
  const revisionRef = useRef(revision);
  revisionRef.current = revision;
  const [activationState, setActivationState] = useState(source?.status === 'paused' ? 'disabled' : 'saved_not_active');
  const [sourceStatusOverride, setSourceStatusOverride] = useState<Source['status'] | null>(null);
  const [activationError, setActivationError] = useState<string | null>(null);
  const [providerCredentialConfigured, setProviderCredentialConfigured] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<{ at: string; generation: number; revision: number } | null>(null);
  const [busyAction, setBusyAction] = useState('');
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState('');
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
    busyRef.current = '';
    setBusyAction('');
    dirtyRef.current = false;
    setDirty(false);
    setSecret('');
    const baseline = serverConfigurationRef.current;
    const resetProvider = baseline
      ? baseline.source_type === 'rss' ? 'rss' : baseline.source_type === 'web' ? 'web' : 'rest'
      : source?.type === 'rss' ? 'rss' : source?.type === 'web' ? 'web' : source ? 'rest' : sourceIdRef.current ? providerRef.current : 'rss';
    setProvider(resetProvider);
    setName(source?.name ?? (sourceIdRef.current ? nameRef.current : ''));
    setConfiguration(baseline?.configuration ?? defaultConfiguration(resetProvider));
    setAuthMethod(baseline?.auth_method ?? 'none');
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
  const catalogQuery = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal) });
  const catalog = catalogQuery.data ?? [];
  const currentEntry = catalog.find((entry) => entry.provider_id === provider);
  const sourceStatus = sourceStatusOverride ?? source?.status ?? 'active';
  const visibleActivationState = activationQuery.isError ? 'unknown' : activationQuery.data?.state ?? activationState;
  const visibleActivationError = activationQuery.isError ? null : activationQuery.data?.error_code ?? activationError;

  /** Applies an accepted server configuration to the draft and its associated revision state. */
  const applyServerConfiguration = useCallback((value: ConnectorConfiguration) => {
    serverConfigurationRef.current = value;
    sourceGenerationRef.current = value.source_generation;
    setProvider(value.source_type === 'rss' ? 'rss' : value.source_type === 'web' ? 'web' : 'rest');
    setConfiguration(value.configuration);
    setAuthMethod(value.auth_method);
    setAuthHeaderName(value.auth_header_name ?? 'Authorization');
    setRevision(value.expected_revision);
    revisionRef.current = value.expected_revision;
    setActivationState(value.activation_state);
    setActivationError(value.activation_error_code);
    setProviderCredentialConfigured(value.provider_credential_configured);
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
    auth_method: authMethod,
    ...(authMethod === 'http_header' ? { auth_header_name: authHeaderName } : {}),
  }), [revision, configuration, authMethod, authHeaderName]);

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
    const created = await createConnectorSource(apiSourceTypes[provider], name.trim(), csrfToken, token.controller.signal);
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
    const draft = { ...settings, configuration: { ...configuration }, expected_source_generation: sourceGenerationRef.current };
    const submittedVersion = draftVersion.current;
    setError('');
    setNotice('');
    try {
      const result = await validateDraftConnector(id, draft, token.controller.signal);
      if (!requestIsCurrent(token) || draftVersion.current !== submittedVersion || result.source_generation !== draft.expected_source_generation || result.expected_revision !== draft.expected_revision) return;
      setValidation({ at: result.validated_at, generation: result.source_generation, revision: result.expected_revision });
    } catch (cause) {
      if (requestIsCurrent(token)) {
        setError(errorText(cause));
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
    const activationDraft = { authMethod, secret };
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
      const result = await activateConnector(id, nextRevision, activationDraft.authMethod === 'http_header' ? (activationDraft.secret || undefined) : undefined, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token)) return;
      setActivationState(result.state);
      setActivationError(result.error_code);
      setProviderCredentialConfigured(activationDraft.authMethod === 'http_header' || providerCredentialConfigured);
      const replacementAccepted = activationDraft.authMethod === 'http_header' && Boolean(activationDraft.secret);
      if (replacementAccepted) setSecret('');
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
      const result = await activateConnector(id, revision, authMethod === 'http_header' ? (requestedSecret || undefined) : undefined, csrfToken, token.controller.signal);
      if (!requestIsCurrent(token)) return;
      setActivationState(result.state);
      setActivationError(result.error_code);
      setProviderCredentialConfigured(authMethod === 'http_header' || providerCredentialConfigured);
      const replacementAccepted = authMethod === 'http_header' && Boolean(requestedSecret);
      if (replacementAccepted) setSecret('');
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
  const catalogStatus = (entry: ConnectorCatalogEntry) => entry.availability === 'available'
    ? t('available') : entry.availability === 'planned' ? t('planned') : t('unavailable');

  return <section className="sub-panel source-editor" aria-label={sourceId ? t('editTitle') : t('setupTitle')}>
    {!sourceId ? <>
      <header className="section-heading"><div><h2>{t('setupTitle')}</h2><p className="muted">{t('stepConnect')}</p></div></header>
      {catalogQuery.isPending && <p className="muted">{t('loading')}</p>}
      {catalogQuery.isError && <p className="error" role="alert">{t('loadFailed')} <Button className="secondary" onClick={() => catalogQuery.refetch()}>{t('retry')}</Button></p>}
      {catalog.length > 0 && <div className="source-provider-grid" role="group" aria-label={t('provider')}>
        {catalog.map((entry) => {
          const localProvider = entry.provider_id;
          const supported = entry.availability === 'available' && isProvider(localProvider);
          return <button key={entry.provider_id} type="button" className={`source-provider${provider === localProvider ? ' is-selected' : ''}`} disabled={!supported || busyAction !== ''} onClick={() => { if (!supported || !isProvider(localProvider)) return; setProvider(localProvider); setConfiguration(defaultConfiguration(localProvider)); markDraftChanged(); }}>
            <strong>{t(providerKey(entry.provider_id))}</strong><span>{catalogStatus(entry)}</span>
            {!supported && entry.unavailable_reason && <small>{entry.unavailable_reason}</small>}
          </button>;
        })}
      </div>}
      <div className="form source-editor-form">
        <div className="field"><Label htmlFor="new-source-name">{t('sourceName')}</Label><Input id="new-source-name" maxLength={200} disabled={busyAction !== ''} value={name} onChange={(event) => { setName(event.target.value); markDraftChanged(); }} /></div>
        <div className="form-actions"><Button disabled={busyAction !== '' || !name.trim() || !currentEntry || currentEntry.availability !== 'available'} onClick={createSource}>{busyAction === 'create' ? t('saving') : t('createAndContinue')}</Button><Button className="secondary" onClick={closeEditor}>{t('cancel')}</Button></div>
      </div>
    </> : <>
      <header className="section-heading"><div><h2>{name || source?.name || t('editTitle')}</h2><p className="muted">{t('stepChooseData')} · {t('stepCollect')}</p></div><Button className="secondary" onClick={closeEditor}>{t('close')}</Button></header>
      {configurationQuery.isPending && <p className="muted">{t('loading')}</p>}
      {configurationQuery.isError && <div className="error" role="alert">{t(errorText(configurationQuery.error) as 'conflict' | 'sourceNameRequired' | 'actionFailed')} <Button className="secondary" onClick={() => configurationQuery.refetch()}>{t('retry')}</Button></div>}
      {configurationQuery.isSuccess && <>
        <fieldset className="source-editor-fields" disabled={busyAction !== ''}>
        <div className="form source-editor-form">
          {provider === 'rss' ? <div className="field"><Label htmlFor="source-feed-url">{t('sourceUrl')}</Label><Input id="source-feed-url" type="url" value={configuration.feed_url ?? ''} onChange={(event) => changeConfiguration('feed_url', event.target.value)} /></div> : <div className="field"><Label htmlFor="source-config-url">{provider === 'rest' ? t('apiUrl') : t('pageUrl')}</Label><Input id="source-config-url" type="url" value={configuration.url ?? ''} onChange={(event) => changeConfiguration('url', event.target.value)} /></div>}
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
          <div className="field"><Label htmlFor="source-timezone">{t('timezone')}</Label><Input id="source-timezone" value={configuration.timezone} onChange={(event) => changeConfiguration('timezone', event.target.value)} /></div>
          <div className="field"><Label htmlFor="source-schedule">{t('schedule')}</Label><Select value={String(configuration.schedule_interval_minutes)} onValueChange={(value) => changeConfiguration('schedule_interval_minutes', Number(value) as ConnectorConfig['schedule_interval_minutes'])}><SelectTrigger id="source-schedule"><SelectValue /></SelectTrigger><SelectContent>{intervals.map((minutes) => <SelectItem key={minutes} value={String(minutes)}>{t('everyMinutes', { minutes })}</SelectItem>)}</SelectContent></Select></div>
          {provider === 'rest' && <>
            <div className="field"><Label htmlFor="source-auth-method">{t('auth')}</Label><Select value={authMethod} onValueChange={(value) => { setAuthMethod(value as typeof authMethod); markDraftChanged(); setValidation(null); }}><SelectTrigger id="source-auth-method"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">{t('noAuthentication')}</SelectItem><SelectItem value="http_header">{t('headerAuthentication')}</SelectItem></SelectContent></Select></div>
            {authMethod === 'http_header' && <>
              <div className="field"><Label htmlFor="source-header-name">{t('headerName')}</Label><Input id="source-header-name" maxLength={128} value={authHeaderName} onChange={(event) => { setAuthHeaderName(event.target.value); markDraftChanged(); setValidation(null); }} /></div>
              <div className="field"><Label htmlFor="source-provider-secret">{t('providerSecret')}</Label><Input id="source-provider-secret" type="password" autoComplete="new-password" value={secret} onChange={(event) => { setSecret(event.target.value); dirtyRef.current = dirty || Boolean(event.target.value); setValidation(null); }} /><small className="muted">{t('secretHelp')}</small></div>
            </>}
          </>}
        </div>
        </fieldset>
        {currentEntry?.history_description && <div className="source-capability"><strong>{t('history')}</strong><p>{currentEntry.history_description}</p></div>}
        {currentEntry?.quota_limits && <div className="source-capability"><strong>{t('quota')}</strong><p>{Object.entries(currentEntry.quota_limits).map(([key, value]) => `${key}: ${value}`).join(' · ')}</p></div>}
        {validation && <p className="status-panel" role="status">{t('validationPassed', { time: formatDate(validation.at, display.locale, display.timezone) })}</p>}
        {error && <p className="error" role="alert">{t(error as 'conflict' | 'sourceNameRequired' | 'actionFailed' | 'reloadFailed')}</p>}
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
  </section>;
}
