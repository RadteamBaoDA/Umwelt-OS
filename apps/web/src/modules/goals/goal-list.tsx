'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { fetchGoals, fetchGoalsByIds } from './api';
import type { Goal, GoalPage } from './types';

/** Renders one bounded owner cursor page or explicit IDs and exposes detail/create actions. */
export function GoalList({ onSelectGoal, onCreateGoal, ids, filters, limit = 50, cacheScope = 'page' }: {
  onSelectGoal?: (goal: Goal) => void;
  onCreateGoal?: () => void;
  ids?: string[];
  filters?: { keywords?: string[]; exclude_keywords?: string[] };
  limit?: number;
  cacheScope?: string;
}) {
  const t = useTranslations('taskGoal');
  const goalStatusLabels = { active: t('goalStatusActive'), completed: t('goalStatusCompleted'), paused: t('goalStatusPaused'), cancelled: t('goalStatusCancelled') };
  const boundedIds = ids?.slice(0, 100) ?? [];
  const include = filters?.keywords ?? [];
  const exclude = filters?.exclude_keywords ?? [];
  const pageSize = Math.min(100, Math.max(1, limit));
  const queryKey = ['goals', cacheScope, boundedIds, include, exclude, pageSize] as const;
  const pageKey = JSON.stringify(queryKey);
  const [activePage, setActivePage] = useState<{ key: string; index: number }>({ key: '', index: 0 });
  const pageIndex = activePage.key === pageKey ? activePage.index : 0;
  const query = useInfiniteQuery({
    queryKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }): Promise<GoalPage & { missing: string[] }> => boundedIds.length
      ? (pageParam ? Promise.resolve({ items: [], missing: [], next_cursor: null }) : fetchGoalsByIds(boundedIds).then((rows) => ({ items: rows.flatMap((row) => row.goal ? [row.goal] : []), missing: rows.filter((row) => row.missing).map((row) => row.id), next_cursor: null })))
      : fetchGoals({ limit: pageSize, cursor: pageParam ?? undefined }).then((page) => ({ ...page, missing: [] as string[] })),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const ownerPage = query.data?.pages[pageIndex] ?? query.data?.pages[0];
  const goals = (ownerPage?.items ?? []).filter((goal) => {
    const text = `${goal.title}\n${goal.description ?? ''}\n${goal.desired_outcome ?? ''}`.toLocaleLowerCase();
    return include.every((word) => text.includes(word.toLocaleLowerCase())) && exclude.every((word) => !text.includes(word.toLocaleLowerCase()));
  }).slice(0, pageSize);
  const missing = ownerPage && 'missing' in ownerPage ? ownerPage.missing : [];
  const canAdvance = pageIndex < (query.data?.pages.length ?? 0) - 1 || query.hasNextPage;
  /** Advances a single cursor after explicit input so keyword filtering can reach later goal pages safely. */
  const loadNextOwnerPage = async () => {
    if (query.isFetchingNextPage) return;
    if (pageIndex < (query.data?.pages.length ?? 0) - 1) {
      setActivePage({ key: pageKey, index: pageIndex + 1 });
      return;
    }
    const result = await query.fetchNextPage();
    if (!result.isError && result.data && result.data.pages.length > pageIndex + 1) setActivePage({ key: pageKey, index: pageIndex + 1 });
  };
  return <section className="flex flex-col gap-3">
    <header className="flex items-center justify-between gap-2"><h2 className="text-lg font-semibold">{t('goalsTitle')}</h2>{onCreateGoal && <Button size="sm" onClick={onCreateGoal}>{t('newGoal')}</Button>}</header>
    {query.isLoading && <p role="status" className="py-5 text-sm text-muted-foreground">{t('loadingGoals')}</p>}
    {query.isError && <div role="alert" className="text-sm text-destructive">{t('goalLoadFailed')} <Button type="button" variant="outline" size="sm" onClick={() => void query.refetch()}>{t('retry')}</Button></div>}
    {!!missing.length && <p role="status" className="text-xs text-muted-foreground">{t('missingGoalIds', { ids: missing.join(', ') })}</p>}
    {boundedIds.length < (ids?.length ?? 0) && <p role="status" className="text-xs text-muted-foreground">{t('selectedIdsTruncated', { count: (ids?.length ?? 0) - boundedIds.length, limit: 100 })}</p>}
    {!query.isLoading && !query.isError && goals.length === 0 && <p className="py-5 text-sm text-muted-foreground">{canAdvance ? t('noGoalsThisPage') : t('noGoals')}</p>}
    <ul className="grid grid-cols-1 gap-2 md:grid-cols-2">{goals.map((goal) => <li key={goal.id}><button type="button" onClick={() => onSelectGoal?.(goal)} className="w-full rounded-lg border border-border p-3 text-left hover:bg-primary/30"><span className="flex justify-between gap-2"><strong className="truncate text-sm">{goal.title}</strong><span className="text-xs text-muted-foreground">{goalStatusLabels[goal.status]}</span></span><span className="mt-1 block text-xs text-muted-foreground">{t('goalSummary', { progress: goal.progress, count: goal.milestones.length, deadline: goal.deadline ?? t('noDeadline') })}</span><span className="mt-2 block h-1.5 overflow-hidden rounded-full bg-secondary"><span className="block h-full bg-primary" style={{ width: `${Math.min(100, Math.max(0, goal.progress))}%` }} /></span></button></li>)}</ul>
    {!boundedIds.length && (query.data?.pages.length ?? 0) > 0 && <p className="text-xs text-muted-foreground">{t('ownerPageNumber', { page: pageIndex + 1 })} · {t('pageResultBound', { count: pageSize })}</p>}
    {pageIndex > 0 && <Button type="button" variant="outline" disabled={query.isFetchingNextPage} onClick={() => setActivePage({ key: pageKey, index: pageIndex - 1 })}>{t('previousPage')}</Button>}
    {canAdvance && !boundedIds.length && <Button type="button" variant="outline" disabled={query.isFetchingNextPage} onClick={() => void loadNextOwnerPage()}>{query.isFetchingNextPage ? t('loading') : t('nextOwnerPage')}</Button>}
  </section>;
}
