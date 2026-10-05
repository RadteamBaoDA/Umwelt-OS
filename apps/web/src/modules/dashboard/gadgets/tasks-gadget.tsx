'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import type { GadgetInstance } from '../api';
import { TaskForm } from '@/modules/tasks/task-form';
import { TaskList } from '@/modules/tasks/task-list';
import { TaskDetail } from '@/modules/tasks/task-detail';
import type { Task } from '@/modules/tasks/types';

/** Props required to render the persisted task gadget instance against owner task APIs. */
export interface TasksGadgetProps { instance: GadgetInstance }

/** Renders configured task IDs or a bounded owner task view with create/detail compositions. */
export function TasksGadget({ instance }: TasksGadgetProps) {
  const t = useTranslations('taskGoal');
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Task | null>(null);
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session') });
  const definition = instance.definition;
  const ids = definition.scope.source_item_ids ?? [];
  const filters = definition.filters;
  const unsupported = Object.keys(filters).filter((key) => !['keywords', 'exclude_keywords', 'limit'].includes(key));
  const scopeKey = `${instance.id}:${definition.id}:${definition.revision}`;
  if (unsupported.length) return <div role="status" className="p-3 text-sm text-destructive">{t('unsupportedTaskFilters', { fields: unsupported.join(', ') })}</div>;
  if (session.isLoading) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('loadingSession')}</p>;
  if (!session.data) return <p role="alert" className="p-3 text-sm text-destructive">{t('sessionUnavailable')}</p>;
  const token = session.data.csrfToken;
  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-3">
    {definition.source_ids.length > 0 && <p role="status" className="rounded border border-border p-2 text-xs text-muted-foreground">{t('taskSourceFilterUnavailable', { count: definition.source_ids.length })}</p>}
    {selected ? <TaskDetail task={selected} csrfToken={token} onClose={() => setSelected(null)} onTaskChanged={setSelected} /> : creating ? <TaskForm csrfToken={token} onSuccess={() => setCreating(false)} onCancel={() => setCreating(false)} /> : <>
      <header className="flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">{instance.title || t('tasksTitle')}</h2><Button type="button" size="sm" onClick={() => setCreating(true)}>{t('newTask')}</Button></header>
      <TaskList csrfToken={token} ids={ids.length ? ids : undefined} filters={filters} limit={filters.limit ?? 50} cacheScope={scopeKey} onSelectTask={setSelected} />
    </>}
  </section>;
}
