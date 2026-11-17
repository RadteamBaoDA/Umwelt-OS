'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { ApiError } from '@/core/api';
import { useWorkspace } from '@/core/workspace-context';
import { Button } from '@/components/ui/button';
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { deleteTask, getTask } from './api';
import { TaskForm } from './task-form';
import type { Task } from './types';
import { useResetOnChange, ownerFailureKey } from '@/modules/shared';

type DeleteIntent = { id: string; revision: number; title: string };

/** Maps each task status to its localized label. */
function useTaskStatusLabels() {
  const t = useTranslations('taskGoal');
  return {
    inbox: t('statusInbox'),
    todo: t('statusTodo'),
    in_progress: t('statusInProgress'),
    blocked: t('statusBlocked'),
    done: t('statusDone'),
    cancelled: t('statusCancelled'),
  };
}

/** Title, status and revision (once the owner read is verified) plus a Back button disabled while a write is pending. */
function TaskDetailHeader({
  current,
  ownerVerified,
  pending,
  onClose,
}: {
  current: Task;
  ownerVerified: boolean;
  pending: boolean;
  onClose?: () => void;
}) {
  const t = useTranslations('taskGoal');
  const statusLabels = useTaskStatusLabels();
  return (
    <header className="flex items-start justify-between gap-3">
      <div>
        <h2 className="text-lg font-semibold">{ownerVerified ? current.title : t('task.detailTitle')}</h2>
        {ownerVerified && (
          <p className="text-xs text-muted-foreground">
            {statusLabels[current.status]} · {t('revisionNumber', { revision: current.revision })}
          </p>
        )}
      </div>
      {onClose && (
        <Button type="button" variant="outline" disabled={pending} onClick={onClose}>
          {t('back')}
        </Button>
      )}
    </header>
  );
}

/** Read-only task description, goal reference, entity references, deadline and completion time. */
function TaskSummary({ task: current }: { task: Task }) {
  const t = useTranslations('taskGoal');
  const display = useDisplayPreferences();
  return (
    <>
      {current.description && <p className="whitespace-pre-wrap text-sm">{current.description}</p>}
      <p className="text-xs text-muted-foreground">
        {t('goal.referenceId')}: {current.goal_id ?? t('none')}
      </p>
      <p className="text-xs text-muted-foreground">
        {t('entity.references', { ids: current.entity_ids.length ? current.entity_ids.join(', ') : t('none') })}
      </p>
      <p className="text-sm">
        {t('deadline')}:{' '}
        {current.due_at ? formatDateTime(current.due_at, display.locale, display.timezone) : (current.due_date ?? t('none'))}
      </p>
      {current.completed_at && (
        <p className="text-sm">
          {t('completedAt')}: {formatDateTime(current.completed_at, display.locale, display.timezone)}
        </p>
      )}
    </>
  );
}

/** Edit and Delete task buttons sharing one `disabled` flag. */
function TaskActions({ disabled, onEdit, onDelete }: { disabled: boolean; onEdit: () => void; onDelete: () => void }) {
  const t = useTranslations('taskGoal');
  return (
    <div className="flex gap-2">
      <Button type="button" disabled={disabled} onClick={onEdit}>
        {t('editTask')}
      </Button>
      <Button type="button" variant="destructive" disabled={disabled} onClick={onDelete}>
        {t('delete.task')}
      </Button>
    </div>
  );
}

/** Loading indicator and read-failure panel (with retry) for the live task query. */
function TaskReadStatus({ query }: { query: { isFetching: boolean; isError: boolean; error: unknown; refetch: () => unknown } }) {
  const t = useTranslations('taskGoal');
  return (
    <>
      {query.isFetching && (
        <p role="status" className="text-sm text-muted-foreground">
          {t('loadingTask')}
        </p>
      )}
      {query.isError && (
        <div role="alert" className="text-sm text-destructive">
          <p>{t(ownerFailureKey(query.error, 'task.ownerMissing', 'task.readFailed'))}</p>
          <Button type="button" variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
            {query.isFetching ? t('loading') : t('retry')}
          </Button>
        </div>
      )}
    </>
  );
}

/** Verified task summary with Edit/Delete actions when `showActions` (hidden for administrator viewers). */
function TaskReadView({
  task,
  showActions,
  disabled,
  onEdit,
  onDelete,
}: {
  task: Task;
  showActions: boolean;
  disabled: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <TaskSummary task={task} />
      {showActions && <TaskActions disabled={disabled} onEdit={onEdit} onDelete={onDelete} />}
    </>
  );
}

/** Failure panel for a rejected task delete; on 409 it can load the current task and adopt its revision for a retry. */
function TaskDeleteError({
  error,
  conflict,
  refreshing,
  refreshError,
  currentTask,
  onRefresh,
  onUseRevision,
}: {
  error: unknown;
  conflict: boolean;
  refreshing: boolean;
  refreshError: string;
  currentTask: Task | null;
  onRefresh: () => void;
  onUseRevision: (task: Task) => void;
}) {
  const t = useTranslations('taskGoal');
  const statusLabels = useTaskStatusLabels();
  return (
    <div role="alert" className="text-sm text-destructive">
      <p>{conflict ? t('task.deleteConflict') : t(ownerFailureKey(error, 'task.ownerMissing', 'task.deleteFailed'))}</p>
      {conflict && (
        <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={onRefresh}>
          {refreshing ? t('loading') : t('refreshCurrentTask')}
        </Button>
      )}
      {refreshError && <p>{refreshError}</p>}
      {currentTask && (
        <div className="rounded border border-border p-2">
          <p>
            {t('current.taskRevision', {
              title: currentTask.title,
              status: statusLabels[currentTask.status],
              revision: currentTask.revision,
            })}
          </p>
          <p>
            {currentTask.description ?? t('none')} · {currentTask.due_at ?? currentTask.due_date ?? t('no.dueDate')}
          </p>
          <Button type="button" variant="outline" size="sm" onClick={() => onUseRevision(currentTask)}>
            {t('use.currentRevisionForDelete', { revision: currentTask.revision })}
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * Delete confirmation bound to a frozen `deleteIntent` (id + revision). Confirm stays disabled
 * unless the owner read is verified, nothing is `busy`, and the intent still matches `current`.
 */
function DeleteTaskDialog({
  open,
  busy,
  deleting,
  deleteIntent,
  fallbackRevision,
  current,
  ownerVerified,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  busy: boolean;
  deleting: boolean;
  deleteIntent: DeleteIntent | null;
  fallbackRevision: number;
  current: Task | undefined;
  ownerVerified: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (intent: DeleteIntent) => void;
}) {
  const t = useTranslations('taskGoal');
  const intentMatches = !!deleteIntent && deleteIntent.id === current?.id && deleteIntent.revision === current?.revision;
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('delete.taskConfirmTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('delete.taskConfirmDescription', { title: deleteIntent?.title ?? '' })} ·{' '}
            {t('revisionNumber', { revision: deleteIntent?.revision ?? fallbackRevision })}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{t('cancel')}</AlertDialogCancel>
          <Button
            type="button"
            variant="destructive"
            disabled={!ownerVerified || busy || !intentMatches}
            onClick={() => {
              if (deleteIntent && ownerVerified && current && !busy && intentMatches) onConfirm(deleteIntent);
            }}
          >
            {deleting ? t('deleting') : t('delete.task')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/**
 * Declares the task delete mutation. Success invalidates task/goal caches, drops the deleted task
 * query, closes the confirm dialog and calls `onClose`; failure only closes the dialog.
 * `conflict` is true for an HTTP 409.
 */
function useTaskDeleteMutation({
  csrfToken,
  queryClient,
  setConfirmDelete,
  setDeleteIntent,
  onClose,
}: {
  csrfToken: string;
  queryClient: QueryClient;
  setConfirmDelete: (value: boolean) => void;
  setDeleteIntent: (value: DeleteIntent | null) => void;
  onClose?: () => void;
}) {
  const remove = useMutation({
    mutationFn: (intent: DeleteIntent) => deleteTask(intent.id, intent.revision, csrfToken),
    onSuccess: (_, intent) => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      void queryClient.invalidateQueries({ queryKey: ['goal'] });
      queryClient.removeQueries({ queryKey: ['task', intent.id] });
      setConfirmDelete(false);
      setDeleteIntent(null);
      onClose?.();
    },
    onError: () => {
      setConfirmDelete(false);
      setDeleteIntent(null);
    },
  });
  const conflict = remove.error instanceof ApiError && remove.error.status === 409;
  return { remove, conflict };
}

/**
 * State for reviewing the server's current task after a conflict: `refreshCurrentTask` loads it
 * (clearing any previous snapshot and error first) and reports a localized failure in `refreshError`.
 */
function useTaskReview(taskId: string) {
  const t = useTranslations('taskGoal');
  const [currentTask, setCurrentTask] = useState<Task | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  /** Fetches the current owner projection for review without advancing a pending deletion revision. */
  const refreshCurrentTask = async () => {
    setRefreshing(true);
    setRefreshError('');
    setCurrentTask(null);
    try {
      setCurrentTask(await getTask(taskId));
    } catch (error) {
      setRefreshError(t(ownerFailureKey(error, 'task.ownerMissing', 'task.readFailed')));
    } finally {
      setRefreshing(false);
    }
  };
  return { currentTask, setCurrentTask, refreshing, refreshError, refreshCurrentTask };
}

/** Shows task details with retained refetch drafts; administrator viewers can read but cannot edit or delete. */
export function TaskDetail({
  task,
  csrfToken,
  onClose,
  onTaskChanged,
}: {
  task: Task;
  csrfToken: string;
  onClose?: () => void;
  onTaskChanged?: (task: Task) => void;
}) {
  const { isAdminViewer } = useWorkspace();
  const [editing, setEditing] = useState(false);
  const [formPending, setFormPending] = useState(false);
  const [expectedRevision, setExpectedRevision] = useState(task.revision);
  const { currentTask, setCurrentTask, refreshing, refreshError, refreshCurrentTask } = useTaskReview(task.id);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteIntent, setDeleteIntent] = useState<DeleteIntent | null>(null);
  const queryClient = useQueryClient();
  const taskQuery = useQuery({ queryKey: ['task', task.id], queryFn: () => getTask(task.id), initialData: task, initialDataUpdatedAt: 0 });
  const current = taskQuery.data;
  const ownerVerified = Boolean(current && taskQuery.dataUpdatedAt > 0 && !taskQuery.isFetching && !taskQuery.isError);
  const { remove, conflict } = useTaskDeleteMutation({ csrfToken, queryClient, setConfirmDelete, setDeleteIntent, onClose });
  useResetOnChange([ownerVerified, current, conflict, confirmDelete], () => {
    if (ownerVerified && current && !conflict && !confirmDelete) setExpectedRevision(current.revision);
  });
  return (
    <section className="flex flex-col gap-4 rounded-lg border border-border p-4">
      <TaskDetailHeader current={current} ownerVerified={ownerVerified} pending={formPending || remove.isPending} onClose={onClose} />
      <TaskReadStatus query={taskQuery} />
      {editing && !isAdminViewer && current ? (
        <TaskForm
          key={current.id}
          task={current}
          csrfToken={csrfToken}
          canSave={ownerVerified}
          onPendingChange={setFormPending}
          onSuccess={(saved) => {
            queryClient.setQueryData(['task', saved.id], saved);
            setExpectedRevision(saved.revision);
            setEditing(false);
            onTaskChanged?.(saved);
          }}
          onCancel={() => {
            if (!formPending) setEditing(false);
          }}
        />
      ) : (
        ownerVerified && (
          <TaskReadView
            task={current}
            showActions={!isAdminViewer}
            disabled={conflict || remove.isPending}
            onEdit={() => setEditing(true)}
            onDelete={() => {
              setExpectedRevision(current.revision);
              setDeleteIntent({ id: current.id, revision: current.revision, title: current.title });
              setConfirmDelete(true);
            }}
          />
        )
      )}
      {remove.error && (
        <TaskDeleteError
          error={remove.error}
          conflict={conflict}
          refreshing={refreshing}
          refreshError={refreshError}
          currentTask={currentTask}
          onRefresh={() => void refreshCurrentTask()}
          onUseRevision={(latest) => {
            queryClient.setQueryData(['task', latest.id], latest);
            setExpectedRevision(latest.revision);
            setCurrentTask(null);
            remove.reset();
          }}
        />
      )}
      {!isAdminViewer && (
        <DeleteTaskDialog
          open={confirmDelete}
          busy={remove.isPending || formPending}
          deleting={remove.isPending}
          deleteIntent={deleteIntent}
          fallbackRevision={expectedRevision}
          current={current}
          ownerVerified={ownerVerified}
          onOpenChange={(open) => {
            if (remove.isPending || formPending) return;
            setConfirmDelete(open);
            if (!open) setDeleteIntent(null);
          }}
          onConfirm={(intent) => remove.mutate(intent)}
        />
      )}
    </section>
  );
}
