'use client';

import { useState } from 'react';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ApiError } from '@/core/api';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';
import { useWorkspace } from '@/core/workspace-context';
import { fetchTasks, fetchTasksByIds, getTask, updateTask } from './api';
import type { Task, TaskPage, TaskStatus, TaskView } from './types';
import { BareButton } from '@/components/bare-button';
import { getDateTimeFormat, ownerFailureKey } from '@/modules/shared';

const VIEWS: TaskView[] = ['inbox', 'today', 'upcoming', 'blocked', 'completed', 'all'];

/** YYYY-MM-DD calendar day of an instant in the given IANA zone (en-US parts, so independent of the UI locale). */
function calendarDay(instant: Date, timezone: string): string {
  const parts = getDateTimeFormat('en-US', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(instant);
  const part = (type: string) => parts.find((item) => item.type === type)?.value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}

/** True for tasks that are neither done nor cancelled. */
function isOpenTask(task: Task): boolean {
  return task.status !== 'done' && task.status !== 'cancelled';
}

/** Whether a task is due on the given calendar day (date-only due date, or due instant converted to that zone's day). */
function dueOn(task: Task, localDay: string, dueAtLocalDay: (value: string) => string): boolean {
  return task.due_date === localDay || Boolean(task.due_at && dueAtLocalDay(task.due_at) === localDay);
}

/** Whether a task is due after the given calendar day. */
function dueAfter(task: Task, localDay: string, dueAtLocalDay: (value: string) => string): boolean {
  return Boolean(task.due_date && task.due_date > localDay) || Boolean(task.due_at && dueAtLocalDay(task.due_at) > localDay);
}

/** Applies the selected view to explicitly selected task IDs, which the server cannot filter by view. */
function matchesSelectedView(task: Task, view: TaskView, localDay: string, dueAtLocalDay: (value: string) => string): boolean {
  switch (view) {
    case 'inbox': return task.status === 'inbox';
    case 'blocked': return task.status === 'blocked';
    case 'completed': return task.status === 'done';
    case 'today': return isOpenTask(task) && dueOn(task, localDay, dueAtLocalDay);
    case 'upcoming': return isOpenTask(task) && dueAfter(task, localDay, dueAtLocalDay);
    default: return true;
  }
}

/** Case-insensitive keyword filter over title and description: every include word present, no exclude word present. */
function matchesKeywords(task: Task, filters: { keywords: string[]; exclude: string[] }): boolean {
  const text = `${task.title}\n${task.description ?? ''}`.toLocaleLowerCase();
  return filters.keywords.every((word) => text.includes(word.toLocaleLowerCase()))
    && filters.exclude.every((word) => !text.includes(word.toLocaleLowerCase()));
}

/** Props of the task list: initial view, CSRF token, optional selection callback, explicit IDs, keyword filters, page size and cache scope. */
type TaskListProps = {
  initialView?: TaskView;
  csrfToken: string;
  onSelectTask?: (task: Task) => void;
  ids?: string[];
  filters?: { keywords?: string[]; exclude_keywords?: string[] };
  limit?: number;
  cacheScope?: string;
};

/** Displays bounded task pages or configured IDs; completion remains conflict-safe and disabled for administrator viewers. */
export function TaskList(props: TaskListProps) {
  const s0 = useTaskListQuery(props);
  const { t } = s0;
  const s1 = useTaskToggle(s0);

  const s2 = deriveTaskListView(s1);

  const vm: TaskListVm = s2;
  return <section className="flex flex-col gap-3" aria-label={t('tasksTitle')}>
<TaskViewSelect vm={vm} />
<TaskListNotices vm={vm} />
<TaskRows vm={vm} />
<TaskListFooter vm={vm} />
  </section>;
}

type TaskListVm = ReturnType<typeof deriveTaskListView>;

/** Task view selector. */
function TaskViewSelect({ vm }: { vm: TaskListVm }) {
  const { t, view, setView, viewLabels } = vm;
  return (
    <Select value={view} onValueChange={(next) => setView(next as TaskView)}>
      <SelectTrigger aria-label={t('taskViews')} className="w-full sm:w-48"><SelectValue /></SelectTrigger>
      <SelectContent>{VIEWS.map((item) => <SelectItem key={item} value={item}>{viewLabels[item]}</SelectItem>)}</SelectContent>
    </Select>
  );
}

/** Loading, error, missing-ID, truncation and empty notices. */
// eslint-disable-next-line complexity -- JSX presentational markup: the score counts every conditional-render branch (&&, ?:, ??) of this panel; it holds no branching business logic, so splitting would only relocate the same render conditions
function TaskListNotices({ vm }: { vm: TaskListVm }) {
  const { t, boundedIds, normalizedFilters, view, ids, isLoading, isError, error, refetch, tasks, missing, canAdvance } = vm;
  return (
<>
    {isLoading && <p role="status" className="py-5 text-sm text-muted-foreground">{t('loadingTasks')}</p>}
    {isError && <div role="alert" className="rounded border border-destructive/40 p-3 text-sm"><p>{error instanceof ApiError && error.status === 403 ? t('unsupportedTaskFilter') : t('taskLoadFailed')}</p><Button type="button" variant="outline" size="sm" onClick={() => void refetch()}>{t('retry')}</Button></div>}
    {!!missing.length && <p role="status" className="text-xs text-muted-foreground">{t('missingTaskIds', { ids: missing.join(', ') })}</p>}
    {boundedIds.length < (ids?.length ?? 0) && <p role="status" className="text-xs text-muted-foreground">{t('selectedIdsTruncated', { count: (ids?.length ?? 0) - boundedIds.length, limit: 100 })}</p>}
    {!isLoading && !isError && tasks.length === 0 && <p className="py-5 text-sm text-muted-foreground">{canAdvance ? t('noTasksThisPage') : view === 'all' && !ids && !normalizedFilters.keywords.length && !normalizedFilters.exclude.length ? t('noTasksYet') : t('noTasks')}</p>}
</>
  );
}

/** Task rows with completion checkbox and select action. */
function TaskRows({ vm }: { vm: TaskListVm }) {
  const { t, display, onSelectTask, isAdminViewer, toggleMutation, tasks, statusLabels } = vm;
  return (
    <ul className="flex flex-col gap-2">{tasks.map((task) => <li key={task.id} className="flex items-start gap-3 rounded-lg border border-border p-3">
      <Checkbox checked={task.status === 'done'} disabled={isAdminViewer || toggleMutation.isPending} onCheckedChange={(checked) => toggleMutation.mutate({ task, done: checked === true })} aria-label={t('toggleTask', { title: task.title, state: task.status === 'done' ? t('incomplete') : t('complete') })} />
      <BareButton type="button" className="min-w-0 flex-1 text-left" onClick={() => onSelectTask?.(task)}><span className="block truncate text-sm font-medium">{task.title}</span><span className="text-xs text-muted-foreground">{statusLabels[task.status]} · {task.due_at ? formatDateTime(task.due_at, display.locale, display.timezone) : task.due_date ?? t('noDueDate')}</span></BareButton>
    </li>)}</ul>
  );
}

/** Update error with conflict details and owner-page navigation. */
function TaskListFooter({ vm }: { vm: TaskListVm }) {
  const { t, boundedIds, pageKey, pageIndex, setActivePage, conflictedTask, conflictLoadError, onSelectTask, limit, isFetchingNextPage, toggleMutation, canAdvance, loadNextOwnerPage, statusLabels } = vm;
  return (
<>
    {toggleMutation.error && <div role="alert" className="text-sm text-destructive"><p>{t('taskUpdateFailed')}</p>{conflictLoadError && <p>{t('currentTaskLoadFailed')}: {conflictLoadError}</p>}{conflictedTask && <div className="rounded border border-border p-2"><p>{t('currentTaskRevision', { title: conflictedTask.title, status: statusLabels[conflictedTask.status], revision: conflictedTask.revision })}</p><Button type="button" variant="outline" size="sm" onClick={() => onSelectTask?.(conflictedTask)}>{t('reviewCurrentTask')}</Button></div>}</div>}
    {!boundedIds.length && (pageIndex > 0 || canAdvance) && <p className="text-xs text-muted-foreground">{t('ownerPageNumber', { page: pageIndex + 1 })} · {t('pageResultBound', { count: Math.min(100, Math.max(1, limit)) })}</p>}
    {pageIndex > 0 && <Button type="button" variant="outline" disabled={isFetchingNextPage} onClick={() => setActivePage({ key: pageKey, index: pageIndex - 1 })}>{t('previousPage')}</Button>}
    {canAdvance && !boundedIds.length && <Button type="button" variant="outline" disabled={isFetchingNextPage} onClick={() => void loadNextOwnerPage()}>{isFetchingNextPage ? t('loading') : t('nextOwnerPage')}</Button>}
</>
  );
}

/** Holds the selected view and owner-page cursor and loads task pages (selected IDs or a server view) for the fixed page key. */
function useTaskListQuery(props: TaskListProps) {
  const { initialView = 'all', csrfToken, onSelectTask, ids, filters, limit = 50, cacheScope = 'page' } = props;
  const t = useTranslations('taskGoal');
  const { isAdminViewer } = useWorkspace();
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

  return { t, queryClient, display, boundedIds, normalizedFilters, queryKey, pageKey, pageIndex, view, setView, activePage, setActivePage, conflictedTask, setConflictedTask, conflictLoadError, setConflictLoadError, initialView, csrfToken, onSelectTask, ids, filters, limit, cacheScope, isAdminViewer, data, isLoading, isError, error, refetch, fetchNextPage, hasNextPage, isFetchingNextPage };
}

/** Declares the completion toggle; a 409 reloads the latest task for conflict display and refreshes the list. */
function useTaskToggle(p: ReturnType<typeof useTaskListQuery>) {
  const { t, queryClient, queryKey, setConflictedTask, setConflictLoadError, csrfToken } = p;
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
        } catch (refreshError) { setConflictLoadError(t(ownerFailureKey(refreshError, 'taskOwnerMissing', 'taskReadFailed'))); }
      }
    },
  });

  return { ...p, toggleMutation };
}

/** Derives the visible tasks for the current owner page, paging helpers and localized labels. */
function deriveTaskListView(p: ReturnType<typeof useTaskToggle>) {
  const { t, display, boundedIds, normalizedFilters, pageKey, pageIndex, view, setActivePage, limit, data, fetchNextPage, hasNextPage, isFetchingNextPage } = p;
  const localDay = calendarDay(new Date(), display.timezone);
  /** Converts a due instant to its owner-selected calendar day for selected-ID view parity. */
  const dueAtLocalDay = (value: string) => calendarDay(new Date(value), display.timezone);
  const ownerPage = data?.pages[pageIndex] ?? data?.pages[0];
  const tasks = (ownerPage?.items ?? []).filter((task) => (!boundedIds.length || matchesSelectedView(task, view, localDay, dueAtLocalDay))
    && matchesKeywords(task, normalizedFilters)).slice(0, Math.min(100, Math.max(1, limit)));
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

  return { ...p, localDay, dueAtLocalDay, ownerPage, tasks, missing, canAdvance, loadNextOwnerPage, viewLabels, statusLabels };
}
