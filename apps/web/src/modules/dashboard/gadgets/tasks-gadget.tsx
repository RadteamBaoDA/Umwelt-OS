'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import { useWorkspace } from '@/core/workspace-context';
import { unsupportedListFilters, type GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { TaskForm } from '@/modules/tasks/task-form';
import { TaskList } from '@/modules/tasks/task-list';
import { TaskDetail } from '@/modules/tasks/task-detail';
import type { Task } from '@/modules/tasks/types';

/** Props required to render the persisted task gadget instance against owner task APIs. */
export interface TasksGadgetProps { instance: GadgetInstance }

/** Renders configured or bounded tasks, hides creation for administrator viewers, and opens task details. */
export function TasksGadget({ instance }: TasksGadgetProps) {
  const t = useTranslations('taskGoal');
  const { isAdminViewer } = useWorkspace();
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Task | null>(null);
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session') });
  const definition = instance.definition;
  const ids = definition.scope.source_item_ids ?? [];
  const filters = definition.filters;
  const unsupported = unsupportedListFilters(filters);
  const scopeKey = `${instance.id}:${definition.id}:${definition.revision}`;
  if (unsupported.length) return <GadgetNotice kind="error" message={t('unsupportedTaskFilters', { fields: unsupported.join(', ') })} />;
  if (session.isLoading) return <GadgetSkeleton variant="list" label={t('loadingSession')} />;
  if (!session.data) return <GadgetNotice kind="error" message={t('sessionUnavailable')} />;
  const token = session.data.csrfToken;
  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-3">
    {definition.source_ids.length > 0 && <p role="status" className="rounded border border-border p-2 text-xs text-muted-foreground">{t('taskSourceFilterUnavailable', { count: definition.source_ids.length })}</p>}
    {selected ? <TaskDetail task={selected} csrfToken={token} onClose={() => setSelected(null)} onTaskChanged={setSelected} /> : creating && !isAdminViewer ? <TaskForm csrfToken={token} onSuccess={() => setCreating(false)} onCancel={() => setCreating(false)} /> : <>
      <header className="flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">{instance.title || t('tasksTitle')}</h2>{!isAdminViewer && <Button type="button" size="sm" onClick={() => setCreating(true)}>{t('newTask')}</Button>}</header>
      <TaskList csrfToken={token} ids={ids.length ? ids : undefined} filters={filters} limit={filters.limit ?? 50} cacheScope={scopeKey} onSelectTask={setSelected} />
    </>}
  </section>;
}
