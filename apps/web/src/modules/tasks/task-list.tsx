'use client';

import { useState } from 'react';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ApiError, apiRequest } from '@/core/api';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';
import { fetchTasks, fetchTasksByIds, getTask, updateTask } from './api';
import type { Task, TaskFilter, TaskPage, TaskStatus, TaskView } from './types';

const VIEWS: TaskView[] = ['inbox', 'today', 'upcoming', 'blocked', 'completed', 'all'];
const STATUSES: TaskStatus[] = ['inbox', 'todo', 'in_progress', 'blocked', 'done', 'cancelled'];

/** Displays one bounded owner cursor page at a time or bounded configured IDs, with conflict-safe completion. */
export function TaskList({
  initialView = 'all', csrfToken, onSelectTask, ids, filters, limit = 50, cacheScope = 'page',
}: {
  initialView?: TaskView;
  csrfToken: string;
  onSelectTask?: (task: Task) => void;
  ids?: string[];
  filters?: { keywords?: string[]; exclude_keywords?: string[] };
  limit?: number;
  cacheScope?: string;
}) {
  const t = useTranslations('taskGoal');
  const [view, setView] = useState<TaskView>(initialView);
  const [activePage, setActivePage] = useState<{ key: string; index: number }>({ key: '', index: 0 });
  const [conflictedTask, setConflictedTask] = useState<Task | null>(null);
  const [conflictLoadError, setConflictLoadError] = useState('');
  const queryClient = useQueryClient();
  const display = useDisplayPreferences();
  const boundedIds = ids?.slice(0, 100) ?? [];
  const normalizedFilters = { keywords: filters?.keywords ?? [], exclude: filters?.exclude_keywords ?? [] };
  const queryKey = ['tasks', view, display.timezone, cacheScope, boundedIds, normalizedFilters, limit] as const;
  const pageKey = JSON.stringify(queryKey);
  const pageIndex = activePage.key === pageKey ? activePage.index : 0;
  const { data, isLoading, isError, error, refetch, fetchNextPage, hasNextPage, isFetchingNextPage } = useInfiniteQuery({
    queryKey,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }): Promise<TaskPage & { missing: string[] }> => boundedIds.length
      ? (pageParam ? Promise.resolve({ items: [], missing: [], next_cursor: null }) : fetchTasksByIds(boundedIds).then((rows) => ({ items: rows.flatMap((row) => row.task ? [row.task] : []), missing: rows.filter((row) => row.missing).map((row) => row.id), next_cursor: null })))
      : fetchTasks({ view, timezone: display.timezone, limit: Math.min(100, Math.max(1, limit)), cursor: pageParam ?? undefined }).then((page) => ({ ...page, missing: [] as string[] })),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const toggleMutation = useMutation({
    mutationFn: ({ task, done }: { task: Task; done: boolean }) => updateTask(task.id, { status: done ? 'done' : 'todo', expected_revision: task.revision }, csrfToken),
    onSuccess: (saved) => {
      setConflictedTask(null);
      queryClient.setQueryData(['task', saved.id], saved);
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      void queryClient.invalidateQueries({ queryKey: ['goal'] });
    },
    onError: async (error, variables) => {
      setConflictedTask(null);
      setConflictLoadError('');
      if (error instanceof ApiError && error.status === 409) {
        try {
          const latest = await getTask(variables.task.id);
          setConflictedTask(latest);
          void queryClient.invalidateQueries({ queryKey });
        } catch (refreshError) { setConflictLoadError(refreshError instanceof ApiError && refreshError.status === 404 ? t('taskOwnerMissing') : refreshError instanceof ApiError && [401, 403].includes(refreshError.status) ? t('ownerAccessDenied') : t('taskReadFailed')); }
      }
    },
  });
  const localDayParts = new Intl.DateTimeFormat('en-US', { timeZone: display.timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const localDay = `${localDayParts.find((part) => part.type === 'year')?.value}-${localDayParts.find((part) => part.type === 'month')?.value}-${localDayParts.find((part) => part.type === 'day')?.value}`;
  /** Converts a due instant to its owner-selected calendar day for selected-ID view parity. */
  const dueAtLocalDay = (value: string) => {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: display.timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(value));
    return `${parts.find((part) => part.type === 'year')?.value}-${parts.find((part) => part.type === 'month')?.value}-${parts.find((part) => part.type === 'day')?.value}`;
  };
  const ownerPage = data?.pages[pageIndex] ?? data?.pages[0];
  const tasks = (ownerPage?.items ?? []).filter((task) => {
    if (boundedIds.length) {
      if (view === 'inbox' && task.status !== 'inbox') return false;
      if (view === 'blocked' && task.status !== 'blocked') return false;
      if (view === 'completed' && task.status !== 'done') return false;
      if (view === 'today' && (task.status === 'done' || task.status === 'cancelled' || !((task.due_date === localDay) || (task.due_at && dueAtLocalDay(task.due_at) === localDay)))) return false;
      if (view === 'upcoming' && (task.status === 'done' || task.status === 'cancelled' || !((task.due_date && task.due_date > localDay) || (task.due_at && dueAtLocalDay(task.due_at) > localDay)))) return false;
    }
    const text = `${task.title}\n${task.description ?? ''}`.toLocaleLowerCase();
    return normalizedFilters.keywords.every((word) => text.includes(word.toLocaleLowerCase()))
      && normalizedFilters.exclude.every((word) => !text.includes(word.toLocaleLowerCase()));
  }).slice(0, Math.min(100, Math.max(1, limit)));
  const missing = ownerPage && 'missing' in ownerPage ? ownerPage.missing : [];
  const canAdvance = pageIndex < (data?.pages.length ?? 0) - 1 || hasNextPage;
  /** Advances one owner cursor at a time so keyword filters can continue past unmatched pages without auto-draining. */
  const loadNextOwnerPage = async () => {
    if (isFetchingNextPage) return;
    if (pageIndex < (data?.pages.length ?? 0) - 1) {
      setActivePage({ key: pageKey, index: pageIndex + 1 });
      return;
    }
    const result = await fetchNextPage();
    if (!result.isError && result.data && result.data.pages.length > pageIndex + 1) setActivePage({ key: pageKey, index: pageIndex + 1 });
  };

  const viewLabels: Record<TaskView, string> = { inbox: t('viewInbox'), today: t('viewToday'), upcoming: t('viewUpcoming'), blocked: t('viewBlocked'), completed: t('viewCompleted'), all: t('viewAll') };
  const statusLabels: Record<TaskStatus, string> = { inbox: t('statusInbox'), todo: t('statusTodo'), in_progress: t('statusInProgress'), blocked: t('statusBlocked'), done: t('statusDone'), cancelled: t('statusCancelled') };
  return <section className="flex flex-col gap-3" aria-label={t('tasksTitle')}>
    <nav className="flex flex-wrap gap-1" aria-label={t('taskViews')}>{VIEWS.map((item) => <Button key={item} type="button" size="sm" variant={view === item ? 'default' : 'outline'} onClick={() => setView(item)}>{viewLabels[item]}</Button>)}</nav>
    {isLoading && <p role="status" className="py-5 text-sm text-muted-foreground">{t('loadingTasks')}</p>}
    {isError && <div role="alert" className="rounded border border-destructive/40 p-3 text-sm"><p>{error instanceof ApiError && error.status === 403 ? t('unsupportedTaskFilter') : t('taskLoadFailed')}</p><Button type="button" variant="outline" size="sm" onClick={() => void refetch()}>{t('retry')}</Button></div>}
    {!!missing.length && <p role="status" className="text-xs text-muted-foreground">{t('missingTaskIds', { ids: missing.join(', ') })}</p>}
    {boundedIds.length < (ids?.length ?? 0) && <p role="status" className="text-xs text-muted-foreground">{t('selectedIdsTruncated', { count: (ids?.length ?? 0) - boundedIds.length, limit: 100 })}</p>}
    {!isLoading && !isError && tasks.length === 0 && <p className="py-5 text-sm text-muted-foreground">{canAdvance ? t('noTasksThisPage') : t('noTasks')}</p>}
    <ul className="flex flex-col gap-2">{tasks.map((task) => <li key={task.id} className="flex items-start gap-3 rounded-lg border border-border p-3">
      <Checkbox checked={task.status === 'done'} disabled={toggleMutation.isPending} onCheckedChange={(checked) => toggleMutation.mutate({ task, done: checked === true })} aria-label={t('toggleTask', { title: task.title, state: task.status === 'done' ? t('incomplete') : t('complete') })} />
      <button type="button" className="min-w-0 flex-1 text-left" onClick={() => onSelectTask?.(task)}><span className="block truncate text-sm font-medium">{task.title}</span><span className="text-xs text-muted-foreground">{statusLabels[task.status]} · {task.due_at ? formatDateTime(task.due_at, display.locale, display.timezone) : task.due_date ?? t('noDueDate')}</span></button>
    </li>)}</ul>
    {toggleMutation.error && <div role="alert" className="text-sm text-destructive"><p>{t('taskUpdateFailed')}</p>{conflictLoadError && <p>{t('currentTaskLoadFailed')}: {conflictLoadError}</p>}{conflictedTask && <div className="rounded border border-border p-2"><p>{t('currentTaskRevision', { title: conflictedTask.title, status: statusLabels[conflictedTask.status], revision: conflictedTask.revision })}</p><Button type="button" variant="outline" size="sm" onClick={() => onSelectTask?.(conflictedTask)}>{t('reviewCurrentTask')}</Button></div>}</div>}
    {!boundedIds.length && (data?.pages.length ?? 0) > 0 && <p className="text-xs text-muted-foreground">{t('ownerPageNumber', { page: pageIndex + 1 })} · {t('pageResultBound', { count: Math.min(100, Math.max(1, limit)) })}</p>}
    {pageIndex > 0 && <Button type="button" variant="outline" disabled={isFetchingNextPage} onClick={() => setActivePage({ key: pageKey, index: pageIndex - 1 })}>{t('previousPage')}</Button>}
    {canAdvance && !boundedIds.length && <Button type="button" variant="outline" disabled={isFetchingNextPage} onClick={() => void loadNextOwnerPage()}>{isFetchingNextPage ? t('loading') : t('nextOwnerPage')}</Button>}
  </section>;
}
