'use client';

import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  clampInterval,
  type ConnectorCatalogEntry,
  type ConnectorConfig,
  type ConnectorConfiguration,
  type ConnectorSettings,
  type ExecutionBackend,
  type Source,
} from './api';
import {
  defaultConfiguration,
  IDLE_VERIFY,
  isNativeEntry,
  isPreset,
  isProvider,
  requiresHeaderCredential,
  type Provider,
  type ScopeIssue,
  type VerifyState,
} from './connector-editor-model';
import type { SourceAiOptions } from './source-ai-toggles';

/** Result of the last successful draft validation, bound to the source generation and revision it checked. */
export type DraftValidation = {
  at: string;
  generation: number;
  revision: number;
  verifiedBotId?: string | null;
  scopeVerified?: boolean | null;
};
/** Authentication modes the editor can hold for a draft. */
export type DraftAuthMethod = 'none' | 'http_header' | 'telegram_bot_token';

/** Provider the editor starts on: the source's registered provider, else its generic source type, else REST. */
export function initialProviderOf(source: Source | null | undefined): Provider {
  if (source?.provider && isProvider(source.provider)) return source.provider;
  if (source?.type === 'mcp' || source?.type === 'rss' || source?.type === 'web') return source.type;
  return 'rest';
}

/** Provider implied by a server configuration snapshot (registered provider, else generic source type, else REST; MCP optional). */
export function providerOfConfiguration(value: ConnectorConfiguration, allowMcp: boolean): Provider {
  if (value.provider && isProvider(value.provider)) return value.provider;
  if (allowMcp && value.source_type === 'mcp') return 'mcp';
  if (value.source_type === 'rss' || value.source_type === 'web') return value.source_type;
  return 'rest';
}

/** Maps the server auth method onto the editor's three supported modes. */
export function authMethodOf(value: string | null | undefined): DraftAuthMethod {
  return value === 'http_header' || value === 'telegram_bot_token' ? value : 'none';
}

/**
 * Editable draft fields of one connector: provider, name, scope configuration, authentication, write-only secret, revision,
 * execution backend, and the dirty/validation flags that track whether the draft still matches what was validated.
 */
function useDraftFields(source: Source | null | undefined) {
  const initialProvider = initialProviderOf(source);
  const [provider, setProvider] = useState<Provider>(initialProvider);
  const [name, setName] = useState(source?.name ?? '');
  const [aiOptions] = useState<SourceAiOptions>({
    graph_enabled: source?.graph_enabled ?? false,
    rag_index_enabled: source?.rag_index_enabled ?? false,
  });
  const [sourceId, setSourceId] = useState(source?.id ?? '');
  const [sourceGeneration, setSourceGeneration] = useState(source?.generation ?? 0);
  const [draftConfiguration, setConfiguration] = useState<ConnectorConfig>(() => defaultConfiguration(initialProvider));
  const [scopeResetEpoch, setScopeResetEpoch] = useState(0);
  const [authMethod, setAuthMethod] = useState<DraftAuthMethod>(source?.provider === 'telegram' ? 'telegram_bot_token' : 'none');
  const [authHeaderName, setAuthHeaderName] = useState('Authorization');
  const [secret, setSecret] = useState('');
  const [telegramSecretAction, setTelegramSecretAction] = useState<'keep' | 'replace'>('keep');
  const [revision, setRevision] = useState(0);
  const [backendChoice, setBackendChoice] = useState<ExecutionBackend | null>(null);
  const [providerCredentialConfigured, setProviderCredentialConfigured] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<DraftValidation | null>(null);
  return {
    provider,
    setProvider,
    name,
    setName,
    aiOptions,
    sourceId,
    setSourceId,
    sourceGeneration,
    setSourceGeneration,
    draftConfiguration,
    setConfiguration,
    scopeResetEpoch,
    setScopeResetEpoch,
    authMethod,
    setAuthMethod,
    authHeaderName,
    setAuthHeaderName,
    secret,
    setSecret,
    telegramSecretAction,
    setTelegramSecretAction,
    revision,
    setRevision,
    backendChoice,
    setBackendChoice,
    providerCredentialConfigured,
    setProviderCredentialConfigured,
    dirty,
    setDirty,
    validation,
    setValidation,
  };
}

/** User-visible outcome of the last action: error code, server detail and per-field issues, conflict flag, notice, and the test-fetch (verify) phase. */
function useDraftStatus() {
  const [issues, setIssues] = useState<ScopeIssue[]>([]);
  const [error, setError] = useState('');
  const [errorDetail, setErrorDetail] = useState('');
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState('');
  const [verify, setVerify] = useState<VerifyState>(IDLE_VERIFY);
  return { issues, setIssues, error, setError, errorDetail, setErrorDetail, conflict, setConflict, notice, setNotice, verify, setVerify };
}

/**
 * Mutable mirrors of the draft that async actions compare against after an await (latest provider/name/source/revision/dirty,
 * the accepted server configuration, which source the draft was loaded for) plus the draft version that fences stale saves.
 */
function useDraftRefs(source: Source | null | undefined, fields: ReturnType<typeof useDraftFields>) {
  const { provider, name, sourceId, revision, dirty, secret } = fields;
  const providerRef = useRef(provider);
  const nameRef = useRef(name);
  const sourceIdRef = useRef(sourceId);
  const sourceGenerationRef = useRef(source?.generation ?? 0);
  const revisionRef = useRef(revision);
  const draftVersion = useRef(0);
  const loadedFor = useRef('');
  const serverConfigurationRef = useRef<ConnectorConfiguration | null>(null);
  const dirtyRef = useRef(dirty);
  /** Stable server code of the last rejected save/enable (e.g. local_only_source), surfaced by the verify step instead of a generic save failure. */
  const saveFailCode = useRef<string | null>(null);
  useLayoutEffect(() => {
    providerRef.current = provider;
    nameRef.current = name;
    sourceIdRef.current = sourceId;
    revisionRef.current = revision;
    dirtyRef.current = dirty || Boolean(secret);
  });
  /** Records the draft-dirty flag in the leave-guard ref (written from field handlers; kept in the hook that owns the ref). */
  const setDirtyRef = useCallback((value: boolean) => {
    dirtyRef.current = value;
  }, []);
  /** Invalidates in-flight saves/validations of an older draft after a field edit. */
  const bumpDraftVersion = useCallback(() => {
    draftVersion.current += 1;
  }, []);
  return {
    providerRef,
    nameRef,
    sourceIdRef,
    sourceGenerationRef,
    revisionRef,
    draftVersion,
    loadedFor,
    serverConfigurationRef,
    dirtyRef,
    saveFailCode,
    setDirtyRef,
    bumpDraftVersion,
  };
}

/** Draft state of the connector editor: fields, outcome status and the async-fence refs, kept in one bundle that actions receive. */
export function useConnectorDraft(source: Source | null | undefined) {
  const fields = useDraftFields(source);
  const status = useDraftStatus();
  const refs = useDraftRefs(source, fields);
  return { ...fields, ...status, ...refs };
}
export type ConnectorDraft = ReturnType<typeof useConnectorDraft>;

/** Advances the draft version and marks the current configuration dirty; a finished test fetch no longer describes the edited draft. */
export function markDraftChanged(draft: ConnectorDraft) {
  draft.draftVersion.current += 1;
  draft.dirtyRef.current = true;
  draft.setDirty(true);
  draft.setVerify((current) => (current.phase === 'idle' ? current : IDLE_VERIFY));
}

/** Updates one typed configuration field and marks the editor draft changed. */
export function changeConfiguration<K extends keyof ConnectorConfig>(draft: ConnectorDraft, key: K, value: ConnectorConfig[K]) {
  draft.setConfiguration((current) => ({ ...current, [key]: value }));
  markDraftChanged(draft);
  draft.setValidation(null);
  draft.setNotice('');
}

/**
 * Switches the add-flow provider card and resets every provider-specific draft field (configuration, auth mode,
 * header name, secret, validation) so nothing from the previously selected card leaks into the new source.
 * Only valid before the source exists; the source name is replaced only when it is empty or still the previous preset label.
 */
export function selectProvider(
  draft: ConnectorDraft,
  next: Provider,
  entry: ConnectorCatalogEntry,
  currentEntry: ConnectorCatalogEntry | undefined,
) {
  draft.setProvider(next);
  draft.setConfiguration(defaultConfiguration(next, entry));
  draft.setAuthMethod('none');
  draft.setAuthHeaderName('Authorization');
  draft.setSecret('');
  // A new Telegram source has no stored token to keep, so it must start in replace mode.
  draft.setTelegramSecretAction(next === 'telegram' ? 'replace' : 'keep');
  draft.setBackendChoice(null);
  draft.setValidation(null);
  draft.setNotice('');
  draft.setError('');
  draft.setErrorDetail('');
  draft.setIssues([]);
  if (isPreset(entry) && (!draft.name.trim() || draft.name === currentEntry?.label)) draft.setName(entry.label);
  markDraftChanged(draft);
}

/**
 * Draft configuration with provider defaults layered underneath for an unsaved source (revision 0), the REST/Telegram/Mastodon
 * effective authentication mode, and the settings payload sent to the server. A source is created before its first save, so an
 * unsaved one reloads with an empty server configuration; defaults keep required scope (e.g. Google News site, locale and
 * history mode) from being silently missing while values the owner typed or cleared still win.
 */
export function useEffectiveDraft(draft: ConnectorDraft, currentEntry: ConnectorCatalogEntry | undefined) {
  const { revision, provider, draftConfiguration, secret, authMethod, authHeaderName } = draft;
  const configuration = useMemo<ConnectorConfig>(() => {
    if (!(revision === 0 && currentEntry)) return draftConfiguration;
    const merged = { ...defaultConfiguration(provider, currentEntry), ...draftConfiguration };
    // A default below the provider's hard minimum would be rejected on save; start at the floor instead.
    return { ...merged, schedule_interval_minutes: clampInterval(merged.schedule_interval_minutes, currentEntry) };
  }, [revision, currentEntry, provider, draftConfiguration]);
  // Registered providers never use REST header auth (their key goes through a dedicated credential slot), so a stale
  // 'http_header' choice from a previously selected REST card must not reach the backend's provider auth check.
  const effectiveAuth = effectiveAuthOf(provider, secret, authMethod, currentEntry);
  const settings = useMemo<ConnectorSettings>(
    () => ({
      expected_revision: revision,
      configuration,
      auth_method: effectiveAuth,
      ...(effectiveAuth === 'http_header' ? { auth_header_name: authHeaderName } : {}),
    }),
    [revision, configuration, effectiveAuth, authHeaderName],
  );
  return { configuration, effectiveAuth, settings };
}

/** Authentication mode the backend should see for the selected provider, ignoring stale choices from other cards. */
function effectiveAuthOf(
  provider: Provider,
  secret: string,
  authMethod: DraftAuthMethod,
  entry: ConnectorCatalogEntry | undefined,
): DraftAuthMethod {
  if (provider === 'telegram') return 'telegram_bot_token';
  if (provider === 'mastodon') return secret.trim() ? 'http_header' : 'none';
  if (requiresHeaderCredential(entry)) return 'http_header';
  return isNativeEntry(entry) ? 'none' : authMethod;
}
