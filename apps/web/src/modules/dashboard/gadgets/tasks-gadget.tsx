'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { useDisplayPreferences } from '@/core/query-provider';
import { useWorkspace } from '@/core/workspace-context';
import { unsupportedListFilters, type GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { TaskForm } from '@/modules/tasks/task-form';
import { TaskList } from '@/modules/tasks/task-list';
import { TaskDetail } from '@/modules/tasks/task-detail';
import { fetchTasks } from '@/modules/tasks/api';
import type { Task } from '@/modules/tasks/types';

/** Props required to render the persisted task gadget instance against owner task APIs. */
export interface TasksGadgetProps { instance: GadgetInstance }

/** Renders configured or bounded tasks, hides creation for administrator viewers, and opens task details. */
export function TasksGadget({ instance }: TasksGadgetProps) {
  const t = useTranslations('taskGoal');
  const tDash = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const { isAdminViewer } = useWorkspace();
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Task | null>(null);
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session') });
  const definition = instance.definition;
  // One bounded read feeds the "N due today · M overdue" line; the list below keeps its own query.
  const summaryQuery = useQuery({
    queryKey: ['tasks-gadget-summary', display.timezone],
    queryFn: () => fetchTasks({ view: 'all', limit: 100, timezone: display.timezone }),
    staleTime: 30_000,
  });
  const ids = definition.scope.source_item_ids ?? [];
  const filters = definition.filters;
  const unsupported = unsupportedListFilters(filters);
  const scopeKey = `${instance.id}:${definition.id}:${definition.revision}`;
  if (unsupported.length) return <GadgetNotice kind="error" message={t('unsupportedTaskFilters', { fields: unsupported.join(', ') })} />;
  if (session.isLoading) return <GadgetSkeleton variant="list" label={t('loadingSession')} />;
  if (!session.data) return <GadgetNotice kind="error" message={t('sessionUnavailable')} />;
  const token = session.data.csrfToken;
  // Day keys are compared as YYYY-MM-DD in the owner's timezone so "today" matches what the owner sees.
  const dayFormat = new Intl.DateTimeFormat('en-CA', { timeZone: display.timezone });
  const today = dayFormat.format(new Date());
  const open = (summaryQuery.data?.items ?? []).filter((task) => task.status !== 'done' && task.status !== 'cancelled');
  /** Calendar day a task is due: its date-only `due_date`, else `due_at` rendered in the owner's timezone; null when undated. */
  const dueDay = (task: Task) => task.due_date ?? (task.due_at ? dayFormat.format(new Date(task.due_at)) : null);
  const dueToday = open.filter((task) => dueDay(task) === today).length;
  const overdue = open.filter((task) => { const day = dueDay(task); return day !== null && day < today; }).length;
  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-3">
    {definition.source_ids.length > 0 && <p role="status" className="rounded border border-border p-2 text-xs text-muted-foreground">{t('taskSourceFilterUnavailable', { count: definition.source_ids.length })}</p>}
    {selected ? <TaskDetail task={selected} csrfToken={token} onClose={() => setSelected(null)} onTaskChanged={setSelected} /> : creating && !isAdminViewer ? <TaskForm csrfToken={token} onSuccess={() => setCreating(false)} onCancel={() => setCreating(false)} /> : <>
      {/* The gadget frame already shows the title; only the create action remains. */}
      <header className="flex items-center justify-between gap-2">
        <p className="m-0 text-sm tabular-nums text-muted-foreground">{summaryQuery.data ? tDash('tasksDueSummary', { due: dueToday, overdue }) : ''}</p>
        {!isAdminViewer && <Button type="button" size="sm" onClick={() => setCreating(true)}>{t('newTask')}</Button>}
      </header>
      <TaskList csrfToken={token} ids={ids.length ? ids : undefined} filters={filters} limit={filters.limit ?? 50} cacheScope={scopeKey} onSelectTask={setSelected} />
    </>}
  </section>;
}
