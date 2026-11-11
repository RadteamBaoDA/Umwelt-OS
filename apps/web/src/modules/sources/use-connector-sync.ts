'use client';

import { useQuery, type QueryClient } from '@tanstack/react-query';
import { useCallback, useEffect } from 'react';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import {
  connectorConfigurationIsAtLeast,
  connectorKeys,
  getMonotonicConnectorConfiguration,
  type ConnectorConfiguration,
  type Source,
} from './api';
import type { SourcesTranslator } from './connector-editor-context';
import { defaultConfiguration } from './connector-editor-model';
import type { ConnectorActivationState } from './use-connector-activation';
import { authMethodOf, providerOfConfiguration, type ConnectorDraft } from './use-connector-draft';
import type { ConnectorRequests } from './use-connector-requests';

/** Provider a discarded draft falls back to when no server configuration was ever accepted. */
function providerWithoutBaseline(source: Source | null | undefined, draft: ConnectorDraft): string {
  if (source?.type === 'rss' || source?.type === 'web') return source.type;
  if (source) return 'rest';
  return draft.sourceIdRef.current ? draft.providerRef.current : 'rss';
}

/**
 * Resets the draft to the last accepted server configuration (or to provider defaults when none exists) and clears
 * validation, conflict and error. Used when the owner discards the draft or leaves the editor.
 */
function resetDraftToBaseline(draft: ConnectorDraft, activation: ConnectorActivationState, source: Source | null | undefined) {
  const baseline = draft.serverConfigurationRef.current;
  const resetProvider = baseline ? providerOfConfiguration(baseline, false) : providerWithoutBaseline(source, draft);
  draft.setProvider(resetProvider);
  draft.setName(source?.name ?? (draft.sourceIdRef.current ? draft.nameRef.current : ''));
  draft.setConfiguration(baseline?.configuration ?? defaultConfiguration(resetProvider));
  draft.setAuthMethod(authMethodOf(baseline?.auth_method));
  draft.setTelegramSecretAction('keep');
  draft.setAuthHeaderName(baseline?.auth_header_name ?? 'Authorization');
  draft.setRevision(baseline?.expected_revision ?? 0);
  draft.revisionRef.current = baseline?.expected_revision ?? 0;
  if (baseline) {
    draft.sourceGenerationRef.current = baseline.source_generation;
    draft.setSourceGeneration(baseline.source_generation);
    activation.setActivationState(baseline.activation_state);
    activation.setActivationError(baseline.activation_error_code);
    draft.setProviderCredentialConfigured(baseline.provider_credential_configured);
    draft.loadedFor.current = baseline.source_id;
  } else {
    draft.setProviderCredentialConfigured(false);
    draft.loadedFor.current = '';
  }
  draft.setValidation(null);
  draft.setConflict(false);
  draft.setError('');
}

/** Applies an accepted server configuration to the draft and its associated revision state. */
function applyConfigurationToDraft(draft: ConnectorDraft, activation: ConnectorActivationState, value: ConnectorConfiguration) {
  draft.setScopeResetEpoch((current) => current + 1);
  draft.serverConfigurationRef.current = value;
  draft.sourceGenerationRef.current = value.source_generation;
  draft.setSourceGeneration(value.source_generation);
  draft.setProvider(providerOfConfiguration(value, true));
  draft.setConfiguration(value.configuration);
  draft.setAuthMethod(authMethodOf(value.auth_method));
  draft.setAuthHeaderName(value.auth_header_name ?? 'Authorization');
  draft.setRevision(value.expected_revision);
  draft.setBackendChoice(value.execution_backend ?? null);
  draft.revisionRef.current = value.expected_revision;
  activation.setActivationState(value.activation_state);
  activation.setActivationError(value.activation_error_code);
  draft.setProviderCredentialConfigured(value.provider_credential_configured);
  draft.setTelegramSecretAction('keep');
  draft.setDirty(false);
  draft.dirtyRef.current = Boolean(draft.secret);
  draft.setValidation(null);
  draft.setConflict(false);
  draft.setError('');
  draft.loadedFor.current = value.source_id;
}

/**
 * Folds a freshly fetched server configuration into the draft: ignored when older than the accepted baseline, applied when the
 * source changed or the baseline moved under a clean draft, and flagged as a conflict when the baseline moved under unsaved edits.
 */
function reconcileIncoming(
  draft: ConnectorDraft,
  incoming: ConnectorConfiguration | undefined,
  revision: number,
  apply: (value: ConnectorConfiguration) => void,
) {
  const baseline = draft.serverConfigurationRef.current;
  if (incoming && !connectorConfigurationIsAtLeast(incoming, [baseline ?? undefined])) return;
  if (incoming) draft.serverConfigurationRef.current = incoming;
  if (incoming && draft.loadedFor.current !== incoming.source_id && !draft.dirtyRef.current) {
    apply(incoming);
  } else if (incoming && draft.loadedFor.current === incoming.source_id) {
    const changedBaseline = incoming.expected_revision !== revision || incoming.source_generation !== draft.sourceGenerationRef.current;
    if (changedBaseline && draft.dirtyRef.current) {
      draft.setConflict(true);
      draft.setError('conflict');
    } else if (changedBaseline) {
      apply(incoming);
    }
  }
}

/** Arguments of the server-sync hook. */
type SyncArgs = {
  source?: Source | null;
  draft: ConnectorDraft;
  requests: ConnectorRequests;
  activation: ConnectorActivationState;
  queryClient: QueryClient;
  t: SourcesTranslator;
  registerTransitionGuard: (guard: (() => boolean) | null) => void;
};

/** Registers the wizard transition guard and the navigation leave guard for the editor's unsaved draft. */
function useEditorGuards(
  confirmTransition: () => boolean,
  confirmDiscard: () => boolean,
  acceptLeave: () => void,
  draft: ConnectorDraft,
  registerTransitionGuard: SyncArgs['registerTransitionGuard'],
) {
  const { registerLeaveGuard } = useGuardedNavigation();
  const { dirtyRef } = draft;
  useEffect(() => {
    registerTransitionGuard(confirmTransition);
    return () => registerTransitionGuard(null);
  }, [confirmTransition, registerTransitionGuard]);
  useEffect(
    () =>
      registerLeaveGuard({
        hasUnsavedChanges: () => dirtyRef.current,
        confirmDiscard,
        acceptLeave,
      }),
    [acceptLeave, confirmDiscard, registerLeaveGuard, dirtyRef],
  );
}

/**
 * Keeps the draft aligned with the server: the configuration query, the monotonic fence over accepted revisions, the leave and
 * transition guards, the authentication-ending reset and the unmount abort. Returns the helpers actions need to apply or fence
 * server state.
 */
export function useConnectorServerSync({ source, draft, requests, activation, queryClient, t, registerTransitionGuard }: SyncArgs) {
  const { sourceId, revision, secret, dirtyRef, serverConfigurationRef } = draft;
  const { dropInFlight, abortOnUnmount } = requests;
  const { setScopeResetEpoch } = draft;
  /** Asks before discarding the current unsaved draft. */
  const confirmDiscard = useCallback(() => !dirtyRef.current || window.confirm(t('draftLeave')), [t, dirtyRef]);
  /** Invalidates the current draft session and accepts leaving the editor. */
  const acceptLeave = useCallback(() => {
    dropInFlight();
    setScopeResetEpoch((current) => current + 1);
    dirtyRef.current = false;
    draft.setDirty(false);
    draft.setSecret('');
    resetDraftToBaseline(draft, activation, source);
    // The bundles are rebuilt each render but only stable setters and refs are read, so the first render's copies stay valid.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, dropInFlight, setScopeResetEpoch, dirtyRef]);
  /** Runs the registered navigation guard before replacing the selected source. */
  const confirmTransition = useCallback(() => {
    if (!confirmDiscard()) return false;
    acceptLeave();
    return true;
  }, [acceptLeave, confirmDiscard]);
  useEditorGuards(confirmTransition, confirmDiscard, acceptLeave, draft, registerTransitionGuard);
  /** Collects the local source and revision bounds used to reject older server configuration. */
  function configurationFences(id: string) {
    return [
      serverConfigurationRef.current ?? undefined,
      queryClient.getQueryData<ConnectorConfiguration>(connectorKeys.configuration(id)),
    ] as const;
  }
  const configurationQuery = useQuery({
    queryKey: connectorKeys.configuration(sourceId),
    queryFn: ({ signal }) => getMonotonicConnectorConfiguration(sourceId, signal, () => configurationFences(sourceId)),
    enabled: Boolean(sourceId),
  });
  const applyServerConfiguration = useCallback(
    (value: ConnectorConfiguration) => applyConfigurationToDraft(draft, activation, value),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [secret],
  );
  useEffect(() => {
    reconcileIncoming(draft, configurationQuery.data, revision, applyServerConfiguration);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configurationQuery.data, applyServerConfiguration, revision]);
  useAuthEndingReset(draft, acceptLeave);
  useEffect(() => abortOnUnmount, [abortOnUnmount]);
  return { configurationQuery, applyServerConfiguration, acceptLeave, confirmDiscard, confirmTransition, configurationFences };
}
export type ConnectorServerSync = ReturnType<typeof useConnectorServerSync>;

/** When authentication ends, discards the draft and clears source identity and configuration so nothing leaks into the next session. */
function useAuthEndingReset(draft: ConnectorDraft, acceptLeave: () => void) {
  const { setSourceId, setSourceGeneration, setConfiguration, setName, sourceGenerationRef, loadedFor } = draft;
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
    window.addEventListener('umwelt:auth-ending', endAuthSession);
    return () => window.removeEventListener('umwelt:auth-ending', endAuthSession);
  }, [acceptLeave, setSourceId, setSourceGeneration, setConfiguration, setName, sourceGenerationRef, loadedFor]);
}
