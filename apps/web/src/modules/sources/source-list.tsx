'use client';

import { useInfiniteQuery, useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Download, History, Pause, Play, RefreshCw, Settings2, Trash2, Unplug } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { AppLocaleId, normalizeFormattingLocale } from '@/core/i18n';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import {
  connectorKeys,
  ConnectorConfiguration,
  getConnectorCatalog,
  getConnectorActivation,
  getMonotonicConnectorConfiguration,
  getOperation,
  listBuiltinSources,
  listSources,
  Source,
  sourceKeys,
  triggerCollection,
} from './api';
import { BuiltinResumeList, BuiltinWizard } from './builtin-sources';
import { DisconnectDialog } from './disconnect-dialog';
import { useSourceActions } from './use-source-actions';
import { SourceWizard } from './source-wizard';
import { matchesFilter, SourceFilter, SourceRowState, sourceRowState, SourcesTable } from './source-table';
import { SyncHistory } from './sync-history';

/** Renders an accessible icon-only source action with the same translated name in its tooltip and accessible label. */
function SourceAction({ label, icon, onClick, disabled = false, expanded }: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  expanded?: boolean;
}) {
  return <Tooltip>
    <TooltipTrigger asChild>
      <Button type="button" size="icon" variant="outline" aria-label={label} aria-expanded={expanded} disabled={disabled} onClick={onClick}>
        {icon}
      </Button>
    </TooltipTrigger>
    <TooltipContent>{label}</TooltipContent>
  </Tooltip>;
}

/** Formats an optional ISO timestamp with the supplied application locale and time zone, returning the supplied fallback when absent. */
function formatDate(value: string | null, locale: AppLocaleId, timezone: string, fallback: string): string {
  return value ? new Intl.DateTimeFormat(normalizeFormattingLocale(locale), {
    dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
  }).format(new Date(value)) : fallback;
}

/** Resolves registered provider IDs to translated names, then to the catalog label, so new providers need no message key; otherwise the raw ID. */
function providerLabel(provider: string | null, t: ReturnType<typeof useTranslations<'sources'>>, catalogLabel?: string) {
  if (!provider) return null;
  const keys: Record<string, string> = {
    youtube: 'providerYoutube', arxiv: 'providerArxiv', huggingface: 'providerHuggingface', github_releases: 'providerGithubReleases',
    telegram: 'providerTelegram', rss: 'providerRss', web: 'providerWeb', rest: 'providerRest',
  };
  const key = keys[provider];
  return key ? t(key as 'providerYoutube') : catalogLabel ?? provider;
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
  const refreshButton = <Button type="button" variant="outline" disabled={operation.isFetching} onClick={() => void operation.refetch()}>
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
    {pendingOwners.length > 0 && <p className="text-muted-foreground">{t('cleanupPendingOwnerStages', { owners: pendingOwners.join(', ') })}</p>}
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
  onRemoved,
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
  /** Called when a confirmed disconnect removes this row, so the owner can place focus. */
  onRemoved: () => void;
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
  // Shared cache with the editor; supplies the label for providers that have no message key of their own.
  const catalog = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal), enabled: Boolean(source.provider), staleTime: 5 * 60_000 });
  const catalogLabel = catalog.data?.find((entry) => entry.provider_id === source.provider)?.label;
  const collect = useMutation({
    mutationFn: () => triggerCollection(source.id, csrfToken),
    onSuccess: (result) => {
      setCollectResult({ status: result.status, hasRun: Boolean(result.run_id) });
      void queryClient.invalidateQueries({ queryKey: connectorKeys.ingestion(source.id) });
      void queryClient.invalidateQueries({ queryKey: sourceKeys.detail(source.id) });
    },
  });
  const { busy, error, toggleStatus, disconnect } = useSourceActions({ source, activation: activation.data, onResumed: onEdit, onChanged, onPurgeStarted });

  const nameCellRef = useRef<HTMLTableCellElement>(null);
  const confirmedRef = useRef(false);
  const everConfirmedRef = useRef(false);
  const onRemovedRef = useRef(onRemoved);
  useEffect(() => { onRemovedRef.current = onRemoved; });
  // A confirmed disconnect that unmounts the row leaves focus on <body>; hand it to the list heading.
  useEffect(() => () => {
    const lost = !document.activeElement || document.activeElement === document.body;
    if (everConfirmedRef.current && lost) onRemovedRef.current();
  }, []);
  const editedHere = editingId === source.id;
  const rowState = sourceRowState(source, activation.data);
  const chipKey: Record<SourceRowState, 'statusActive' | 'statusPaused' | 'statusArchived' | 'stateSavedNotActive' | 'stateError' | 'stateReconnect'> = {
    active: 'statusActive', paused: 'statusPaused', archived: 'statusArchived', savedNotActive: 'stateSavedNotActive', error: 'stateError', reconnect: 'stateReconnect',
  };
  const hasDetail = Boolean(collectResult || operationId || error || collect.error || activation.isError);
  const cell = 'px-3 py-3 align-top text-sm max-[720px]:block max-[720px]:px-0 max-[720px]:py-1';
  const mobileLabel = (key: 'colSchedule' | 'colCollected' | 'colIndexed') => <span className="text-muted-foreground mr-1 min-[721px]:hidden">{t(key)}:</span>;
  return <>
    <tr className="border-b border-border max-[720px]:mb-3 max-[720px]:block max-[720px]:rounded-lg max-[720px]:border max-[720px]:p-3">
      <th scope="row" ref={nameCellRef} tabIndex={-1} className={`${cell} font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring`}>
        <strong>{source.name}</strong>
        <p className="text-muted-foreground">{providerLabel(source.provider, t, catalogLabel) ?? source.type}</p>
        {(source.collection_error_code || source.processing_error_code) && <p className="error">{t('collectionError')}: {source.collection_error_code ?? t('noError')} · {t('processingError')}: {source.processing_error_code ?? t('noError')}</p>}
      </th>
      <td className={cell}>
        <Badge variant="outline" className={rowState === 'active' ? 'border-primary text-primary' : rowState === 'error' || rowState === 'savedNotActive' ? 'border-destructive text-destructive' : undefined}>{t(chipKey[rowState])}</Badge>
        {connector && activation.data?.error_code && <p className="text-muted-foreground">{activation.data.error_code}</p>}
        {connector && activation.data && <p className="text-muted-foreground">{t('activationState')}: {t(({ queued: 'stateQueued', provisioning: 'stateProvisioning', active: 'active', saved_not_active: 'stateSavedNotActive', reconciliation_required: 'stateReconciliationRequired', disabled: 'stateDisabled' } as Record<string, string>)[activation.data.state] ?? 'stateUnknown')}</p>}
      </td>
      <td className={cell}>{mobileLabel('colSchedule')}{connector ? `${schedule}${timezone ? ` · ${timezone}` : ''}` : '—'}</td>
      <td className={cell}>{mobileLabel('colCollected')}{formatDate(source.collected_at, display.locale, display.timezone, t('never'))}</td>
      <td className={cell}>{mobileLabel('colIndexed')}{formatDate(source.indexed_at, display.locale, display.timezone, t('never'))}</td>
      <td className={cell}>
        <TooltipProvider delayDuration={300}>
        <div className="flex flex-wrap gap-2" role="group" aria-label={t('sourceActions', { name: source.name })}>
          {connector && rowState === 'savedNotActive' && source.status !== 'archived' && <SourceAction label={t('retryActivation')} icon={<RefreshCw size={16} />} disabled={busy} onClick={() => onEdit(source)} />}
          {connector && source.status !== 'archived' && <SourceAction label={t('configure')} icon={<Settings2 size={16} />} onClick={() => onEdit(source)} />}
          {connector && source.status === 'active' && activation.data?.state === 'active' && <SourceAction label={collect.isPending ? t('collecting') : t('collectNow')} icon={<Download size={16} />} disabled={collect.isPending || busy} onClick={() => collect.mutate()} />}
          {!editedHere && source.status !== 'archived' && <SourceAction label={busy ? t(source.status === 'paused' ? 'resuming' : 'pausing') : t(source.status === 'paused' ? 'resume' : 'pause')} icon={source.status === 'paused' ? <Play size={16} /> : <Pause size={16} />} disabled={busy} onClick={toggleStatus} />}
          {!editedHere && !purged && <DisconnectDialog sourceId={source.id} name={source.name} archived={source.status === 'archived'} disabled={busy} triggerLabel={busy ? t('disconnecting') : t(source.status === 'archived' ? 'disconnectDelete' : 'disconnectAction')} onConfirm={(deleteData) => { confirmedRef.current = true; everConfirmedRef.current = true; void disconnect(deleteData); }}
            onCloseAutoFocus={(event) => { if (!confirmedRef.current) return; confirmedRef.current = false; event.preventDefault(); nameCellRef.current?.focus(); }}
            trigger={<Button type="button" size="icon" variant="outline" aria-label={busy ? t('disconnecting') : t(source.status === 'archived' ? 'disconnectDelete' : 'disconnectAction')} disabled={busy}><span aria-hidden="true">{source.status === 'archived' ? <Trash2 size={16} /> : <Unplug size={16} />}</span></Button>} />}
          {connector && <SourceAction label={t('showHistory')} icon={<History size={16} />} expanded={historyOpen} onClick={() => setHistoryOpen(true)} />}
        </div>
        </TooltipProvider>
      </td>
    </tr>
    {hasDetail && <tr className="max-[720px]:block"><td colSpan={6} className="px-3 pb-4 max-[720px]:block max-[720px]:px-0">
      {collectResult && <p className="text-muted-foreground" role="status">{t('collectResult', { status: collectResult.status })}{!collectResult.hasRun ? ` ${t('noRunId')}` : ''}</p>}
      {operationId && <PurgeProgress operationId={operationId} onSucceeded={onPurgeFinished} />}
      {(error || collect.error || activation.isError) && <p className="error" role="alert">{t('actionFailed')}{activation.isError && ` · ${t('stateUnknown')}`}</p>}
    </td></tr>}
    {connector && <Dialog open={historyOpen} onOpenChange={setHistoryOpen}>
      <DialogContent className="w-[80vw] max-w-[80vw] sm:max-w-[80vw]">
        <DialogHeader>
          <DialogTitle>{t('runHistoryTitle')} · {source.name}</DialogTitle>
          <DialogDescription>{t('recentRuns')}</DialogDescription>
        </DialogHeader>
        <SyncHistory source={source} />
      </DialogContent>
    </Dialog>}
  </>;
}

/** Which add/edit dialog is open on the sources page; null when only the table is shown. */
type DialogState = { kind: 'builtin' } | { kind: 'connector' } | { kind: 'manual' } | { kind: 'edit'; source: Source } | null;

/**
 * Sources page header and table, with add actions beside the title and paused built-ins above the list.
 * Every add and edit flow runs in a full-screen wizard dialog; closing it goes through the active dirty-draft guard.
 * `?source=<id>` opens that source's edit dialog once the list has loaded.
 */
export function SourceList() {
  const t = useTranslations('sources');
  const queryClient = useQueryClient();
  const { ensureSourcesDocument } = useGuardedNavigation();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [dialog, setDialog] = useState<DialogState>(null);
  const [editSession, setEditSession] = useState(0);
  const transitionGuard = useRef<(() => boolean) | null>(null);
  /** Opens, switches or closes the dialog, first confirming that a dirty connector draft may be discarded. */
  const transition = useCallback((next: DialogState) => {
    if (next !== null && !ensureSourcesDocument()) return;
    if (transitionGuard.current && !transitionGuard.current()) return;
    transitionGuard.current = null;
    setEditSession((value) => value + 1);
    setDialog(next);
  }, [ensureSourcesDocument]);
  const sources = useInfiniteQuery({
    queryKey: sourceKeys.list,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listSources(pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = useMemo(() => sources.data?.pages.flatMap((page) => page.items) ?? [], [sources.data]);
  const builtins = useQuery({ queryKey: connectorKeys.builtins, queryFn: ({ signal }) => listBuiltinSources(signal) });
  // One source per built-in: with every built-in already added there is nothing left to pick.
  const allBuiltinsAdded = Boolean(builtins.data?.length) && builtins.data!.every((item) => item.source_id !== null);
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
  const counts: Record<SourceFilter, number> = { all: 0, active: 0, paused: 0, attention: 0, archived: 0 };
  for (const state of states.values()) for (const key of Object.keys(counts) as SourceFilter[]) counts[key] += matchesFilter(state, key) ? 1 : 0;
  const visible = items.filter((source) => {
    const state = states.get(source.id) ?? 'active';
    return (state !== 'archived' && Boolean(operations[source.id])) || matchesFilter(state, filter);
  });
  const configurationBySourceId = new Map(connectors.map((source, index) => [source.id, configurations[index]]));
  /** Invalidates the source list query after a source mutation. */
  const refresh = () => queryClient.invalidateQueries({ queryKey: sourceKeys.all });
  // Deep link: /settings/sources?source=<id> opens that source's editor once, when it appears in the loaded list.
  const deepLinkHandled = useRef(false);
  useEffect(() => {
    if (deepLinkHandled.current || !sources.isSuccess) return;
    const id = new URLSearchParams(window.location.search).get('source');
    if (!id) { deepLinkHandled.current = true; return; }
    const target = items.find((source) => source.id === id);
    if (!target) return;
    deepLinkHandled.current = true;
    transition({ kind: 'edit', source: target });
  }, [items, sources.isSuccess, transition]);
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

  const closeDialog = () => transition(null);
  return <div className="sources-list">
    <header className="sources-page-header">
      <div className="min-w-0">
        <h1 ref={headingRef} tabIndex={-1} className="m-0 text-[30px] font-bold tracking-[-.04em] outline-none focus-visible:ring-2 focus-visible:ring-ring">{t('title')}</h1>
        <p className="mt-2 mb-0 text-muted-foreground">{t('description')}</p>
      </div>
      <div className="form-actions source-list-actions" role="group" aria-label={t('tableToolbar')}>
        <Button size="sm" disabled={allBuiltinsAdded} title={allBuiltinsAdded ? t('addBuiltinAllAdded') : undefined} onClick={() => transition({ kind: 'builtin' })}>{t('addBuiltin')}</Button>
        <Button size="sm" variant="outline" onClick={() => transition({ kind: 'connector' })}>{t('addConnector')}</Button>
        <Button size="sm" variant="outline" onClick={() => transition({ kind: 'manual' })}>{t('addManual')}</Button>
      </div>
    </header>
    <BuiltinResumeList onChanged={() => { void refresh(); }} />
    {dialog?.kind === 'builtin' && <BuiltinWizard key={editSession} onClose={closeDialog} onChanged={() => { void refresh(); }} />}
    {dialog && dialog.kind !== 'builtin' && <SourceWizard key={editSession} request={dialog} registerTransitionGuard={(guard) => { transitionGuard.current = guard; }} onChanged={() => { void refresh(); }} onClose={closeDialog} />}
    {sources.isPending && <div className="skeleton" aria-label={t('loading')} />}
    {sources.isError && <p className="error" role="alert">{t('loadFailed')} <Button variant="outline" onClick={() => sources.refetch()}>{t('retry')}</Button></p>}
    {sources.isSuccess && items.length === 0 && <p className="empty-state">{t('empty')}</p>}
    {items.length > 0 && <SourcesTable filter={filter} counts={counts} onFilterChange={setFilter}>
      {visible.map((source) => {
        const configuration = configurationBySourceId.get(source.id);
        return <SourceEntry key={source.id} source={source} schedule={configuration?.data ? scheduleLabel(configuration.data.configuration.schedule_interval_minutes, t) : t('scheduleUnknown')} timezone={configuration?.data?.configuration.timezone ?? null} operationId={operations[source.id] ?? ''} editingId={dialog?.kind === 'edit' ? dialog.source.id : null} onPurgeStarted={(id) => setOperations((current) => ({ ...current, [source.id]: id }))} purged={Boolean(purged[source.id])} onPurgeFinished={() => { if (purged[source.id]) return; setPurged((current) => ({ ...current, [source.id]: true })); void refresh(); }} onEdit={(value) => transition({ kind: 'edit', source: value })} onChanged={() => { void refresh(); }} onRemoved={() => headingRef.current?.focus()} />;
      })}
    </SourcesTable>}
    {items.length > 0 && visible.length === 0 && <p className="empty-state">{t('emptyFiltered')}</p>}
    {sources.hasNextPage && <Button variant="outline" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{t('loadMore')}</Button>}
  </div>;
}
