'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ApiError } from '@/core/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { createTask, getTask, updateTask } from './api';
import type { Task, TaskCreate, TaskStatus } from './types';

const STATUSES: TaskStatus[] = ['inbox', 'todo', 'in_progress', 'blocked', 'done', 'cancelled'];

/** Edits or creates a retained task draft; current owner verification gates saves and pending state is reported to the parent. */
export function TaskForm({ task, csrfToken, onSuccess, onCancel, canSave = true, onPendingChange }: {
  task?: Task;
  csrfToken: string;
  onSuccess?: (task: Task) => void;
  onCancel?: () => void;
  canSave?: boolean;
  onPendingChange?: (pending: boolean) => void;
}) {
  const t = useTranslations('taskGoal');
  const statusLabels: Record<TaskStatus, string> = { inbox: t('statusInbox'), todo: t('statusTodo'), in_progress: t('statusInProgress'), blocked: t('statusBlocked'), done: t('statusDone'), cancelled: t('statusCancelled') };
  const [title, setTitle] = useState(task?.title ?? '');
  const [description, setDescription] = useState(task?.description ?? '');
  const [status, setStatus] = useState<TaskStatus>(task?.status ?? 'todo');
  const [goalId, setGoalId] = useState(task?.goal_id ?? '');
  const [entityIds, setEntityIds] = useState(task?.entity_ids.join(', ') ?? '');
  const [expectedRevision, setExpectedRevision] = useState(task?.revision ?? null);
  const [dueDate, setDueDate] = useState(task?.due_date ?? '');
  const [dueAt, setDueAt] = useState(task?.due_at ?? '');
  const [currentTask, setCurrentTask] = useState<Task | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: async () => {
      const payload: TaskCreate = { title: title.trim(), description: description.trim() || null, status, due_date: dueAt.trim() ? null : dueDate || null, due_at: dueAt.trim() || null, goal_id: goalId.trim() || null, entity_ids: [...new Set(entityIds.split(/[\s,]+/).filter(Boolean))] };
      return task ? updateTask(task.id, { ...payload, expected_revision: expectedRevision ?? task.revision }, csrfToken) : createTask(payload, csrfToken);
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      void queryClient.invalidateQueries({ queryKey: ['goal'] });
      queryClient.setQueryData(['task', saved.id], saved);
      onSuccess?.(saved);
    },
    onMutate: () => onPendingChange?.(true),
    onSettled: () => onPendingChange?.(false),
  });
  const conflict = mutation.error instanceof ApiError && mutation.error.status === 409;
  const saveError = mutation.error instanceof ApiError && mutation.error.status === 404 ? t('taskOwnerMissing') : mutation.error instanceof ApiError && [401, 403].includes(mutation.error.status) ? t('ownerAccessDenied') : t('taskWriteFailed');

  /** Loads the latest owner revision for review without changing the retained form draft. */
  const refreshCurrentTask = async () => {
    if (!task) return;
    setRefreshing(true);
    setRefreshError('');
    setCurrentTask(null);
    try { setCurrentTask(await getTask(task.id)); }
    catch (error) { setRefreshError(error instanceof ApiError && error.status === 404 ? t('taskOwnerMissing') : error instanceof ApiError && [401, 403].includes(error.status) ? t('ownerAccessDenied') : t('taskReadFailed')); }
    finally { setRefreshing(false); }
  };

  /** Submits one preserved draft at a time and rejects due instants without an explicit offset. */
  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const instant = dueAt.trim();
    if (!canSave || mutation.isPending || !title.trim() || (instant && !/(Z|[+-]\d{2}:\d{2})$/i.test(instant))) return;
    mutation.mutate();
  };

  return <form onSubmit={handleSubmit} className="flex flex-col gap-3 rounded-lg border border-border p-4">
    {task && <p className="text-xs text-muted-foreground">{t('editingRevision', { revision: expectedRevision ?? task.revision })}</p>}
    <div><Label htmlFor="task-title">{t('title')}</Label><Input id="task-title" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={500} /></div>
    <div><Label htmlFor="task-description">{t('description')}</Label><Input id="task-description" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={10000} /></div>
    <div><Label htmlFor="task-goal-id">{t('goalReferenceId')}</Label><Input id="task-goal-id" value={goalId} onChange={(event) => setGoalId(event.target.value)} aria-describedby="task-goal-help" /><p id="task-goal-help" className="text-xs text-muted-foreground">{t('goalReferenceHelp')}</p></div>
    <div><Label htmlFor="task-entities">{t('entityReferencesLabel')}</Label><Input id="task-entities" value={entityIds} onChange={(event) => setEntityIds(event.target.value)} aria-describedby="task-entities-help" /><p id="task-entities-help" className="text-xs text-muted-foreground">{t('entityReferencesHelp')}</p></div>
    <div><Label htmlFor="task-status">{t('status')}</Label><Select value={status} onValueChange={(value) => setStatus(value as TaskStatus)}><SelectTrigger id="task-status"><SelectValue /></SelectTrigger><SelectContent>{STATUSES.map((value) => <SelectItem key={value} value={value}>{statusLabels[value]}</SelectItem>)}</SelectContent></Select></div>
    <div><Label htmlFor="task-due-date">{t('dateOnlyDeadline')}</Label><Input id="task-due-date" type="date" value={dueDate} disabled={!!dueAt} onChange={(event) => setDueDate(event.target.value)} /></div>
    <div><Label htmlFor="task-due-at">{t('dueInstantLabel')}</Label><Input id="task-due-at" type="text" placeholder="2026-10-04T09:30:00+07:00" value={dueAt} onChange={(event) => setDueAt(event.target.value)} /></div>
    {dueAt && !/(Z|[+-]\d{2}:\d{2})$/i.test(dueAt.trim()) && <p role="alert" className="text-xs text-destructive">{t('dueInstantOffsetRequired')}</p>}
    {mutation.error && <div role="alert" className="text-sm text-destructive"><p>{conflict ? t('taskConflictDraftRetained') : saveError}</p>{conflict && <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={() => void refreshCurrentTask()}>{refreshing ? t('loading') : t('loadCurrentTask')}</Button>}{refreshError && <p>{refreshError}</p>}{currentTask && <div className="rounded border border-border p-2"><p>{t('currentTaskRevision', { title: currentTask.title, status: statusLabels[currentTask.status], revision: currentTask.revision })}</p><p>{currentTask.description ?? t('none')} · {currentTask.due_at ?? currentTask.due_date ?? t('noDueDate')}</p><p>{t('goalReferenceId')}: {currentTask.goal_id ?? t('none')} · {t('entityReferences', { ids: currentTask.entity_ids.join(', ') || t('none') })}</p><Button type="button" variant="outline" size="sm" onClick={() => { setExpectedRevision(currentTask.revision); setCurrentTask(null); mutation.reset(); }}>{t('useCurrentRevisionKeepDraft', { revision: currentTask.revision })}</Button></div>}</div>}
    <div className="flex justify-end gap-2">{onCancel && <Button type="button" variant="outline" disabled={mutation.isPending} onClick={onCancel}>{t('cancel')}</Button>}<Button type="submit" disabled={!canSave || mutation.isPending || !title.trim() || (!!dueAt && !/(Z|[+-]\d{2}:\d{2})$/i.test(dueAt.trim()))}>{mutation.isPending ? t('saving') : task ? t('saveTask') : t('createTask')}</Button></div>
  </form>;
}
