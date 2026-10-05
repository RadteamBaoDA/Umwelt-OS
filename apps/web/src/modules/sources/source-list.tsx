'use client';

import { useInfiniteQuery, useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import {
  archiveSource,
  connectorKeys,
  ConnectorConfiguration,
  deactivateConnector,
  getConnectorActivation,
  getGitHubWebhookStatus,
  getMonotonicConnectorConfiguration,
  getOperation,
  listSources,
  purgeSource,
  Source,
  sourceKeys,
  triggerCollection,
  updateSourceStatus,
} from './api';
import { ConnectorEditor } from './connector-editor';
import { SourceForm } from './source-form';
import { SyncHistory } from './sync-history';

/** Formats an optional ISO timestamp with the supplied application locale and time zone, returning the supplied fallback when absent. */
function formatDate(value: string | null, locale: AppLocaleId, timezone: string, fallback: string): string {
  return value ? new Intl.DateTimeFormat(normalizeFormattingLocale(locale), {
    dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
  }).format(new Date(value)) : fallback;
}

/** Maps a source or run status to the matching translation message key. */
function statusKey(status: string): 'statusActive' | 'statusPaused' | 'statusArchived' {
  return status === 'paused' ? 'statusPaused' : status === 'archived' ? 'statusArchived' : 'statusActive';
}

/** Resolves registered provider IDs to translated names while preserving generic source labels. */
function providerLabel(provider: string | null, t: ReturnType<typeof useTranslations<'sources'>>) {
  if (!provider) return null;
  const keys: Record<string, string> = {
    youtube: 'providerYoutube', arxiv: 'providerArxiv', huggingface: 'providerHuggingface', github_releases: 'providerGithubReleases',
    telegram: 'providerTelegram', rss: 'providerRss', web: 'providerWeb', rest: 'providerRest',
  };
  const key = keys[provider];
  return key ? t(key as 'providerYoutube') : provider;
}

/** Formats a connector schedule interval as localized display text. */
function scheduleLabel(minutes: number, t: ReturnType<typeof useTranslations<'sources'>>) {
  if (minutes === 15 || minutes === 30) return t('everyMinutes', { minutes });
  if (minutes === 60) return t('everyHour');
  if (minutes === 360) return t('everyHours', { hours: 6 });
  if (minutes === 1440) return t('dailyAtMidnight');
  return t('scheduleUnknown');
}

/** Polls and renders progress for the supplied asynchronous source purge operation. */
function PurgeProgress({ operationId }: { operationId: string }) {
  const t = useTranslations('sources');
  const operation = useQuery({
    queryKey: ['operation', operationId],
    queryFn: () => getOperation(operationId),
    refetchInterval: (query) => ['succeeded', 'failed'].includes(query.state.data?.status ?? '') ? false : 1500,
  });
  if (operation.isPending) return <p className="muted">{t('loading')}</p>;
  if (operation.isError) return <p className="error" role="alert">{t('actionFailed')}</p>;
  return <p role="status">{operation.data.status}{operation.data.error_code ? ` · ${operation.data.error_code}` : ''}</p>;
}

/** Renders a source’s schedule, status, and actions, including archive or purge confirmation. */
function SourceEntry({
  source,
  schedule,
  timezone,
  onEdit,
  onChanged,
}: {
  source: Source;
  schedule: string;
  timezone: string | null;
  onEdit: (source: Source) => void;
  onChanged: () => void;
}) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [operationId, setOperationId] = useState('');
  const [collectResult, setCollectResult] = useState<{ status: string; hasRun: boolean } | null>(null);
  const connector = ['rss', 'web', 'api'].includes(source.type);
  const activation = useQuery({
    queryKey: connectorKeys.activation(source.id),
    queryFn: () => getConnectorActivation(source.id),
    enabled: connector,
  });
  const collect = useMutation({
    mutationFn: () => triggerCollection(source.id, csrfToken),
    onSuccess: (result) => {
      setCollectResult({ status: result.status, hasRun: Boolean(result.run_id) });
      void queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(source.id) });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.detail(source.id) });
    },
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  /** Changes the source between active and paused states and refreshes the displayed source data. */
  async function toggleStatus() {
    setBusy(true);
    setError(false);
    try {
      if (source.status === 'paused') {
        const resumed = await updateSourceStatus(source.id, 'active', csrfToken);
        await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
        onEdit(resumed);
      } else if (connector && activation.data && activation.data.desired_revision > 0) {
        await deactivateConnector(source.id, csrfToken);
        await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(source.id) });
        await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
        onChanged();
      } else {
        await updateSourceStatus(source.id, 'paused', csrfToken);
        await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
        onChanged();
      }
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  /** Archives the source and optionally starts its data purge, preserving the user-selected deletion behavior. */
  async function disconnect(deleteData: boolean) {
    if (!window.confirm(t(deleteData ? 'confirmDelete' : 'confirmKeep'))) return;
    setBusy(true);
    setError(false);
    try {
      if (deleteData) {
        const operation = await purgeSource(source.id, csrfToken);
        setOperationId(operation.operation_id);
      } else {
        await archiveSource(source.id, csrfToken);
      }
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      await queryClient.invalidateQueries({ queryKey: connectorKeys.activation(source.id) });
      onChanged();
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  return <li className="record-row source-record">
    <div className="record-content">
      <div className="source-record-heading"><div><strong>{source.name}</strong><p className="muted">{providerLabel(source.provider, t) ?? source.type} · {t(statusKey(source.status))}</p></div>
        {connector && <p className="source-activation-label">{t('activationState')}: {activation.isPending ? t('stateUnknown') : activation.isError ? t('stateUnknown') : t(({ queued: 'stateQueued', provisioning: 'stateProvisioning', active: 'active', saved_not_active: 'stateSavedNotActive', reconciliation_required: 'stateReconciliationRequired', disabled: 'stateDisabled' } as Record<string, string>)[activation.data.state] ?? 'stateUnknown')}{activation.data?.error_code ? ` · ${activation.data.error_code}` : ''}</p>}
      </div>
      {connector && <p className="muted">{t('schedule')}: {schedule}{timezone ? ` · ${timezone}` : ''}</p>}
      <div className="source-timestamps"><p>{t('collectedAt')}: {formatDate(source.collected_at, display.locale, display.timezone, t('never'))}</p><p>{t('indexedAt')}: {formatDate(source.indexed_at, display.locale, display.timezone, t('never'))}</p></div>
      <p className="muted">{t('collectionError')}: {source.collection_error_code ?? t('noError')} · {t('processingError')}: {source.processing_error_code ?? t('noError')}</p>
      <div className="form-actions source-actions">
        {connector && source.status !== 'archived' && <Button className="secondary" onClick={() => onEdit(source)}>{t('configure')}</Button>}
        {connector && source.status === 'active' && activation.data?.state === 'active' && <Button className="secondary" disabled={collect.isPending || busy} onClick={() => collect.mutate()}>{collect.isPending ? t('collecting') : t('collectNow')}</Button>}
        {source.status !== 'archived' && <Button className="secondary" disabled={busy} onClick={toggleStatus}>{busy ? t(source.status === 'paused' ? 'resuming' : 'pausing') : t(source.status === 'paused' ? 'resume' : 'pause')}</Button>}
        {source.status !== 'archived' && <Button className="secondary" disabled={busy} onClick={() => disconnect(false)}>{t('disconnectKeep')}</Button>}
        <Button className="secondary" disabled={busy} onClick={() => disconnect(true)}>{t('disconnectDelete')}</Button>
        {connector && <Button className="secondary" aria-expanded={historyOpen} onClick={() => setHistoryOpen((open) => !open)}>{t(historyOpen ? 'hideHistory' : 'showHistory')}</Button>}
      </div>
      {collectResult && <p className="muted" role="status">{t('collectResult', { status: collectResult.status })}{!collectResult.hasRun ? ` ${t('noRunId')}` : ''}</p>}
      {operationId && <PurgeProgress operationId={operationId} />}
      {historyOpen && connector && <SyncHistory source={source} />}
      {(error || collect.error || activation.isError) && <p className="error" role="alert">{t('actionFailed')}{activation.isError && ` · ${t('stateUnknown')}`}</p>}
    </div>
  </li>;
}

/** Loads sources and connector configuration, and coordinates selection changes with the active unsaved-draft guard. */
/** Loads sources and shows the owner's secret-free GitHub receiver and backlog status. */
export function SourceList() {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  const queryClient = useQueryClient();
  const { ensureSourcesDocument } = useGuardedNavigation();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Source | null | undefined>(undefined);
  const [editSession, setEditSession] = useState(0);
  const transitionGuard = useRef<(() => boolean) | null>(null);
  /** Coordinates editor changes and confirms leaving a dirty connector draft before changing selection. */
  const transition = useCallback((next: Source | null | undefined, manual = false) => {
    if (next !== undefined && !ensureSourcesDocument()) return;
    if (transitionGuard.current && !transitionGuard.current()) return;
    transitionGuard.current = null;
    setEditSession((value) => value + 1);
    setAdding(manual);
    setEditing(next);
  }, [ensureSourcesDocument]);
  const sources = useInfiniteQuery({
    queryKey: sourceKeys.list,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listSources(pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = sources.data?.pages.flatMap((page) => page.items) ?? [];
  const githubWebhook = useQuery({
    queryKey: ['github-webhook-status'],
    queryFn: ({ signal }) => getGitHubWebhookStatus(signal),
    refetchInterval: 30_000,
  });
  const connectors = items.filter((source) => ['rss', 'web', 'api'].includes(source.type));
  const configurations = useQueries({ queries: connectors.map((source) => ({
    queryKey: connectorKeys.configuration(source.id),
    queryFn: ({ signal }: { signal: AbortSignal }) => getMonotonicConnectorConfiguration(source.id, signal, () => [
      queryClient.getQueryData<ConnectorConfiguration>(connectorKeys.configuration(source.id)),
    ]),
  })) });
  const configurationBySourceId = new Map(connectors.map((source, index) => [source.id, configurations[index]]));
  /** Invalidates the source list query after a source mutation. */
  const refresh = () => queryClient.invalidateQueries({ queryKey: sourceKeys.all });

  return <div className="sources-list">
    <section className="sub-panel" aria-labelledby="github-webhook-heading">
      <h2 id="github-webhook-heading">{t('githubWebhookStatus')}</h2>
      {githubWebhook.isPending && <p className="muted">{t('loading')}</p>}
      {githubWebhook.isError && <p className="error" role="alert">{t('githubWebhookUnavailable')} <Button className="secondary" onClick={() => githubWebhook.refetch()}>{t('retry')}</Button></p>}
      {githubWebhook.data && <>
        <p role="status">{t(githubWebhook.data.receiver_configured ? 'githubWebhookReady' : 'githubWebhookNotConfigured')} · {t('githubWebhookRevision', { revision: githubWebhook.data.receiver_revision })}</p>
        <p className="muted">{t('githubWebhookBacklog', { pending: githubWebhook.data.pending_count, deliveries: githubWebhook.data.pending_deliveries, hints: githubWebhook.data.pending_hints, capacity: 100000 })}</p>
        <p className="muted">{t('githubWebhookAttention', { count: githubWebhook.data.needs_attention })}</p>
        {githubWebhook.data.oldest_pending_at && <p className="muted">{t('githubWebhookOldest', { time: formatDate(githubWebhook.data.oldest_pending_at, display.locale, display.timezone, t('never')) })}</p>}
      </>}
    </section>
    <div className="form-actions source-list-actions">
      <Button onClick={() => transition(null)}>{t('addConnector')}</Button>
      <Button className="secondary" onClick={() => transition(undefined, !adding)}>{t('addManual')}</Button>
    </div>
    {adding && <section className="sub-panel"><h2>{t('manualTitle')}</h2><SourceForm onSaved={() => { setAdding(false); void refresh(); }} /></section>}
    {editing !== undefined && <ConnectorEditor key={`${editing?.id ?? 'new'}:${editSession}`} source={editing} registerTransitionGuard={(guard) => { transitionGuard.current = guard; }} onChanged={() => { void refresh(); }} onClose={() => transition(undefined)} />}
    {sources.isPending && <div className="skeleton" aria-label={t('loading')} />}
    {sources.isError && <p className="error" role="alert">{t('loadFailed')} <Button className="secondary" onClick={() => sources.refetch()}>{t('retry')}</Button></p>}
    {sources.isSuccess && items.length === 0 && <p className="empty-state">{t('empty')}</p>}
    {items.length > 0 && <ul className="record-list">{items.map((source) => {
      const configuration = configurationBySourceId.get(source.id);
      return <SourceEntry key={source.id} source={source} schedule={configuration?.data ? scheduleLabel(configuration.data.configuration.schedule_interval_minutes, t) : t('scheduleUnknown')} timezone={configuration?.data?.configuration.timezone ?? null} onEdit={(value) => transition(value)} onChanged={() => { void refresh(); }} />;
    })}</ul>}
    {sources.hasNextPage && <Button className="secondary" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{t('loadMore')}</Button>}
  </div>;
}
