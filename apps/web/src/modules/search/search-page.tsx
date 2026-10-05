'use client';

import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { zodResolver } from '@hookform/resolvers/zod';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { useEffect } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { useTranslations } from 'next-intl';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { apiFailureKey, type ApiFailureKey } from '@/core/api-failure-key';
import { listSources, sourceKeys } from '@/modules/sources/api';
import { getSearchIndexStatus, searchDocuments, searchEntities, searchGoals, searchTasks } from './api';
import { GoalSearchResults, SearchResults, TaskSearchResults } from './search-results';

const DOCUMENT_QUERY_LIMIT = 1000;
const ENTITY_QUERY_LIMIT = 300;
const searchFailureKeys: Record<ApiFailureKey, string> = {
  unauthorized: 'searchFailureUnauthenticated', forbidden: 'searchFailureForbidden',
  conflict: 'searchFailureConflict', serviceUnavailable: 'searchFailureUnavailable',
  requestFailed: 'searchFailureRequest',
};
const indexStatusKeys: Record<string, string> = {
  queued: 'indexQueued', running: 'indexRunning', active: 'indexActive', failed: 'indexFailed',
  unavailable: 'indexUnavailableStatus',
};
/** Checks whether the query satisfies the minimum search length. */
const queryLength = (value: string) => [...value].length;
const schema = z.object({
  query: z.string().refine((value) => queryLength(value) <= DOCUMENT_QUERY_LIMIT, 'queryTooLong'),
  sourceId: z.string(),
  dateFrom: z.string(),
  dateTo: z.string(),
  contentType: z.string(),
  mode: z.enum(['lexical', 'hybrid']),
  entityScope: z.enum(['all', 'documents']),
}).refine((value) => !value.dateFrom || !value.dateTo || value.dateFrom <= value.dateTo, { path: ['dateTo'], message: 'invalidDateRange' });
type SearchForm = z.infer<typeof schema>;

/** Coordinates search query, filters, mode, and pagination for the search route. */
export function SearchPage() {
  const t = useTranslations('entities');
  const router = useRouter();
  const searchParams = useSearchParams();
  const applied = searchParams.toString();
  const params = new URLSearchParams(applied);
  const query = params.get('q')?.trim() ?? '';
  const sourceId = params.get('source') ?? '';
  const dateFrom = params.get('from') ?? '';
  const dateTo = params.get('to') ?? '';
  const contentType = params.get('type') ?? '';
  const mode = params.get('mode') === 'lexical' ? 'lexical' : 'hybrid';
  const entityScope = params.get('scope') === 'documents' ? 'documents' : 'all';
  const form = useForm<SearchForm>({
    resolver: zodResolver(schema),
    defaultValues: { query, sourceId, dateFrom, dateTo, contentType, mode, entityScope },
  });
  const { register, control, handleSubmit, reset, watch, formState: { isSubmitting, errors } } = form;
  const draftFrom = watch('dateFrom');
  useEffect(() => { reset({ query, sourceId, dateFrom, dateTo, contentType, mode, entityScope }); }, [query, sourceId, dateFrom, dateTo, contentType, mode, entityScope, reset]);

  const sources = useInfiniteQuery({ queryKey: sourceKeys.list, initialPageParam: undefined as string | undefined, queryFn: ({ pageParam }) => listSources(pageParam), getNextPageParam: (last) => last.next_cursor ?? undefined });
  const index = useQuery({ queryKey: ['search-index'], queryFn: getSearchIndexStatus, refetchInterval: 10000 });
  const filters = {
    source_ids: sourceId ? [sourceId] : [],
    date_from: dateFrom ? `${dateFrom}T00:00:00.000Z` : null,
    date_to: dateTo ? `${dateTo}T23:59:59.999Z` : null,
    content_types: contentType ? [contentType] : [],
  };
  const documentQueryValid = queryLength(query) <= DOCUMENT_QUERY_LIMIT;
  const entityQueryValid = queryLength(query) > 0 && queryLength(query) <= ENTITY_QUERY_LIMIT;
  const internalSearchEnabled = entityQueryValid && entityScope === 'all';
  const results = useInfiniteQuery({
    queryKey: ['search', query, sourceId, dateFrom, dateTo, contentType, mode],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => searchDocuments(query, filters, mode, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: query.length > 0 && documentQueryValid,
  });
  const entityResults = useInfiniteQuery({
    queryKey: ['search', 'entities', query],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => searchEntities(query, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: query.length > 0 && entityQueryValid && entityScope === 'all',
  });
  const taskResults = useInfiniteQuery({
    queryKey: ['search', 'tasks', query],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => searchTasks(query, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: internalSearchEnabled,
  });
  const goalResults = useInfiniteQuery({
    queryKey: ['search', 'goals', query],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => searchGoals(query, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: internalSearchEnabled,
  });
  const items = results.data?.pages.flatMap((page) => page.items) ?? [];
  const warnings = [...new Set(results.data?.pages.flatMap((page) => page.warnings) ?? [])];
  const tasks = taskResults.data?.pages.flatMap((page) => page.items) ?? [];
  const goals = goalResults.data?.pages.flatMap((page) => page.items) ?? [];
  const effectiveMode = results.data?.pages.at(-1)?.effective_mode;

  /** Applies the current search form values to the search route state. */
  function submit(value: SearchForm) {
    const next = new URLSearchParams();
    if (value.query.trim()) next.set('q', value.query.trim());
    if (value.sourceId) next.set('source', value.sourceId);
    if (value.dateFrom) next.set('from', value.dateFrom);
    if (value.dateTo) next.set('to', value.dateTo);
    if (value.contentType.trim()) next.set('type', value.contentType.trim());
    if (value.mode === 'lexical') next.set('mode', 'lexical');
    if (value.entityScope === 'documents') next.set('scope', 'documents');
    router.push(`/search${next.size ? `?${next}` : ''}`);
  }

  return <section className="content-panel"><span className="brand">{t('knowledgeBrand')}</span><h1>{t('searchTitle')}</h1><p className="muted">{t('searchIntro')}</p>
    <form className="form" onSubmit={handleSubmit(submit)}><div className="field"><Label htmlFor="search-query">{t('searchTerms')}</Label><Input id="search-query" data-search-query type="search" {...register('query')} aria-invalid={!!errors.query} />{errors.query && <p className="error" role="alert">{t('documentQueryBound', { limit: DOCUMENT_QUERY_LIMIT })}</p>}</div>
      <div className="search-filters"><div className="field"><Label htmlFor="search-source">{t('searchSource')}</Label><Controller control={control} name="sourceId" render={({ field }) => <Select value={field.value || 'all'} onValueChange={(value) => field.onChange(value === 'all' ? '' : value)}><SelectTrigger id="search-source"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">{t('allSources')}</SelectItem>{sources.data?.pages.flatMap((page) => page.items).map((source) => <SelectItem key={source.id} value={source.id}>{source.name}</SelectItem>)}</SelectContent></Select>} />{sources.hasNextPage && <Button type="button" className="secondary" disabled={sources.isFetchingNextPage} onClick={() => sources.fetchNextPage()}>{t('loadSources')}</Button>}{sources.isError && <span className="error" role="alert">{t('sourcesUnavailable')} <Button type="button" className="secondary" onClick={() => sources.refetch()}>{t('retrySearch')}</Button></span>}</div>
        <div className="field"><Label htmlFor="search-from">{t('searchFrom')}</Label><Input id="search-from" type="date" {...register('dateFrom')} /></div>
        <div className="field"><Label htmlFor="search-to">{t('searchTo')}</Label><Input id="search-to" type="date" min={draftFrom || undefined} {...register('dateTo')} />{errors.dateTo && <p className="error" role="alert">{t('invalidDateRange')}</p>}</div>
        <div className="field"><Label htmlFor="search-type">{t('searchContentType')}</Label><Input id="search-type" placeholder={t('searchTypePlaceholder')} {...register('contentType')} /></div>
        <div className="field"><Label htmlFor="search-mode">{t('searchMode')}</Label><Controller control={control} name="mode" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger id="search-mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="hybrid">{t('hybridMode')}</SelectItem><SelectItem value="lexical">{t('lexicalMode')}</SelectItem></SelectContent></Select>} /></div></div>
      <div className="field"><Label htmlFor="search-scope">{t('searchScope')}</Label><Controller control={control} name="entityScope" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger id="search-scope"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">{t('searchBoth')}</SelectItem><SelectItem value="documents">{t('searchDocumentsOnly')}</SelectItem></SelectContent></Select>} /></div>
      <p className="muted">{t('entityFiltersNotApplied')}</p><Button type="submit" disabled={isSubmitting}>{t('search')}</Button>
    </form>
    {index.data && <p className="muted" role="status">{t('semanticIndex')}: {t(indexStatusKeys[index.data.status] ?? 'indexUnknownStatus')} · {t('indexedItems', { count: index.data.indexed_items })} · {t('failedItems', { count: index.data.failed_items })}</p>}
    {index.isError && <p className="error" role="alert">{t('indexUnavailable')} <Button className="secondary" onClick={() => index.refetch()}>{t('retrySearch')}</Button></p>}
    {!query && <p className="empty-state">{t('enterTerms')}</p>}
    {query && !documentQueryValid && <p className="error" role="alert">{t('documentQueryBound', { limit: DOCUMENT_QUERY_LIMIT })}</p>}
    {query && documentQueryValid && results.isPending && <div className="skeleton" aria-label={t('searchingDocuments')} />}
    {query && documentQueryValid && results.isError && !results.isFetchNextPageError && <p className="error" role="alert">{results.error instanceof ApiError && results.error.status === 422 ? t('invalidSearchRequest') : t(searchFailureKeys[apiFailureKey(results.error) ?? 'requestFailed'])} <Button className="secondary" onClick={() => results.refetch()}>{t('retrySearch')}</Button></p>}
    {results.data && <><p className="muted" role="status">{t('resultsLoaded', { count: items.length })} · {effectiveMode === 'lexical' ? t('lexicalSearch') : t('hybridSearch')}</p>{warnings.map((warning) => <p className="error" role="alert" key={warning}>{t(warning === 'Semantic search unavailable' ? 'semanticSearchUnavailable' : 'searchWarningGeneric')}</p>)}{items.length ? <SearchResults items={items} /> : <p className="empty-state">{t('noDocumentResults')}</p>}{results.hasNextPage && <Button className="secondary" disabled={results.isFetchingNextPage} onClick={() => results.fetchNextPage()}>{results.isFetchingNextPage ? t('loadingResults') : t('loadResults')}</Button>}{results.isFetchNextPageError && <p className="error" role="alert">{t('loadResultsFailed')} <Button className="secondary" disabled={results.isFetchingNextPage} onClick={() => results.fetchNextPage()}>{t('retrySearch')}</Button></p>}</>}
    {query && entityScope === 'all' && <section aria-labelledby="entity-search-heading"><h2 id="entity-search-heading">{t('entitySearch')}</h2>{!entityQueryValid && <p className="muted" role="status">{t('entityQueryBound', { limit: ENTITY_QUERY_LIMIT })}</p>}{entityQueryValid && entityResults.isPending && <div className="skeleton" aria-label={t('searchingEntities')} />}{entityQueryValid && entityResults.isError && !entityResults.isFetchNextPageError && <p className="error" role="alert">{t('entitySearchFailed')} <Button className="secondary" onClick={() => entityResults.refetch()}>{t('retrySearch')}</Button></p>}{entityQueryValid && entityResults.data && (entityResults.data.pages.flatMap((page) => page.items).length ? <ul className="record-list">{entityResults.data.pages.flatMap((page) => page.items).map((item) => <li className="record-row" key={item.id}><Link href={`/knowledge/entities/${item.id}`}><strong>{item.name ?? t('unnamedEntity')}</strong></Link><p className="muted">{t(`type_${item.type}` as 'type_person')} · {t('revision')} {item.revision} · {item.aliases.length} {t('aliasesCount')}</p></li>)}</ul> : <p className="empty-state">{t('noEntityResults')}</p>)}{entityQueryValid && entityResults.hasNextPage && <Button className="secondary" disabled={entityResults.isFetchingNextPage} onClick={() => entityResults.fetchNextPage()}>{entityResults.isFetchingNextPage ? t('loadingResults') : t('loadEntities')}</Button>}{entityResults.isFetchNextPageError && <p className="error" role="alert">{t('loadEntitiesFailed')} <Button className="secondary" disabled={entityResults.isFetchingNextPage} onClick={() => entityResults.fetchNextPage()}>{t('retrySearch')}</Button></p>}</section>}
    {query && entityScope === 'all' && <section aria-labelledby="task-search-heading"><h2 id="task-search-heading">{t('searchTasks')}</h2>{!entityQueryValid && <p className="muted" role="status">{t('recordQueryBound', { limit: ENTITY_QUERY_LIMIT })}</p>}{entityQueryValid && taskResults.isPending && <><p className="muted" role="status">{t('searchingTasks')}</p><div className="skeleton" aria-hidden="true" /></>}{entityQueryValid && taskResults.isError && !taskResults.isFetchNextPageError && <p className="error" role="alert">{t('taskSearchFailed')} <Button className="secondary" onClick={() => taskResults.refetch()}>{t('retrySearch')}</Button></p>}{entityQueryValid && taskResults.data && (tasks.length ? <><p className="muted" role="status">{t('resultsLoaded', { count: tasks.length })}</p><TaskSearchResults items={tasks} /></> : <p className="empty-state">{t('noTaskResults')}</p>)}{entityQueryValid && taskResults.hasNextPage && <Button className="secondary" disabled={taskResults.isFetchingNextPage} onClick={() => taskResults.fetchNextPage()}>{taskResults.isFetchingNextPage ? t('loadingResults') : t('loadTasks')}</Button>}{taskResults.isFetchNextPageError && <p className="error" role="alert">{t('loadTasksFailed')} <Button className="secondary" disabled={taskResults.isFetchingNextPage} onClick={() => taskResults.fetchNextPage()}>{t('retrySearch')}</Button></p>}</section>}
    {query && entityScope === 'all' && <section aria-labelledby="goal-search-heading"><h2 id="goal-search-heading">{t('searchGoals')}</h2>{!entityQueryValid && <p className="muted" role="status">{t('recordQueryBound', { limit: ENTITY_QUERY_LIMIT })}</p>}{entityQueryValid && goalResults.isPending && <><p className="muted" role="status">{t('searchingGoals')}</p><div className="skeleton" aria-hidden="true" /></>}{entityQueryValid && goalResults.isError && !goalResults.isFetchNextPageError && <p className="error" role="alert">{t('goalSearchFailed')} <Button className="secondary" onClick={() => goalResults.refetch()}>{t('retrySearch')}</Button></p>}{entityQueryValid && goalResults.data && (goals.length ? <><p className="muted" role="status">{t('resultsLoaded', { count: goals.length })}</p><GoalSearchResults items={goals} /></> : <p className="empty-state">{t('noGoalResults')}</p>)}{entityQueryValid && goalResults.hasNextPage && <Button className="secondary" disabled={goalResults.isFetchingNextPage} onClick={() => goalResults.fetchNextPage()}>{goalResults.isFetchingNextPage ? t('loadingResults') : t('loadGoals')}</Button>}{goalResults.isFetchNextPageError && <p className="error" role="alert">{t('loadGoalsFailed')} <Button className="secondary" disabled={goalResults.isFetchingNextPage} onClick={() => goalResults.fetchNextPage()}>{t('retrySearch')}</Button></p>}</section>}
  </section>;
}


