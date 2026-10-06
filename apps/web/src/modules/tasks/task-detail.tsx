'use client';

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { ApiError } from '@/core/api';
import { Button } from '@/components/ui/button';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { deleteTask, getTask } from './api';
import { TaskForm } from './task-form';
import type { Task } from './types';

/** Shows owner task data with retained refetch drafts; saves and frozen deletes require the current verified identity and revision. */
export function TaskDetail({ task, csrfToken, onClose, onTaskChanged }: {
  task: Task;
  csrfToken: string;
  onClose?: () => void;
  onTaskChanged?: (task: Task) => void;
}) {
  const t = useTranslations('taskGoal');
  const display = useDisplayPreferences();
  const [editing, setEditing] = useState(false);
  const [formPending, setFormPending] = useState(false);
  const [currentTask, setCurrentTask] = useState<Task | null>(null);
  const [expectedRevision, setExpectedRevision] = useState(task.revision);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteIntent, setDeleteIntent] = useState<{ id: string; revision: number; title: string } | null>(null);
  const queryClient = useQueryClient();
  const taskQuery = useQuery({ queryKey: ['task', task.id], queryFn: () => getTask(task.id), initialData: task, initialDataUpdatedAt: 0 });
  const current = taskQuery.data;
  const ownerVerified = Boolean(current && taskQuery.dataUpdatedAt > 0 && !taskQuery.isFetching && !taskQuery.isError);
  const remove = useMutation({
    mutationFn: (intent: { id: string; revision: number; title: string }) => deleteTask(intent.id, intent.revision, csrfToken),
    onSuccess: (_, intent) => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      void queryClient.invalidateQueries({ queryKey: ['goal'] });
      queryClient.removeQueries({ queryKey: ['task', intent.id] });
      setConfirmDelete(false);
      setDeleteIntent(null);
      onClose?.();
    },
    onError: () => { setConfirmDelete(false); setDeleteIntent(null); },
  });
  const conflict = remove.error instanceof ApiError && remove.error.status === 409;
  // eslint-disable-next-line react-hooks/set-state-in-effect -- syncs state to an external or prop change; reset-on-change is intentional here
  useEffect(() => { if (ownerVerified && current && !conflict && !confirmDelete) setExpectedRevision(current.revision); }, [ownerVerified, current, conflict, confirmDelete]);
  /** Fetches the current owner projection for review without advancing a pending deletion revision. */
  const refreshCurrentTask = async () => {
    setRefreshing(true);
    setRefreshError('');
    setCurrentTask(null);
    try { setCurrentTask(await getTask(task.id)); }
    catch (error) {
      setRefreshError(error instanceof ApiError && error.status === 404 ? t('taskOwnerMissing') : error instanceof ApiError && [401, 403].includes(error.status) ? t('ownerAccessDenied') : t('taskReadFailed'));
    } finally { setRefreshing(false); }
  };
  const statusLabels = { inbox: t('statusInbox'), todo: t('statusTodo'), in_progress: t('statusInProgress'), blocked: t('statusBlocked'), done: t('statusDone'), cancelled: t('statusCancelled') };
  return <section className="flex flex-col gap-4 rounded-lg border border-border p-4">
    <header className="flex items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">{ownerVerified ? current.title : t('taskDetailTitle')}</h2>{ownerVerified && <p className="text-xs text-muted-foreground">{statusLabels[current.status]} · {t('revisionNumber', { revision: current.revision })}</p>}</div>{onClose && <Button type="button" variant="outline" disabled={formPending || remove.isPending} onClick={onClose}>{t('back')}</Button>}</header>
    {taskQuery.isFetching && <p role="status" className="text-sm text-muted-foreground">{t('loadingTask')}</p>}
    {taskQuery.isError && <div role="alert" className="text-sm text-destructive"><p>{taskQuery.error instanceof ApiError && taskQuery.error.status === 404 ? t('taskOwnerMissing') : taskQuery.error instanceof ApiError && [401, 403].includes(taskQuery.error.status) ? t('ownerAccessDenied') : t('taskReadFailed')}</p><Button type="button" variant="outline" size="sm" disabled={taskQuery.isFetching} onClick={() => void taskQuery.refetch()}>{taskQuery.isFetching ? t('loading') : t('retry')}</Button></div>}
    {editing && current
      ? <TaskForm key={current.id} task={current} csrfToken={csrfToken} canSave={ownerVerified} onPendingChange={setFormPending} onSuccess={(saved) => { queryClient.setQueryData(['task', saved.id], saved); setExpectedRevision(saved.revision); setEditing(false); onTaskChanged?.(saved); }} onCancel={() => { if (!formPending) setEditing(false); }} />
      : ownerVerified && (
      <>
        {current.description && <p className="whitespace-pre-wrap text-sm">{current.description}</p>}
        <p className="text-xs text-muted-foreground">{t('goalReferenceId')}: {current.goal_id ?? t('none')}</p>
        <p className="text-xs text-muted-foreground">{t('entityReferences', { ids: current.entity_ids.length ? current.entity_ids.join(', ') : t('none') })}</p>
        <p className="text-sm">{t('deadline')}: {current.due_at ? formatDateTime(current.due_at, display.locale, display.timezone) : current.due_date ?? t('none')}</p>
        {current.completed_at && <p className="text-sm">{t('completedAt')}: {formatDateTime(current.completed_at, display.locale, display.timezone)}</p>}
        <div className="flex gap-2"><Button type="button" disabled={conflict || remove.isPending} onClick={() => setEditing(true)}>{t('editTask')}</Button><Button type="button" variant="destructive" disabled={conflict || remove.isPending} onClick={() => { setExpectedRevision(current.revision); setDeleteIntent({ id: current.id, revision: current.revision, title: current.title }); setConfirmDelete(true); }}>{t('deleteTask')}</Button></div>
      </>
      )}
    {remove.error && <div role="alert" className="text-sm text-destructive"><p>{conflict ? t('taskDeleteConflict') : remove.error instanceof ApiError && remove.error.status === 404 ? t('taskOwnerMissing') : remove.error instanceof ApiError && [401, 403].includes(remove.error.status) ? t('ownerAccessDenied') : t('taskDeleteFailed')}</p>{conflict && <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={() => void refreshCurrentTask()}>{refreshing ? t('loading') : t('refreshCurrentTask')}</Button>}{refreshError && <p>{refreshError}</p>}{currentTask && <div className="rounded border border-border p-2"><p>{t('currentTaskRevision', { title: currentTask.title, status: statusLabels[currentTask.status], revision: currentTask.revision })}</p><p>{currentTask.description ?? t('none')} · {currentTask.due_at ?? currentTask.due_date ?? t('noDueDate')}</p><Button type="button" variant="outline" size="sm" onClick={() => { queryClient.setQueryData(['task', currentTask.id], currentTask); setExpectedRevision(currentTask.revision); setCurrentTask(null); remove.reset(); }}>{t('useCurrentRevisionForDelete', { revision: currentTask.revision })}</Button></div>}</div>}
    <AlertDialog open={confirmDelete} onOpenChange={(open) => { if (remove.isPending || formPending) return; setConfirmDelete(open); if (!open) setDeleteIntent(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('deleteTaskConfirmTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteTaskConfirmDescription', { title: deleteIntent?.title ?? '' })} · {t('revisionNumber', { revision: deleteIntent?.revision ?? expectedRevision })}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={remove.isPending || formPending}>{t('cancel')}</AlertDialogCancel><Button type="button" variant="destructive" disabled={!ownerVerified || remove.isPending || formPending || !deleteIntent || deleteIntent.id !== current?.id || deleteIntent.revision !== current?.revision} onClick={() => { if (deleteIntent && ownerVerified && current && !remove.isPending && !formPending && deleteIntent.id === current.id && deleteIntent.revision === current.revision) remove.mutate(deleteIntent); }}>{remove.isPending ? t('deleting') : t('deleteTask')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </section>;
}
