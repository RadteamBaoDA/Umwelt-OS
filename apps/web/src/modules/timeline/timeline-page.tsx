'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { useDisplayPreferences } from '@/core/query-provider';
import { entityKeys, getGraphStatuses, listEntities } from '@/modules/knowledge/api';
import { listSources, sourceKeys } from '@/modules/sources/api';
import { EventDetail } from './event-detail';
import { listTimeline, type TimelineQuery } from './api';

type Precision = TimelineQuery['precision'];

/** Coordinates URL-persisted filters, bounded timeline paging, and linked event detail. */
export function TimelinePage() {
  const t = useTranslations('timeline');
  const router = useRouter();
  const searchParams = useSearchParams();
  const display = useDisplayPreferences();
  const applied = searchParams.toString();
  const params = new URLSearchParams(applied);
  const dateFrom = params.get('date_from') ?? '';
  const dateTo = params.get('date_to') ?? '';
  const sourceId = params.get('source_id') ?? '';
  const entityId = params.get('entity_id') ?? '';
  const precision = (['all', 'timed', 'date', 'unknown'].includes(params.get('precision') ?? '') ? params.get('precision') : 'all') as Precision;
  const eventType = (params.get('type') ?? '').trim();
  const searchText = (params.get('q') ?? '').trim().slice(0, 200);
  const [draftDateFrom, setDraftDateFrom] = useState(dateFrom);
  const [draftDateTo, setDraftDateTo] = useState(dateTo);
  const [draftSourceId, setDraftSourceId] = useState(sourceId);
  const [draftEntityId, setDraftEntityId] = useState(entityId);
  const [draftPrecision, setDraftPrecision] = useState<Precision>(precision);
  const [draftEventType, setDraftEventType] = useState(eventType);
  const [dateError, setDateError] = useState(false);
  const typeTooLong = [...draftEventType.trim()].length > 64;
  const appliedTypeTooLong = [...eventType].length > 64;

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncs state to an external or prop change; reset-on-change is intentional here
    setDraftDateFrom(dateFrom); setDraftDateTo(dateTo); setDraftSourceId(sourceId);
    setDraftEntityId(entityId); setDraftPrecision(precision); setDraftEventType(eventType);
  }, [dateFrom, dateTo, sourceId, entityId, precision, eventType]);

  const query: TimelineQuery = {
    date_from: dateFrom, date_to: dateTo, timezone: display.timezone,
    source_id: sourceId, entity_id: entityId, precision, type: eventType, q: searchText,
  };
  const timeline = useInfiniteQuery({
    // A normalized server filter in the key starts a fresh cursor chain whenever the filter changes.
    queryKey: ['timeline', query],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listTimeline(query, pageParam),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: !appliedTypeTooLong,
  });
  const sources = useInfiniteQuery({ queryKey: sourceKeys.list, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listSources(pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const entities = useInfiniteQuery({ queryKey: entityKeys.list(), initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listEntities(pageParam), getNextPageParam: (page) => page.next_cursor ?? undefined });
  const events = timeline.data?.pages.flatMap((page) => page.items) ?? [];
  const allDocumentVersionIds = [...new Set(events.flatMap((event) => event.evidence.map((item) => item.document_version_id)))];
  // The owner status endpoint accepts at most 100 version IDs per request; disclose the omitted remainder below.
  const documentVersionIds = allDocumentVersionIds.slice(0, 100);
  const graphStatuses = useQuery({ queryKey: ['graph-status', documentVersionIds], queryFn: () => getGraphStatuses(documentVersionIds), enabled: !!timeline.data && documentVersionIds.length > 0 });
  const sourceOptions = sources.data?.pages.flatMap((page) => page.items) ?? [];
  const entityOptions = entities.data?.pages.flatMap((page) => page.items) ?? [];
  const entityNames = useMemo(() => new Map((entities.data?.pages.flatMap((page) => page.items) ?? []).map((entity) => [entity.id, entity.name ?? t('unnamedEntity')])), [entities.data, t]);

  /** Applies validated filters to the URL so refresh, sharing, and browser history retain the view. */
  function submitFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (typeTooLong) return;
    if (draftPrecision !== 'unknown' && (!!draftDateFrom !== !!draftDateTo || (draftDateFrom && draftDateTo && draftDateTo <= draftDateFrom))) {
      setDateError(true);
      return;
    }
    setDateError(false);
    const next = new URLSearchParams();
    if (draftPrecision !== 'unknown' && draftDateFrom && draftDateTo) { next.set('date_from', draftDateFrom); next.set('date_to', draftDateTo); }
    if (draftSourceId) next.set('source_id', draftSourceId);
    if (draftEntityId) next.set('entity_id', draftEntityId);
    if (draftPrecision !== 'all') next.set('precision', draftPrecision);
    if (draftEventType.trim()) next.set('type', draftEventType.trim());
    router.push(`/timeline${next.size ? `?${next}` : ''}`);
  }

  return <section className="content-panel">
    <span className="brand">{t('knowledgeBrand')}</span><h1>{t('title')}</h1><p className="muted">{t('intro', { timezone: display.timezone })}</p>
    <form className="form" onSubmit={submitFilters}>
      <div className="search-filters">
        <div className="field"><Label htmlFor="timeline-from">{t('dateFrom')}</Label><Input id="timeline-from" type="date" value={draftDateFrom} disabled={draftPrecision === 'unknown'} onChange={(event) => setDraftDateFrom(event.target.value)} /></div>
        <div className="field"><Label htmlFor="timeline-to">{t('dateToExclusive')}</Label><Input id="timeline-to" type="date" min={draftDateFrom || undefined} value={draftDateTo} disabled={draftPrecision === 'unknown'} onChange={(event) => setDraftDateTo(event.target.value)} /></div>
        <div className="field"><Label htmlFor="timeline-source">{t('source')}</Label><Select value={draftSourceId || 'all'} onValueChange={(value) => setDraftSourceId(value === 'all' ? '' : value)}><SelectTrigger id="timeline-source"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">{t('allSources')}</SelectItem>{draftSourceId && !sourceOptions.some((source) => source.id === draftSourceId) && <SelectItem value={draftSourceId}>{draftSourceId}</SelectItem>}{sourceOptions.map((source) => <SelectItem key={source.id} value={source.id}>{source.name}</SelectItem>)}</SelectContent></Select>{sources.hasNextPage && <Button type="button" className="secondary" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{t('loadSources')}</Button>}{sources.isError && <p className="error" role="alert">{t('sourcesUnavailable')}</p>}</div>
        <div className="field"><Label htmlFor="timeline-entity">{t('entity')}</Label><Select value={draftEntityId || 'all'} onValueChange={(value) => setDraftEntityId(value === 'all' ? '' : value)}><SelectTrigger id="timeline-entity"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">{t('allEntities')}</SelectItem>{draftEntityId && !entityOptions.some((entity) => entity.id === draftEntityId) && <SelectItem value={draftEntityId}>{draftEntityId}</SelectItem>}{entityOptions.map((entity) => <SelectItem key={entity.id} value={entity.id}>{entity.name ?? t('unnamedEntity')}</SelectItem>)}</SelectContent></Select>{entities.hasNextPage && <Button type="button" className="secondary" disabled={entities.isFetchingNextPage} onClick={() => entities.fetchNextPage()}>{t('loadEntities')}</Button>}{entities.isError && <p className="error" role="alert">{t('entitiesUnavailable')}</p>}</div>
        <div className="field"><Label htmlFor="timeline-precision">{t('precision')}</Label><Select value={draftPrecision} onValueChange={(value) => setDraftPrecision(value as Precision)}><SelectTrigger id="timeline-precision"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">{t('allPrecision')}</SelectItem><SelectItem value="timed">{t('timed')}</SelectItem><SelectItem value="date">{t('dateOnly')}</SelectItem><SelectItem value="unknown">{t('unknownTime')}</SelectItem></SelectContent></Select></div>
        <div className="field"><Label htmlFor="timeline-type">{t('type')}</Label><Input id="timeline-type" value={draftEventType} aria-invalid={typeTooLong} aria-describedby={typeTooLong ? 'timeline-type-error' : undefined} onChange={(event) => setDraftEventType(event.target.value)} />{typeTooLong ? <p id="timeline-type-error" className="error" role="alert">{t('typeTooLong')}</p> : <small className="muted">{t('typeHelp')}</small>}</div>
      </div>
      {dateError && <p className="error" role="alert">{t('invalidRange')}</p>}
      <p className="muted">{t('rangeHelp')} · {t('timezoneLabel', { timezone: display.timezone })}</p>
      <Button type="submit">{t('applyFilters')}</Button>
    </form>
    {searchText && <p className="muted" role="status">{t('searchActive', { query: searchText })} <Button type="button" className="secondary" onClick={() => router.push('/timeline')}>{t('clearSearch')}</Button></p>}
    {timeline.isPending && !appliedTypeTooLong && <div className="skeleton" aria-label={t('loading')} />}
    {timeline.isError && <p className="error" role="alert">{timeline.error instanceof ApiError ? timeline.error.message : t('loadFailed')} <Button type="button" className="secondary" onClick={() => timeline.refetch()}>{t('retry')}</Button></p>}
    {timeline.data && <>
      <p className="muted" role="status">{t('loadedCount', { count: events.length })} · {documentVersionIds.length === 0 ? t('graphStatusNoEvidence') : graphStatuses.isPending ? t('graphStatusLoading') : graphStatuses.isError ? t('graphStatusUnavailable') : t('graphStatusSummary', { count: graphStatuses.data?.length ?? 0 })}</p>
      {allDocumentVersionIds.length > 100 && <p className="muted">{t('graphStatusBound', { count: allDocumentVersionIds.length - 100 })}</p>}
      {!!graphStatuses.data?.length && <ul className="stack" aria-label={t('graphStatusDetails')}>{graphStatuses.data.slice(0, 20).map((status) => <li className={status.error_code ? 'error' : 'muted'} key={status.mapping_id}>{status.status} · {status.graph_enabled ? t('graphEnabled') : t('graphDisabled')}{status.error_code ? ` · ${status.error_code}` : ''}</li>)}</ul>}
      {(graphStatuses.data?.length ?? 0) > 20 && <p className="muted">{t('graphStatusRowsBound', { count: (graphStatuses.data?.length ?? 0) - 20 })}</p>}
      {events.length ? <div className="stack">{events.map((event) => <EventDetail key={event.id} event={event} locale={display.locale} timezone={display.timezone} entityNames={entityNames} />)}</div> : <p className="empty-state">{t('noEvents')}</p>}
      {timeline.hasNextPage && <Button type="button" className="secondary" disabled={timeline.isFetchingNextPage} onClick={() => timeline.fetchNextPage()}>{timeline.isFetchingNextPage ? t('loading') : t('loadMore')}</Button>}
      {timeline.isFetchNextPageError && <p className="error" role="alert">{t('nextPageFailed')} <Button type="button" className="secondary" onClick={() => timeline.fetchNextPage()}>{t('retry')}</Button></p>}
    </>}
  </section>;
}
