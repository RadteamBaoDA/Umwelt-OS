'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ownerFailureKey } from '@/modules/shared';
import { ApiError } from '@/core/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { createTask, getTask, updateTask } from './api';
import type { Task, TaskCreate, TaskStatus } from './types';

const STATUSES: TaskStatus[] = ['inbox', 'todo', 'in_progress', 'blocked', 'done', 'cancelled'];

type TaskFieldValues = {
  title: string;
  description: string;
  goalId: string;
  entityIds: string;
  status: TaskStatus;
  dueDate: string;
  dueAt: string;
};

type TaskFieldSetters = {
  setTitle: (value: string) => void;
  setDescription: (value: string) => void;
  setGoalId: (value: string) => void;
  setEntityIds: (value: string) => void;
  setStatus: (value: TaskStatus) => void;
  setDueDate: (value: string) => void;
  setDueAt: (value: string) => void;
};

/** Controlled task inputs; the date-only deadline is disabled once a due instant is typed, and an instant without an offset shows an alert. */
function TaskFields({ values, setters }: { values: TaskFieldValues; setters: TaskFieldSetters }) {
  const t = useTranslations('taskGoal');
  const { title, description, goalId, entityIds, status, dueDate, dueAt } = values;
  const { setTitle, setDescription, setGoalId, setEntityIds, setStatus, setDueDate, setDueAt } = setters;
  const statusLabels: Record<TaskStatus, string> = {
    inbox: t('statusInbox'),
    todo: t('statusTodo'),
    in_progress: t('statusInProgress'),
    blocked: t('statusBlocked'),
    done: t('statusDone'),
    cancelled: t('statusCancelled'),
  };
  return (
    <>
      <div>
        <Label htmlFor="task-title">{t('title')}</Label>
        <Input id="task-title" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={500} />
      </div>
      <div>
        <Label htmlFor="task-description">{t('description')}</Label>
        <Input id="task-description" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={10000} />
      </div>
      <div>
        <Label htmlFor="task-goal-id">{t('goal.referenceId')}</Label>
        <Input id="task-goal-id" value={goalId} onChange={(event) => setGoalId(event.target.value)} aria-describedby="task-goal-help" />
        <p id="task-goal-help" className="text-xs text-muted-foreground">
          {t('goal.referenceHelp')}
        </p>
      </div>
      <div>
        <Label htmlFor="task-entities">{t('entity.referencesLabel')}</Label>
        <Input
          id="task-entities"
          value={entityIds}
          onChange={(event) => setEntityIds(event.target.value)}
          aria-describedby="task-entities-help"
        />
        <p id="task-entities-help" className="text-xs text-muted-foreground">
          {t('entity.referencesHelp')}
        </p>
      </div>
      <div>
        <Label htmlFor="task-status">{t('status')}</Label>
        <Select value={status} onValueChange={(value) => setStatus(value as TaskStatus)}>
          <SelectTrigger id="task-status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUSES.map((value) => (
              <SelectItem key={value} value={value}>
                {statusLabels[value]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label htmlFor="task-due-date">{t('dateOnlyDeadline')}</Label>
        <Input id="task-due-date" type="date" value={dueDate} disabled={!!dueAt} onChange={(event) => setDueDate(event.target.value)} />
      </div>
      <div>
        <Label htmlFor="task-due-at">{t('dueInstantLabel')}</Label>
        <Input
          id="task-due-at"
          type="text"
          placeholder="2026-10-04T09:30:00+07:00"
          value={dueAt}
          onChange={(event) => setDueAt(event.target.value)}
        />
      </div>
      {dueAt && !/(Z|[+-]\d{2}:\d{2})$/i.test(dueAt.trim()) && (
        <p role="alert" className="text-xs text-destructive">
          {t('dueInstantOffsetRequired')}
        </p>
      )}
    </>
  );
}

/** Failure panel for a rejected task save; on 409 it can load the current task and adopt its revision while keeping the draft. */
function TaskSaveError({
  conflict,
  saveError,
  refreshing,
  refreshError,
  currentTask,
  onRefresh,
  onUseRevision,
}: {
  conflict: boolean;
  saveError: string;
  refreshing: boolean;
  refreshError: string;
  currentTask: Task | null;
  onRefresh: () => void;
  onUseRevision: (task: Task) => void;
}) {
  const t = useTranslations('taskGoal');
  const statusLabels: Record<TaskStatus, string> = {
    inbox: t('statusInbox'),
    todo: t('statusTodo'),
    in_progress: t('statusInProgress'),
    blocked: t('statusBlocked'),
    done: t('statusDone'),
    cancelled: t('statusCancelled'),
  };
  return (
    <div role="alert" className="text-sm text-destructive">
      <p>{conflict ? t('task.conflictDraftRetained') : saveError}</p>
      {conflict && (
        <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={onRefresh}>
          {refreshing ? t('loading') : t('load.currentTask')}
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
          <p>
            {t('goal.referenceId')}: {currentTask.goal_id ?? t('none')} ·{' '}
            {t('entity.references', { ids: currentTask.entity_ids.join(', ') || t('none') })}
          </p>
          <Button type="button" variant="outline" size="sm" onClick={() => onUseRevision(currentTask)}>
            {t('use.currentRevisionKeepDraft', { revision: currentTask.revision })}
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * Declares the create/update mutation for the task form. Updates send `expectedRevision` (falling
 * back to the task's own revision) so concurrent edits surface as 409; a due instant clears the
 * date-only deadline. Success invalidates task/goal caches and calls `onSuccess`;
 * `onPendingChange` fires true on mutate and false on settle.
 */
function useTaskSaveMutation({
  task,
  csrfToken,
  values,
  expectedRevision,
  onSuccess,
  onPendingChange,
}: {
  task?: Task;
  csrfToken: string;
  values: { title: string; description: string; status: TaskStatus; goalId: string; entityIds: string; dueDate: string; dueAt: string };
  expectedRevision: number | null;
  onSuccess?: (task: Task) => void;
  onPendingChange?: (pending: boolean) => void;
}) {
  const { title, description, status, goalId, entityIds, dueDate, dueAt } = values;
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const payload: TaskCreate = {
        title: title.trim(),
        description: description.trim() || null,
        status,
        due_date: dueAt.trim() ? null : dueDate || null,
        due_at: dueAt.trim() || null,
        goal_id: goalId.trim() || null,
        entity_ids: [...new Set(entityIds.split(/[\s,]+/).filter(Boolean))],
      };
      return task
        ? updateTask(task.id, { ...payload, expected_revision: expectedRevision ?? task.revision }, csrfToken)
        : createTask(payload, csrfToken);
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
}

/** Edits or creates a retained task draft; current owner verification gates saves and pending state is reported to the parent. */
// eslint-disable-next-line complexity -- stateful form with conflict-retention and per-field validation branches that mirror independent UI/request states; extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
export function TaskForm({
  task,
  csrfToken,
  onSuccess,
  onCancel,
  canSave = true,
  onPendingChange,
}: {
  task?: Task;
  csrfToken: string;
  onSuccess?: (task: Task) => void;
  onCancel?: () => void;
  canSave?: boolean;
  onPendingChange?: (pending: boolean) => void;
}) {
  const t = useTranslations('taskGoal');
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
  const mutation = useTaskSaveMutation({
    task,
    csrfToken,
    values: { title, description, status, goalId, entityIds, dueDate, dueAt },
    expectedRevision,
    onSuccess,
    onPendingChange,
  });
  const conflict = mutation.error instanceof ApiError && mutation.error.status === 409;
  const saveError = t(ownerFailureKey(mutation.error, 'task.ownerMissing', 'task.writeFailed'));

  /** Loads the latest owner revision for review without changing the retained form draft. */
  const refreshCurrentTask = async () => {
    if (!task) return;
    setRefreshing(true);
    setRefreshError('');
    setCurrentTask(null);
    try {
      setCurrentTask(await getTask(task.id));
    } catch (error) {
      setRefreshError(t(ownerFailureKey(error, 'task.ownerMissing', 'task.readFailed')));
    } finally {
      setRefreshing(false);
    }
  };

  /** Submits one preserved draft at a time and rejects due instants without an explicit offset. */
  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const instant = dueAt.trim();
    if (!canSave || mutation.isPending || !title.trim() || (instant && !/(Z|[+-]\d{2}:\d{2})$/i.test(instant))) return;
    mutation.mutate();
  };

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3 rounded-lg border border-border p-4">
      {task && <p className="text-xs text-muted-foreground">{t('editingRevision', { revision: expectedRevision ?? task.revision })}</p>}
      <TaskFields
        values={{ title, description, goalId, entityIds, status, dueDate, dueAt }}
        setters={{ setTitle, setDescription, setGoalId, setEntityIds, setStatus, setDueDate, setDueAt }}
      />
      {mutation.error && (
        <TaskSaveError
          conflict={conflict}
          saveError={saveError}
          refreshing={refreshing}
          refreshError={refreshError}
          currentTask={currentTask}
          onRefresh={() => void refreshCurrentTask()}
          onUseRevision={(latest) => {
            setExpectedRevision(latest.revision);
            setCurrentTask(null);
            mutation.reset();
          }}
        />
      )}
      <div className="flex justify-end gap-2">
        {onCancel && (
          <Button type="button" variant="outline" disabled={mutation.isPending} onClick={onCancel}>
            {t('cancel')}
          </Button>
        )}
        <Button
          type="submit"
          disabled={!canSave || mutation.isPending || !title.trim() || (!!dueAt && !/(Z|[+-]\d{2}:\d{2})$/i.test(dueAt.trim()))}
        >
          {mutation.isPending ? t('saving') : task ? t('saveTask') : t('createTask')}
        </Button>
      </div>
    </form>
  );
}
