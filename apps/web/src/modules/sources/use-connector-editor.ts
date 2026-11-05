'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import type { MutableRefObject } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import type { DataShape } from '@/core/data-shapes';
import { useDisplayPreferences } from '@/core/query-provider';
import { useWorkspace } from '@/core/workspace-context';
import type { ConnectorCatalogEntry, ConnectorConfig, Source } from './api';
import type { EditorContext } from './connector-editor-context';
import type { EditorActions, EditorSnapshot, EditorStep, Provider } from './connector-editor-model';
import {
  closeEditor,
  collectNow,
  createSource,
  reloadServerConfiguration,
  removeCredential,
  resumeSource,
  saveConfiguration,
  validateDraft,
} from './connector-persistence';
import { retryActivation, saveAndEnable, useConnectorActivation } from './use-connector-activation';
import { useConnectorCatalog } from './use-connector-catalog';
import { changeConfiguration, markDraftChanged, selectProvider, useConnectorDraft, useEffectiveDraft } from './use-connector-draft';
import { useConnectorRequests } from './use-connector-requests';
import { useConnectorServerSync } from './use-connector-sync';
import { runVerify, useConnectorWizard } from './use-connector-wizard';
import { useGitHubConnection } from './use-github-connection';
import { useGoogleConnection } from './use-google-connection';
import { useRedditConnection } from './use-reddit-connection';

export type ConnectorEditorProps = {
  source?: Source | null;
  /** Add grid keeps only catalog entries whose `data_shapes` intersect this list. */
  shapeFilter?: DataShape[];
  /** Clears the shape filter from the empty state. */
  onClearFilter?: () => void;
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
};

/**
 * Composes the connector editor: draft fields, request fence, activation status, server sync, catalog, the three OAuth
 * connections and the wizard gating, then binds the persistence and activation actions to the current render. Returns the view
 * model the section components render; ordering, fences and timings are those of the former single component body.
 */
export function useConnectorEditor(props: ConnectorEditorProps) {
  const { source, onClose, onChanged, onPurgeStarted, registerTransitionGuard, step, actionsRef, onSnapshot, shapeFilter, onClearFilter } =
    props;
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const display = useDisplayPreferences();
  const queryClient = useQueryClient();
  const workspace = useWorkspace();
  const draft = useConnectorDraft(source);
  const requests = useConnectorRequests(draft, display);
  const activation = useConnectorActivation(source, draft.sourceId, display);
  const sync = useConnectorServerSync({ source, draft, requests, activation, queryClient, t, registerTransitionGuard });
  const catalog = useConnectorCatalog(draft.provider, draft.sourceId, shapeFilter, t);
  const effective = useEffectiveDraft(draft, catalog.currentEntry);
  const ctx: EditorContext = {
    t,
    csrfToken,
    display,
    queryClient,
    source,
    onChanged,
    onClose,
    draft,
    requests,
    activation,
    sync,
    currentEntry: catalog.currentEntry,
    configuration: effective.configuration,
    settings: effective.settings,
  };
  const github = useGitHubConnection(ctx);
  const google = useGoogleConnection(ctx);
  const reddit = useRedditConnection(ctx);
  const wizard = useConnectorWizard(ctx, {
    redditReady: reddit.redditStatusQuery.data?.state === 'ready',
    googleReady: google.googleStatusQuery.data?.state === 'ready',
    connectGoogle: google.connectGoogle,
    step,
    actionsRef,
    onSnapshot,
    termsBlocked: catalog.termsBlocked,
  });
  return {
    ...draft,
    ...activation,
    ...catalog,
    ...github,
    ...google,
    ...reddit,
    ...wizard,
    csrfToken,
    display,
    queryClient,
    workspace,
    busyAction: requests.busyAction,
    setActionsBusy: requests.setActionsBusy,
    configuration: effective.configuration,
    configurationQuery: sync.configurationQuery,
    source,
    onClose,
    onChanged,
    onPurgeStarted,
    step,
    shapeFilter,
    onClearFilter,
    selectProvider: (next: Provider, entry: ConnectorCatalogEntry) => selectProvider(draft, next, entry, catalog.currentEntry),
    changeConfiguration: <K extends keyof ConnectorConfig>(key: K, value: ConnectorConfig[K]) => changeConfiguration(draft, key, value),
    markDraftChanged: () => markDraftChanged(draft),
    closeEditor: () => closeEditor(ctx),
    resumeSource: () => resumeSource(ctx),
    collectNow: () => collectNow(ctx),
    reloadServerConfiguration: () => reloadServerConfiguration(ctx),
    validateDraft: () => validateDraft(ctx),
    saveConfiguration: () => saveConfiguration(ctx),
    saveAndEnable: () => saveAndEnable(ctx),
    retryActivation: () => retryActivation(ctx),
    removeCredential: () => removeCredential(ctx),
    createSource: () => createSource(ctx),
    runVerify: () => runVerify(ctx),
  };
}
