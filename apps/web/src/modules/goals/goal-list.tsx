'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { fetchGoals, fetchGoalsByIds } from './api';
import type { Goal, GoalPage } from './types';
import { BareButton } from '@/components/bare-button';
import { asciiProgressBar } from './goal-progress-bar';
import './goal-skin.css';

/** One goal tile: title, localized status, summary line and a clamped 0-100 progress bar; clicking calls `onSelect`. */
function GoalCard({ goal, onSelect }: { goal: Goal; onSelect?: (goal: Goal) => void }) {
  const t = useTranslations('taskGoal');
  const goalStatusLabels = {
    active: t('goal.statusActive'),
    completed: t('goal.statusCompleted'),
    paused: t('goal.statusPaused'),
    cancelled: t('goal.statusCancelled'),
  };
  return (
    <li>
      <BareButton
        type="button"
        onClick={() => onSelect?.(goal)}
        className="goal-row w-full rounded-lg border border-border p-3 text-left hover:bg-primary/30"
      >
        <span className="goal-row-head flex justify-between gap-2">
          <strong className="truncate text-sm">{goal.title}</strong>
          <span className="text-xs text-muted-foreground">{goalStatusLabels[goal.status]}</span>
        </span>
        <span className="goal-row-summary mt-1 block text-xs text-muted-foreground">
          {t('goal.summary', { progress: goal.progress, count: goal.milestones.length, deadline: goal.deadline ?? t('no.deadline') })}
        </span>
        <span className="goal-progress mt-2 block h-1.5 overflow-hidden rounded-full bg-secondary">
          <span className="block h-full bg-primary" style={{ width: `${Math.min(100, Math.max(0, goal.progress))}%` }} />
        </span>
        {/* Terminal-skin text bar. Decorative: the summary line already states the progress. */}
        <span className="goal-ascii-bar" aria-hidden="true">
          {asciiProgressBar(goal.progress)}
        </span>
      </BareButton>
    </li>
  );
}

/**
 * Loading, load-failed (with retry), missing-id, truncated-id and empty-state messages for the goal
 * list. `emptyKind` picks the empty text: more pages exist, no goals at all, or none match filters.
 */
function GoalListNotices({
  query,
  missing,
  idsTruncatedBy,
  showEmpty,
  emptyKind,
}: {
  query: { isLoading: boolean; isError: boolean; refetch: () => unknown };
  missing: string[];
  idsTruncatedBy: number;
  showEmpty: boolean;
  emptyKind: 'page' | 'none-yet' | 'none';
}) {
  const t = useTranslations('taskGoal');
  return (
    <>
      {query.isLoading && (
        <p role="status" className="py-5 text-sm text-muted-foreground">
          {t('loadingGoals')}
        </p>
      )}
      {query.isError && (
        <div role="alert" className="text-sm text-destructive">
          {t('goal.loadFailed')}{' '}
          <Button type="button" variant="outline" size="sm" onClick={() => void query.refetch()}>
            {t('retry')}
          </Button>
        </div>
      )}
      {!!missing.length && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('missing.goalIds', { ids: missing.join(', ') })}
        </p>
      )}
      {idsTruncatedBy > 0 && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('selectedIdsTruncated', { count: idsTruncatedBy, limit: 100 })}
        </p>
      )}
      {!query.isLoading && !query.isError && showEmpty && (
        <p className="py-5 text-sm text-muted-foreground">
          {emptyKind === 'page' ? t('no.goalsThisPage') : emptyKind === 'none-yet' ? t('no.goalsYet') : t('no.goals')}
        </p>
      )}
    </>
  );
}

/** Page indicator plus Previous/Next buttons for the owner cursor pages; buttons are disabled while the next page is fetching. */
function GoalPager({
  showPageInfo,
  showNext,
  pageIndex,
  pageSize,
  fetching,
  onPrevious,
  onNext,
}: {
  showPageInfo: boolean;
  showNext: boolean;
  pageIndex: number;
  pageSize: number;
  fetching: boolean;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <>
      {showPageInfo && (
        <p className="text-xs text-muted-foreground">
          {t('ownerPageNumber', { page: pageIndex + 1 })} · {t('pageResultBound', { count: pageSize })}
        </p>
      )}
      {pageIndex > 0 && (
        <Button type="button" variant="outline" disabled={fetching} onClick={onPrevious}>
          {t('previousPage')}
        </Button>
      )}
      {showNext && (
        <Button type="button" variant="outline" disabled={fetching} onClick={onNext}>
          {fetching ? t('loading') : t('nextOwnerPage')}
        </Button>
      )}
    </>
  );
}

/** Renders one bounded owner cursor page or explicit IDs and exposes detail/create actions. */
// eslint-disable-next-line complexity -- stateful workflow whose branches mirror independent UI/request states (loading, error, conflict, per-step content); extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
export function GoalList({
  onSelectGoal,
  onCreateGoal,
  ids,
  filters,
  limit = 50,
  cacheScope = 'page',
  hideTitle = false,
}: {
  onSelectGoal?: (goal: Goal) => void;
  onCreateGoal?: () => void;
  ids?: string[];
  filters?: { keywords?: string[]; exclude_keywords?: string[] };
  limit?: number;
  cacheScope?: string;
  /** Keeps the heading for assistive tech only when a surrounding frame already shows the title. */
  hideTitle?: boolean;
}) {
  const t = useTranslations('taskGoal');
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
    queryFn: ({ pageParam }): Promise<GoalPage & { missing: string[] }> =>
      boundedIds.length
        ? pageParam
          ? Promise.resolve({ items: [], missing: [], next_cursor: null })
          : fetchGoalsByIds(boundedIds).then((rows) => ({
              items: rows.flatMap((row) => (row.goal ? [row.goal] : [])),
              missing: rows.filter((row) => row.missing).map((row) => row.id),
              next_cursor: null,
            }))
        : fetchGoals({ limit: pageSize, cursor: pageParam ?? undefined }).then((page) => ({ ...page, missing: [] as string[] })),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const ownerPage = query.data?.pages[pageIndex] ?? query.data?.pages[0];
  const goals = (ownerPage?.items ?? [])
    .filter((goal) => {
      const text = `${goal.title}\n${goal.description ?? ''}\n${goal.desired_outcome ?? ''}`.toLocaleLowerCase();
      return (
        include.every((word) => text.includes(word.toLocaleLowerCase())) &&
        exclude.every((word) => !text.includes(word.toLocaleLowerCase()))
      );
    })
    .slice(0, pageSize);
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
  return (
    <section className="flex flex-col gap-3">
      <header className="flex items-center justify-between gap-2">
        <h2 className={hideTitle ? 'sr-only' : 'text-lg font-semibold'}>{t('goalsTitle')}</h2>
        {onCreateGoal && (
          <Button size="sm" onClick={onCreateGoal}>
            {t('new.goal')}
          </Button>
        )}
      </header>
      <GoalListNotices
        query={query}
        missing={missing}
        idsTruncatedBy={(ids?.length ?? 0) - boundedIds.length}
        showEmpty={goals.length === 0}
        emptyKind={canAdvance ? 'page' : !ids && !include.length && !exclude.length ? 'none-yet' : 'none'}
      />
      <ul className="goal-list grid grid-cols-1 gap-2 md:grid-cols-2">
        {goals.map((goal) => (
          <GoalCard key={goal.id} goal={goal} onSelect={onSelectGoal} />
        ))}
      </ul>
      <GoalPager
        showPageInfo={!boundedIds.length && (pageIndex > 0 || canAdvance)}
        showNext={canAdvance && !boundedIds.length}
        pageIndex={pageIndex}
        pageSize={pageSize}
        fetching={query.isFetchingNextPage}
        onPrevious={() => setActivePage({ key: pageKey, index: pageIndex - 1 })}
        onNext={() => void loadNextOwnerPage()}
      />
    </section>
  );
}
