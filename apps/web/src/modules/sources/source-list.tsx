'use client';

import { useInfiniteQuery, useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import {
  connectorKeys,
  ConnectorConfiguration,
  getConnectorActivation,
  getGitHubWebhookStatus,
  getMonotonicConnectorConfiguration,
  getOperation,
  listSources,
  Source,
  sourceKeys,
  triggerCollection,
} from './api';
import { ConnectorEditor } from './connector-editor';
import { DisconnectDialog } from './disconnect-dialog';
import { useSourceActions } from './use-source-actions';
import { SourceForm } from './source-form';
import { matchesFilter, SourceFilter, SourceRowState, sourceRowState, SourcesTable } from './source-table';
import { SyncHistory } from './sync-history';

/** Formats an optional ISO timestamp with the supplied application locale and time zone, returning the supplied fallback when absent. */
function formatDate(value: string | null, locale: AppLocaleId, timezone: string, fallback: string): string {
  return value ? new Intl.DateTimeFormat(normalizeFormattingLocale(locale), {
    dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
  }).format(new Date(value)) : fallback;
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

/** Shows allowlisted owner cleanup progress, polling for at most 30 seconds with abortable GETs and manual refresh afterward. */
function PurgeProgress({ operationId, onSucceeded }: { operationId: string; onSucceeded: () => void }) {
  const t = useTranslations('sources');
  const pollingStartedAt = useRef<number | null>(null);
  const operation = useQuery({
    queryKey: ['operation', operationId],
    queryFn: ({ signal }) => {
      pollingStartedAt.current ??= Date.now();
      const controller = new AbortController();
      const timeoutId = window.setTimeout(() => controller.abort(), 10_000);
      const cancelQuery = () => controller.abort(signal.reason);
      if (signal.aborted) cancelQuery();
      else signal.addEventListener('abort', cancelQuery, { once: true });
      return getOperation(operationId, controller.signal).then((result) => {
        return result;
      }).finally(() => {
        window.clearTimeout(timeoutId);
        signal.removeEventListener('abort', cancelQuery);
      });
    },
    refetchInterval: (query) => {
      const current = query.state.data;
      const terminal = current?.status === 'succeeded' || current?.status === 'failed'
        || current?.documents_status === 'failed' || current?.documents_status === 'unavailable';
      return terminal || Date.now() - (pollingStartedAt.current ?? Date.now()) >= 30_000 ? false : 1500;
    },
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  const succeeded = operation.data?.status === 'succeeded';
  useEffect(() => { if (succeeded) onSucceeded(); }, [succeeded, onSucceeded]);
  const refreshButton = <Button type="button" className="secondary" disabled={operation.isFetching} onClick={() => void operation.refetch()}>
    {operation.isFetching ? t('cleanupRefreshingProgress') : t('cleanupRefreshProgress')}
  </Button>;
  if (!operation.data) return <div><p className={operation.isError ? 'error' : 'muted'} role={operation.isError ? 'alert' : 'status'}>{operation.isError ? t('actionFailed') : t('loading')}</p>{refreshButton}</div>;

  const ownerLabels = new Map<string, string>([
    ['documents', t('cleanupOwnerDocuments')],
    ['raw', t('cleanupOwnerRaw')],
    ['chat', t('cleanupOwnerChat')],
    ['memory', t('cleanupOwnerMemory')],
    ['agents', t('cleanupOwnerAgents')],
    ['dashboard', t('cleanupOwnerDashboard')],
    ['notifications', t('cleanupOwnerNotifications')],
    ['automations', t('cleanupOwnerAutomations')],
  ]);
  const pendingOwners = [...new Set(operation.data.pending_owner_codes.map((code) => ownerLabels.get(code) ?? t('cleanupOwnerOther')))].slice(0, 9);
  return <div>
    <p role="status">{succeeded ? t('cleanupComplete') : operation.data.status}{operation.data.error_code ? ` · ${operation.data.error_code}` : ''}</p>
    {operation.data.memory_status === 'failed' && (operation.data.memory_error_code === 'legacy_provenance_unresolved' || operation.data.memory_error_code === 'evidence_identity_unavailable')
      && <p className="error" role="alert">{t('cleanupMemoryUnavailable')}</p>}
    {pendingOwners.length > 0 && <p className="muted">{t('cleanupPendingOwnerStages', { owners: pendingOwners.join(', ') })}</p>}
    {refreshButton}
  </div>;
}

/** Renders a source’s schedule, status, and actions, including archive or purge confirmation. */
function SourceEntry({
  source,
  schedule,
  timezone,
  operationId,
  onPurgeStarted,
  onPurgeFinished,
  purged,
  editingId,
  onEdit,
  onChanged,
}: {
  source: Source;
  schedule: string;
  timezone: string | null;
  operationId: string;
  onPurgeStarted: (operationId: string) => void;
  onPurgeFinished: () => void;
  /** Cleanup finished; the row must not offer Disconnect & delete again. */
  purged: boolean;
  /** Source open in the editor; its Pause/Disconnect live in the editor header only. */
  editingId: string | null;
  onEdit: (source: Source) => void;
  onChanged: () => void;
}) {
  const t = useTranslations('sources');
  const display = useDisplayPreferences();
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [historyOpen, setHistoryOpen] = useState(false);
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
  const { busy, error, toggleStatus, disconnect } = useSourceActions({ source, activation: activation.data, onResumed: onEdit, onChanged, onPurgeStarted });

  const editedHere = editingId === source.id;
  const rowState = sourceRowState(source, activation.data);
  const chipKey: Record<SourceRowState, 'statusActive' | 'statusPaused' | 'statusArchived' | 'stateSavedNotActive' | 'stateError'> = {
    active: 'statusActive', paused: 'statusPaused', archived: 'statusArchived', savedNotActive: 'stateSavedNotActive', error: 'stateError',
  };
  const hasDetail = Boolean(collectResult || operationId || (historyOpen && connector) || error || collect.error || activation.isError);
  const cell = 'px-3 py-3 align-top text-sm max-[720px]:block max-[720px]:px-0 max-[720px]:py-1';
  const mobileLabel = (key: 'colSchedule' | 'colCollected' | 'colIndexed') => <span className="muted mr-1 min-[721px]:hidden">{t(key)}:</span>;
  return <>
    <tr className="border-b border-border max-[720px]:mb-3 max-[720px]:block max-[720px]:rounded-lg max-[720px]:border max-[720px]:p-3">
      <th scope="row" className={`${cell} font-normal`}>
        <strong>{source.name}</strong>
        <p className="muted">{providerLabel(source.provider, t) ?? source.type}</p>
        {(source.collection_error_code || source.processing_error_code) && <p className="error">{t('collectionError')}: {source.collection_error_code ?? t('noError')} · {t('processingError')}: {source.processing_error_code ?? t('noError')}</p>}
      </th>
      <td className={cell}>
        <span className="badge">{t(chipKey[rowState])}</span>
        {connector && activation.data?.error_code && <p className="muted">{activation.data.error_code}</p>}
        {connector && activation.data && <p className="muted">{t('activationState')}: {t(({ queued: 'stateQueued', provisioning: 'stateProvisioning', active: 'active', saved_not_active: 'stateSavedNotActive', reconciliation_required: 'stateReconciliationRequired', disabled: 'stateDisabled' } as Record<string, string>)[activation.data.state] ?? 'stateUnknown')}</p>}
      </td>
      <td className={cell}>{mobileLabel('colSchedule')}{connector ? `${schedule}${timezone ? ` · ${timezone}` : ''}` : '—'}</td>
      <td className={cell}>{mobileLabel('colCollected')}{formatDate(source.collected_at, display.locale, display.timezone, t('never'))}</td>
      <td className={cell}>{mobileLabel('colIndexed')}{formatDate(source.indexed_at, display.locale, display.timezone, t('never'))}</td>
      <td className={cell}>
        <div className="flex flex-wrap gap-2" role="group" aria-label={t('sourceActions', { name: source.name })}>
          {connector && source.status !== 'archived' && <Button className="secondary" onClick={() => onEdit(source)}>{t('configure')}</Button>}
          {connector && source.status === 'active' && activation.data?.state === 'active' && <Button className="secondary" disabled={collect.isPending || busy} onClick={() => collect.mutate()}>{collect.isPending ? t('collecting') : t('collectNow')}</Button>}
          {!editedHere && source.status !== 'archived' && <Button className="secondary" disabled={busy} onClick={toggleStatus}>{busy ? t(source.status === 'paused' ? 'resuming' : 'pausing') : t(source.status === 'paused' ? 'resume' : 'pause')}</Button>}
          {!editedHere && !purged && <DisconnectDialog name={source.name} archived={source.status === 'archived'} disabled={busy} onConfirm={(deleteData) => void disconnect(deleteData)}
            trigger={<Button className="secondary" disabled={busy}>{busy ? t('disconnecting') : t(source.status === 'archived' ? 'disconnectDelete' : 'disconnectAction')}</Button>} />}
          {connector && <Button className="secondary" aria-expanded={historyOpen} onClick={() => setHistoryOpen((open) => !open)}>{t(historyOpen ? 'hideHistory' : 'showHistory')}</Button>}
        </div>
      </td>
    </tr>
    {hasDetail && <tr className="max-[720px]:block"><td colSpan={6} className="px-3 pb-4 max-[720px]:block max-[720px]:px-0">
      {collectResult && <p className="muted" role="status">{t('collectResult', { status: collectResult.status })}{!collectResult.hasRun ? ` ${t('noRunId')}` : ''}</p>}
      {operationId && <PurgeProgress operationId={operationId} onSucceeded={onPurgeFinished} />}
      {historyOpen && connector && <SyncHistory source={source} />}
      {(error || collect.error || activation.isError) && <p className="error" role="alert">{t('actionFailed')}{activation.isError && ` · ${t('stateUnknown')}`}</p>}
    </td></tr>}
  </>;
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
  const items = useMemo(() => sources.data?.pages.flatMap((page) => page.items) ?? [], [sources.data]);
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
  const [filter, setFilter] = useState<SourceFilter>('all');
  // Purge operations stay lifted here so rows with in-flight cleanup remain mounted under any filter.
  const [operations, setOperations] = useState<Record<string, string>>({});
  const [purged, setPurged] = useState<Record<string, boolean>>({});
  const activations = useQueries({ queries: connectors.map((source) => ({
    queryKey: connectorKeys.activation(source.id),
    queryFn: () => getConnectorActivation(source.id),
  })) });
  const activationBySourceId = new Map(connectors.map((source, index) => [source.id, activations[index]?.data]));
  const states = new Map(items.map((source) => [source.id, sourceRowState(source, activationBySourceId.get(source.id))]));
  const counts: Record<SourceFilter, number> = { all: items.length, active: 0, paused: 0, attention: 0 };
  for (const state of states.values()) { counts.active += matchesFilter(state, 'active') ? 1 : 0; counts.paused += matchesFilter(state, 'paused') ? 1 : 0; counts.attention += matchesFilter(state, 'attention') ? 1 : 0; }
  const visible = items.filter((source) => Boolean(operations[source.id]) || matchesFilter(states.get(source.id) ?? 'active', filter));
  const configurationBySourceId = new Map(connectors.map((source, index) => [source.id, configurations[index]]));
  /** Invalidates the source list query after a source mutation. */
  const refresh = () => queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  // Prune finished-purge entries only after the row has left the list.
  useEffect(() => {
    if (!sources.isSuccess || sources.isFetching) return;
    const listed = new Set(items.map((source) => source.id));
    const gone = Object.keys(purged).filter((id) => !listed.has(id));
    if (gone.length === 0) return;
    const drop = (current: Record<string, unknown>) => Object.fromEntries(Object.entries(current).filter(([id]) => !gone.includes(id)));
    // eslint-disable-next-line react-hooks/set-state-in-effect -- prunes finished-purge entries once their rows have left the refetched list; no-op when none are gone
    setOperations((current) => drop(current) as Record<string, string>);
    setPurged((current) => drop(current) as Record<string, boolean>);
  }, [items, purged, sources.isSuccess, sources.isFetching]);

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
    {editing !== undefined && <nav aria-label={t('backToSources')}><Button type="button" variant="ghost" onClick={() => transition(undefined)}>← {t('backToSources')}</Button></nav>}
    {editing !== undefined && <ConnectorEditor key={`${editing?.id ?? 'new'}:${editSession}`} source={editing} registerTransitionGuard={(guard) => { transitionGuard.current = guard; }} onChanged={() => { void refresh(); }} onPurgeStarted={(id) => editing && setOperations((current) => ({ ...current, [editing.id]: id }))} onClose={() => transition(undefined)} />}
    {sources.isPending && <div className="skeleton" aria-label={t('loading')} />}
    {sources.isError && <p className="error" role="alert">{t('loadFailed')} <Button className="secondary" onClick={() => sources.refetch()}>{t('retry')}</Button></p>}
    {sources.isSuccess && items.length === 0 && <p className="empty-state">{t('empty')}</p>}
    {items.length > 0 && <SourcesTable filter={filter} counts={counts} onFilterChange={setFilter}>
      {visible.map((source) => {
        const configuration = configurationBySourceId.get(source.id);
        return <SourceEntry key={source.id} source={source} schedule={configuration?.data ? scheduleLabel(configuration.data.configuration.schedule_interval_minutes, t) : t('scheduleUnknown')} timezone={configuration?.data?.configuration.timezone ?? null} operationId={operations[source.id] ?? ''} editingId={editing?.id ?? null} onPurgeStarted={(id) => setOperations((current) => ({ ...current, [source.id]: id }))} purged={Boolean(purged[source.id])} onPurgeFinished={() => { if (purged[source.id]) return; setPurged((current) => ({ ...current, [source.id]: true })); void refresh(); }} onEdit={(value) => transition(value)} onChanged={() => { void refresh(); }} />;
      })}
    </SourcesTable>}
    {items.length > 0 && visible.length === 0 && <p className="empty-state">{t('emptyFiltered')}</p>}
    {sources.hasNextPage && <Button className="secondary" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{t('loadMore')}</Button>}
  </div>;
}
