'use client';

import { useInfiniteQuery, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { useGuardedNavigation } from '@/core/guarded-navigation';
import {
  connectorKeys,
  ConnectorConfiguration,
  getConnectorActivation,
  getMonotonicConnectorConfiguration,
  listBuiltinSources,
  listSources,
  Source,
  sourceKeys,
} from './api';
import { BuiltinResumeList, BuiltinWizard } from './builtin-sources';
import { SourceWizard } from './source-wizard';
import { StarterPackWizard } from './starter-pack-wizard';
import { SourceEntry, scheduleLabel } from './source-entry';
import { matchesFilter, SourceFilter, sourceRowState, SourcesTable } from './source-table';
import { TablePagination, usePagination } from '@/components/ui/pagination';
import { parseShapeFilter } from './shape-filter';
import { templateName } from '@/core/renderer-labels';
import { humanizeCode } from '../shared/humanize-code';

/** Banner naming the gadget renderer the Sources page is filtered for, with a clear action. */
function ShapeFilterBanner({ renderer, onClear }: { renderer: string | null; onClear: () => void }) {
  const t = useTranslations('sources');
  const tg = useTranslations('gadgetSettings');
  return <div role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted p-3 text-sm">
    <span>{t('shapeFilterBanner', { renderer: renderer ? templateName(renderer, tg) : '' })}</span>
    <Button size="sm" variant="outline" onClick={onClear}>{t('shapeFilterClear')}</Button>
  </div>;
}

/** Which add/edit dialog is open on the sources page; null when only the table is shown. */
type DialogState = { kind: 'builtin' } | { kind: 'starter' } | { kind: 'connector' } | { kind: 'edit'; source: Source } | null;

/**
 * Dialog open state with the dirty-draft guard. `transition` opens, switches or closes the dialog, first confirming that
 * the sources document may be left and that a dirty connector draft may be discarded; each accepted transition bumps
 * `editSession` so the wizard remounts with fresh state. `transitionGuard` is registered by the open wizard.
 */
function useDialogTransition() {
  const { ensureSourcesDocument } = useGuardedNavigation();
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
  /** Lets the open wizard register (or clear) its dirty-draft guard. */
  const registerTransitionGuard = (guard: (() => boolean) | null) => { transitionGuard.current = guard; };
  return { dialog, editSession, transition, registerTransitionGuard };
}

/**
 * Purge operations lifted out of the rows so rows with in-flight cleanup stay mounted under any filter. Entries for
 * finished purges are pruned only after the row has left the (settled) refetched list.
 */
function usePurgeTracking(items: Source[], listSettled: boolean) {
  const [operations, setOperations] = useState<Record<string, string>>({});
  const [purged, setPurged] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (!listSettled) return;
    const listed = new Set(items.map((source) => source.id));
    const gone = Object.keys(purged).filter((id) => !listed.has(id));
    if (gone.length === 0) return;
    const drop = (current: Record<string, unknown>) => Object.fromEntries(Object.entries(current).filter(([id]) => !gone.includes(id)));
    // eslint-disable-next-line react-hooks/set-state-in-effect -- prunes finished-purge entries once their rows have left the refetched list; no-op when none are gone
    setOperations((current) => drop(current) as Record<string, string>);
    setPurged((current) => drop(current) as Record<string, boolean>);
  }, [items, purged, listSettled]);
  return { operations, setOperations, purged, setPurged };
}

/**
 * Opens `?source=<id>` once, when that source first appears in the loaded list. A missing param settles immediately;
 * an unknown id keeps waiting for later pages.
 */
function useSourceDeepLink(items: Source[], loaded: boolean, open: (source: Source) => void) {
  const deepLinkHandled = useRef(false);
  useEffect(() => {
    if (deepLinkHandled.current || !loaded) return;
    const id = new URLSearchParams(window.location.search).get('source');
    if (!id) { deepLinkHandled.current = true; return; }
    const target = items.find((source) => source.id === id);
    if (!target) return;
    deepLinkHandled.current = true;
    open(target);
  }, [items, loaded, open]);
}

/** Per-connector configuration and activation queries (shared caches with the editor) keyed by source id. */
function useConnectorDetails(connectors: Source[]) {
  const queryClient = useQueryClient();
  const configurations = useQueries({ queries: connectors.map((source) => ({
    queryKey: connectorKeys.configuration(source.id),
    queryFn: ({ signal }: { signal: AbortSignal }) => getMonotonicConnectorConfiguration(source.id, signal, () => [
      queryClient.getQueryData<ConnectorConfiguration>(connectorKeys.configuration(source.id)),
    ]),
  })) });
  const activations = useQueries({ queries: connectors.map((source) => ({
    queryKey: connectorKeys.activation(source.id),
    queryFn: () => getConnectorActivation(source.id),
  })) });
  return {
    configurationBySourceId: new Map(connectors.map((source, index) => [source.id, configurations[index]])),
    activationBySourceId: new Map(connectors.map((source, index) => [source.id, activations[index]?.data])),
  };
}

/** Add menu: starter packs button plus the Add datasource dropdown (built-in / connector). */
function AddDatasourceMenu({ empty, allBuiltinsAdded, onSelect }: { empty: boolean; allBuiltinsAdded: boolean; onSelect: (kind: 'starter' | 'builtin' | 'connector') => void }) {
  const t = useTranslations('sources');
  return <div className="flex flex-wrap items-center justify-end gap-2 max-[1023px]:justify-start" role="group" aria-label={t('tableToolbar')}>
    <Button size="sm" variant={empty ? 'default' : 'outline'} onClick={() => onSelect('starter')}>{t('starterPacks')}</Button>
    <DropdownMenu>
      <DropdownMenuTrigger asChild><Button size="sm">{t('addDatasource')}<ChevronDown aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem disabled={allBuiltinsAdded} onSelect={() => onSelect('builtin')}>
          <span className="flex flex-col"><span>{t('menuBuiltin')}</span><span className="text-xs text-muted-foreground">{allBuiltinsAdded ? t('addBuiltinAllAdded') : t('menuBuiltinHint')}</span></span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onSelect('connector')}>
          <span className="flex flex-col"><span>{t('menuConnector')}</span><span className="text-xs text-muted-foreground">{t('menuConnectorHint')}</span></span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  </div>;
}

/** The single open add/edit dialog, if any; closing goes through the draft-guarded transition. */
function SourceDialogs({ dialog, editSession, shapeFilter, onClearFilter, transition, registerTransitionGuard, refresh }: {
  dialog: DialogState; editSession: number; shapeFilter: ReturnType<typeof parseShapeFilter>['shapes'] | undefined; onClearFilter: () => void;
  transition: (next: DialogState) => void; registerTransitionGuard: (guard: (() => boolean) | null) => void; refresh: () => void;
}) {
  const closeDialog = () => transition(null);
  if (dialog?.kind === 'builtin') return <BuiltinWizard key={editSession} shapeFilter={shapeFilter} onClearFilter={onClearFilter} onAddConnector={() => transition({ kind: 'connector' })} onClose={closeDialog} onChanged={refresh} />;
  if (dialog?.kind === 'starter') return <StarterPackWizard key={editSession} onAddConnector={() => transition({ kind: 'connector' })} onClose={closeDialog} onChanged={refresh} />;
  if (!dialog) return null;
  return <SourceWizard key={editSession} request={dialog} shapeFilter={shapeFilter} onClearFilter={onClearFilter} registerTransitionGuard={registerTransitionGuard} onChanged={refresh} onClose={closeDialog} />;
}

/**
 * Per-filter row counts over all sources plus the rows visible under `filter`. A row with an in-flight purge operation
 * stays visible (unless archived) so its cleanup progress is not lost when the owner switches filters.
 */
function filterSources(items: Source[], activationBySourceId: Map<string, Parameters<typeof sourceRowState>[1]>, operations: Record<string, string>, filter: SourceFilter) {
  const states = new Map(items.map((source) => [source.id, sourceRowState(source, activationBySourceId.get(source.id))]));
  const counts: Record<SourceFilter, number> = { all: 0, active: 0, paused: 0, attention: 0, archived: 0 };
  for (const state of states.values()) for (const key of Object.keys(counts) as SourceFilter[]) counts[key] += matchesFilter(state, key) ? 1 : 0;
  const visible = items.filter((source) => {
    const state = states.get(source.id) ?? 'active';
    return (state !== 'archived' && Boolean(operations[source.id])) || matchesFilter(state, filter);
  });
  return { counts, visible };
}

/**
 * Sources page header and table, with the single Add datasource menu beside the title and paused built-ins above the list.
 * Every add and edit flow runs in a full-screen wizard dialog; closing it goes through the active dirty-draft guard.
 * `?source=<id>` opens that source's edit dialog once the list has loaded.
 */
export function SourceList() {
  const t = useTranslations('sources');
  const queryClient = useQueryClient();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const router = useRouter();
  const searchParams = useSearchParams();
  // Dashboard tiles with no compatible source link here with ?shapes=&for=; the filter only narrows the add flows.
  const { shapes, renderer } = useMemo(() => parseShapeFilter(searchParams), [searchParams]);
  const shapeFilter = shapes.length ? shapes : undefined;
  /** Drops both filter params from the URL. */
  const clearFilter = useCallback(() => router.replace('/settings/sources'), [router]);
  const { dialog, editSession, transition, registerTransitionGuard } = useDialogTransition();
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
  const { configurationBySourceId, activationBySourceId } = useConnectorDetails(connectors);
  const [filter, setFilter] = useState<SourceFilter>('all');
  const { operations, setOperations, purged, setPurged } = usePurgeTracking(items, sources.isSuccess && !sources.isFetching);
  const { counts, visible } = filterSources(items, activationBySourceId, operations, filter);
  const pager = usePagination(visible);
  /** Invalidates the source list query after a source mutation. */
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: sourceKeys.all }); };
  useSourceDeepLink(items, sources.isSuccess, (target) => transition({ kind: 'edit', source: target }));

  return <div className="sources-list">
    <header className="sources-page-header">
      <div className="min-w-0">
        <h1 ref={headingRef} tabIndex={-1} className="m-0 text-2xl font-semibold tracking-[-.02em] outline-none focus-visible:ring-2 focus-visible:ring-ring">{t('title')}</h1>
        <p className="mt-2 mb-0 text-sm text-muted-foreground">{t('description')}</p>
      </div>
      {/* One primary "Add datasource" menu replaces the former row of add buttons; each item opens its wizard via the draft-guarded transition. */}
      <AddDatasourceMenu empty={items.length === 0 && sources.isSuccess} allBuiltinsAdded={allBuiltinsAdded} onSelect={(kind) => transition({ kind })} />
    </header>
    {shapeFilter && <ShapeFilterBanner renderer={renderer} onClear={clearFilter} />}
    <BuiltinResumeList onChanged={refresh} />
    <SourceDialogs dialog={dialog} editSession={editSession} shapeFilter={shapeFilter} onClearFilter={clearFilter} transition={transition} registerTransitionGuard={registerTransitionGuard} refresh={refresh} />
    {sources.isPending && <div className="skeleton" aria-label={t('loading')} />}
    {sources.isError && <p className="error" role="alert">{t('loadFailed')} <Button variant="outline" onClick={() => sources.refetch()}>{t('retry')}</Button></p>}
    {sources.isSuccess && items.length === 0 && <p className="empty-state">{t('empty')} {t('starterEmptyHint')}</p>}
    {items.length > 0 && <SourcesTable filter={filter} counts={counts} onFilterChange={(next) => { setFilter(next); pager.setPage(1); }} pagination={<TablePagination {...pager} />}>
      {pager.pageItems.map((source) => {
        const configuration = configurationBySourceId.get(source.id);
        return <SourceEntry key={source.id} source={source} schedule={configuration?.data ? scheduleLabel(configuration.data.configuration.schedule_interval_minutes, t) : configuration?.isPending ? t('scheduleLoading') : t('scheduleUnknown')} timezone={configuration?.data?.configuration.timezone ?? null} realtimeMode={configuration?.data?.configuration.realtime_mode ?? null} operationId={operations[source.id] ?? ''} editingId={dialog?.kind === 'edit' ? dialog.source.id : null} onPurgeStarted={(id) => setOperations((current) => ({ ...current, [source.id]: id }))} purged={Boolean(purged[source.id])} onPurgeFinished={() => { if (purged[source.id]) return; setPurged((current) => ({ ...current, [source.id]: true })); refresh(); }} onEdit={(value) => transition({ kind: 'edit', source: value })} onChanged={refresh} onRemoved={() => headingRef.current?.focus()} />;
      })}
    </SourcesTable>}
    {items.length > 0 && visible.length === 0 && <p className="empty-state">{t('emptyFiltered')}</p>}
    {sources.hasNextPage && <Button variant="outline" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{t('loadMore')}</Button>}
  </div>;
}

/** Re-exported for existing importers; the implementation lives in `modules/shared/humanize-code`. */
export { humanizeCode };
