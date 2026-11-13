'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { ApiError } from '@/core/api';
import { useTranslations } from 'next-intl';
import { useWorkspace } from '@/core/workspace-context';
import { Button } from '@/components/ui/button';
import { deleteGoal, getGoal, updateGoal } from './api';
import { CurrentGoalReview } from './goal-current-review';
import { GoalForm } from './goal-form';
import type { Goal } from './types';
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Checkbox } from '@/components/ui/checkbox';
import { useResetOnChange, ownerFailureKey } from '@/modules/shared';

/**
 * Milestone checklist. Task-linked milestones are read-only with an "open task" button; the rest
 * toggle via `onToggle(id, completed)`. The parent decides write eligibility and does the mutation.
 */
function MilestoneList({
  milestones,
  disabledToggle,
  disabledOpen,
  onOpenTask,
  onToggle,
}: {
  milestones: Goal['milestones'];
  disabledToggle: boolean;
  disabledOpen: boolean;
  onOpenTask: (taskId: string) => void;
  onToggle: (milestoneId: string, completed: boolean) => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <div>
      <h3 className="mb-2 text-sm font-medium">{t('milestones')}</h3>
      {milestones.length ? (
        <ul className="flex flex-col gap-2">
          {milestones.map((milestone) => (
            <li key={milestone.id} className="flex items-center gap-2 rounded border border-border p-2 text-sm">
              {milestone.task_id ? (
                <>
                  <Checkbox checked={milestone.completed} disabled aria-label={t('linked.completionAria', { title: milestone.title })} />
                  <span className="flex-1">
                    {milestone.title} · {milestone.completed ? t('task.complete') : t('task.open')}
                  </span>
                  <Button type="button" variant="outline" size="sm" disabled={disabledOpen} onClick={() => onOpenTask(milestone.task_id!)}>
                    {t('openTask')}
                  </Button>
                </>
              ) : (
                <>
                  <Checkbox
                    checked={milestone.completed}
                    disabled={disabledToggle}
                    onCheckedChange={(checked) => onToggle(milestone.id, checked === true)}
                    aria-label={t('toggleMilestone', {
                      title: milestone.title,
                      state: milestone.completed ? t('incomplete') : t('complete'),
                    })}
                  />
                  <span className="flex-1">
                    {milestone.title} {milestone.due_date && <span className="text-muted-foreground">· {milestone.due_date}</span>}
                  </span>
                </>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{t('no.milestones')}</p>
      )}
    </div>
  );
}

/**
 * Delete confirmation bound to a frozen `deleteIntent` (id + revision). Confirm stays disabled
 * unless the intent still matches the live `current` goal and no write is pending or under review.
 */
function DeleteGoalDialog({
  open,
  writesPending,
  deleting,
  deleteIntent,
  fallbackRevision,
  current,
  disabledConfirm,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  writesPending: boolean;
  deleting: boolean;
  deleteIntent: { id: string; revision: number; title: string } | null;
  fallbackRevision: number;
  current: Goal | undefined;
  disabledConfirm: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (intent: { id: string; revision: number; title: string }) => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('delete.goalConfirmTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('delete.goalConfirmDescription', { title: deleteIntent?.title ?? '' })} ·{' '}
            {t('revisionNumber', { revision: deleteIntent?.revision ?? fallbackRevision })}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={writesPending}>{t('cancel')}</AlertDialogCancel>
          <Button
            type="button"
            variant="destructive"
            disabled={
              disabledConfirm ||
              writesPending ||
              !deleteIntent ||
              deleteIntent.id !== current?.id ||
              deleteIntent.revision !== current?.revision
            }
            onClick={() => {
              if (
                deleteIntent &&
                !disabledConfirm &&
                current &&
                !writesPending &&
                deleteIntent.id === current.id &&
                deleteIntent.revision === current.revision
              )
                onConfirm(deleteIntent);
            }}
          >
            {deleting ? t('deleting') : t('delete.goal')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Goal title with status, revision and id once the owner read is verified, plus an optional Back button (disabled while a write is pending). */
function GoalDetailHeader({
  current,
  ownerVerified,
  writesPending,
  onClose,
}: {
  current: Goal;
  ownerVerified: boolean;
  writesPending: boolean;
  onClose?: () => void;
}) {
  const t = useTranslations('taskGoal');
  const goalStatusLabels = {
    active: t('goal.statusActive'),
    completed: t('goal.statusCompleted'),
    paused: t('goal.statusPaused'),
    cancelled: t('goal.statusCancelled'),
  };
  return (
    <header className="flex items-start justify-between gap-3">
      <div>
        <h2 className="text-lg font-semibold">{ownerVerified ? current.title : t('goal.detailTitle')}</h2>
        {ownerVerified && (
          <>
            <p className="text-xs text-muted-foreground">
              {goalStatusLabels[current.status]} · {t('revisionNumber', { revision: current.revision })}
            </p>
            <p className="text-xs text-muted-foreground">
              {t('goal.id')}: {current.id}
            </p>
          </>
        )}
      </div>
      {onClose && (
        <Button type="button" variant="outline" disabled={writesPending} onClick={onClose}>
          {t('back')}
        </Button>
      )}
    </header>
  );
}

/** Read-only description, desired outcome, deadline/progress and entity references of a goal. */
function GoalSummary({ goal: current }: { goal: Goal }) {
  const t = useTranslations('taskGoal');
  return (
    <>
      {current.description && <p className="whitespace-pre-wrap text-sm">{current.description}</p>}
      {current.desired_outcome && (
        <p className="text-sm">
          <strong>{t('desiredOutcome')}:</strong> {current.desired_outcome}
        </p>
      )}
      <p className="text-sm">
        {t('deadline')}: {current.deadline ?? t('none')} ·{' '}
        {t('progressValue', { progress: current.progress, mode: current.manual_progress ? t('manual') : t('milestone.derived') })}
      </p>
      <p className="text-xs text-muted-foreground">
        {t('entity.references', { ids: current.entity_ids.length ? current.entity_ids.join(', ') : t('none') })}
      </p>
    </>
  );
}

/** Edit and Delete goal buttons; both share one `disabled` flag and their guarded handlers come from the parent. */
function GoalActions({ disabled, onEdit, onDelete }: { disabled: boolean; onEdit: () => void; onDelete: () => void }) {
  const t = useTranslations('taskGoal');
  return (
    <div className="flex gap-2">
      <Button type="button" disabled={disabled} onClick={onEdit}>
        {t('editGoal')}
      </Button>
      <Button type="button" variant="destructive" disabled={disabled} onClick={onDelete}>
        {t('delete.goal')}
      </Button>
    </div>
  );
}

/** Optional "refresh current goal" button, its failure text and the fetched snapshot review with an accept button. */
function GoalReviewControls({
  showRefresh,
  refreshing,
  disabled,
  refreshError,
  currentGoal,
  acceptLabel,
  onRefresh,
  onAccept,
}: {
  showRefresh: boolean;
  refreshing: boolean;
  disabled: boolean;
  refreshError: string;
  currentGoal: Goal | null;
  acceptLabel: string;
  onRefresh: () => void;
  onAccept: () => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <>
      {showRefresh && (
        <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={onRefresh}>
          {refreshing ? t('loading') : t('refreshCurrentGoal')}
        </Button>
      )}
      {refreshError && <p>{refreshError}</p>}
      {currentGoal && <CurrentGoalReview goal={currentGoal} acceptLabel={acceptLabel} onAccept={onAccept} />}
    </>
  );
}

/**
 * Declares the goal delete and milestone-update mutations (call order matters: delete first).
 * Delete success invalidates goal/task caches, closes the confirm dialog and calls `onClose`;
 * milestone success stores the saved goal and its revision. `conflict` flags report HTTP 409s.
 */
function useGoalWriteMutations({
  csrfToken,
  queryClient,
  setConfirmDelete,
  setDeleteIntent,
  setExpectedRevision,
  onClose,
}: {
  csrfToken: string;
  queryClient: QueryClient;
  setConfirmDelete: (value: boolean) => void;
  setDeleteIntent: (value: { id: string; revision: number; title: string } | null) => void;
  setExpectedRevision: (revision: number) => void;
  onClose?: () => void;
}) {
  const remove = useMutation({
    mutationFn: (intent: { id: string; revision: number; title: string }) => deleteGoal(intent.id, intent.revision, csrfToken),
    onSuccess: (_, intent) => {
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['task'] });
      queryClient.removeQueries({ queryKey: ['goal', intent.id] });
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
  const milestoneMutation = useMutation({
    mutationFn: ({ id, revision, milestones }: { id: string; revision: number; milestones: Goal['milestones'] }) =>
      updateGoal(id, { milestones, expected_revision: revision }, csrfToken),
    onSuccess: (saved) => {
      queryClient.setQueryData(['goal', saved.id], saved);
      setExpectedRevision(saved.revision);
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
    },
  });
  const milestoneConflict = milestoneMutation.error instanceof ApiError && milestoneMutation.error.status === 409;
  return { remove, conflict, milestoneMutation, milestoneConflict };
}

type DeleteIntent = { id: string; revision: number; title: string };

/**
 * Owns all state, queries and mutations behind GoalDetail (called once, in a fixed hook order).
 * Live goal read: `ownerVerified` is true only for a settled, successful fetch. `writesPending`
 * covers delete, milestone and form writes; `writeReviewRequired` is set by a 409 and cleared by
 * accepting the reviewed server snapshot.
 */
function useGoalDetailController({ goal, csrfToken, onClose }: { goal: Goal; csrfToken: string; onClose?: () => void }) {
  const t = useTranslations('taskGoal');
  const liveGoal = useQuery({ queryKey: ['goal', goal.id], queryFn: () => getGoal(goal.id), initialData: goal, initialDataUpdatedAt: 0 });
  const current = liveGoal.data;
  const [editing, setEditing] = useState(false);
  const [formPending, setFormPending] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [currentGoal, setCurrentGoal] = useState<Goal | null>(null);
  const [expectedRevision, setExpectedRevision] = useState(goal.revision);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const [deleteIntent, setDeleteIntent] = useState<DeleteIntent | null>(null);
  const queryClient = useQueryClient();
  const ownerVerified = Boolean(current && liveGoal.dataUpdatedAt > 0 && !liveGoal.isFetching && !liveGoal.isError);
  const { remove, conflict, milestoneMutation, milestoneConflict } = useGoalWriteMutations({
    csrfToken,
    queryClient,
    setConfirmDelete,
    setDeleteIntent,
    setExpectedRevision,
    onClose,
  });
  /** Tracks fresh revisions only outside an active owner conflict or frozen delete confirmation. */
  useResetOnChange([ownerVerified, current, conflict, milestoneConflict, confirmDelete], () => {
    if (ownerVerified && current && !conflict && !milestoneConflict && !confirmDelete) setExpectedRevision(current.revision);
  });
  const writesPending = remove.isPending || milestoneMutation.isPending || formPending;
  const writeReviewRequired = conflict || milestoneConflict;
  /** Fetches the current goal projection for review without advancing a pending deletion revision. */
  const refreshCurrentGoal = async () => {
    setRefreshing(true);
    setRefreshError('');
    setCurrentGoal(null);
    try {
      setCurrentGoal(await getGoal(goal.id));
    } catch (error) {
      setRefreshError(t(ownerFailureKey(error, 'goal.ownerMissing', 'goal.readFailed')));
    } finally {
      setRefreshing(false);
    }
  };
  /** Applies the explicitly reviewed goal snapshot and resets stale mutation errors before a new write. */
  const acceptCurrentGoalRevision = () => {
    if (!currentGoal || writesPending) return;
    queryClient.setQueryData(['goal', currentGoal.id], currentGoal);
    setExpectedRevision(currentGoal.revision);
    setCurrentGoal(null);
    remove.reset();
    milestoneMutation.reset();
  };
  return {
    liveGoal,
    current,
    editing,
    setEditing,
    setFormPending,
    formPending,
    confirmDelete,
    setConfirmDelete,
    currentGoal,
    expectedRevision,
    setExpectedRevision,
    refreshing,
    refreshError,
    deleteIntent,
    setDeleteIntent,
    queryClient,
    ownerVerified,
    remove,
    conflict,
    milestoneMutation,
    writesPending,
    writeReviewRequired,
    refreshCurrentGoal,
    acceptCurrentGoalRevision,
  };
}

type GoalDetailVm = ReturnType<typeof useGoalDetailController>;

/** Read-only goal view: summary, milestone checklist, milestone-failure review and Edit/Delete actions (hidden for administrator viewers). */
function GoalReadView({
  vm,
  goal,
  isAdminViewer,
  onEditLinkedTask,
}: {
  vm: GoalDetailVm;
  goal: Goal;
  isAdminViewer: boolean;
  onEditLinkedTask?: (taskId: string) => void;
}) {
  const t = useTranslations('taskGoal');
  const { liveGoal, ownerVerified, writesPending, writeReviewRequired, milestoneMutation, refreshing, refreshError, currentGoal } = vm;
  const current = vm.current;
  return (
    <>
      <GoalSummary goal={current} />
      <MilestoneList
        milestones={current.milestones}
        disabledToggle={isAdminViewer || !ownerVerified || writesPending || writeReviewRequired}
        disabledOpen={writesPending}
        onOpenTask={(taskId) => {
          if (!writesPending && ownerVerified) onEditLinkedTask?.(taskId);
        }}
        onToggle={(milestoneId, completed) => {
          if (
            !isAdminViewer &&
            ownerVerified &&
            !writesPending &&
            !writeReviewRequired &&
            current.id === goal.id &&
            current.revision === liveGoal.data?.revision
          )
            milestoneMutation.mutate({
              id: current.id,
              revision: current.revision,
              milestones: current.milestones.map((item) => (item.id === milestoneId ? { ...item, completed } : item)),
            });
        }}
      />
      {milestoneMutation.error && (
        <div role="alert" className="text-sm text-destructive">
          <p>
            {milestoneMutation.error instanceof ApiError && milestoneMutation.error.status === 409
              ? t('milestone.updateFailed', { revision: current.revision })
              : t('goal.mutationFailed')}
          </p>
          <GoalReviewControls
            showRefresh
            refreshing={refreshing}
            disabled={refreshing || writesPending}
            refreshError={refreshError}
            currentGoal={currentGoal}
            acceptLabel={t('use.currentGoalRevision')}
            onRefresh={() => void vm.refreshCurrentGoal()}
            onAccept={vm.acceptCurrentGoalRevision}
          />
        </div>
      )}
      {!isAdminViewer && (
        <GoalActions
          disabled={!ownerVerified || writesPending || writeReviewRequired}
          onEdit={() => {
            if (ownerVerified && !writesPending && !writeReviewRequired) vm.setEditing(true);
          }}
          onDelete={() => {
            if (!ownerVerified || writesPending || writeReviewRequired) return;
            vm.setExpectedRevision(current.revision);
            vm.setDeleteIntent({ id: current.id, revision: current.revision, title: current.title });
            vm.setConfirmDelete(true);
          }}
        />
      )}
    </>
  );
}

/** Failure panel for a rejected goal delete; on a 409 conflict it offers a refresh of the current goal for review. */
function GoalDeleteError({ vm }: { vm: GoalDetailVm }) {
  const t = useTranslations('taskGoal');
  const { remove, conflict, refreshing, writesPending, refreshError, currentGoal } = vm;
  return (
    <div role="alert" className="text-sm text-destructive">
      <p>{conflict ? t('goal.deleteConflict') : t(ownerFailureKey(remove.error, 'goal.ownerMissing', 'goal.deleteFailed'))}</p>
      <GoalReviewControls
        showRefresh={conflict}
        refreshing={refreshing}
        disabled={refreshing || writesPending}
        refreshError={refreshError}
        currentGoal={currentGoal}
        acceptLabel={currentGoal ? t('use.currentRevisionForDelete', { revision: currentGoal.revision }) : ''}
        onRefresh={() => void vm.refreshCurrentGoal()}
        onAccept={vm.acceptCurrentGoalRevision}
      />
    </div>
  );
}

/** Displays goal data with retained refetch drafts; administrator viewers can read but cannot change milestones or goals. */
export function GoalDetail({
  goal,
  csrfToken,
  onClose,
  onEditLinkedTask,
}: {
  goal: Goal;
  csrfToken: string;
  onClose?: () => void;
  onEditLinkedTask?: (taskId: string) => void;
}) {
  const t = useTranslations('taskGoal');
  const { isAdminViewer } = useWorkspace();
  const vm = useGoalDetailController({ goal, csrfToken, onClose });
  const { liveGoal, current, editing, ownerVerified, writesPending, writeReviewRequired, queryClient } = vm;
  const readError = t(ownerFailureKey(liveGoal.error, 'goal.ownerMissing', 'goal.readFailed'));
  return (
    <section className="flex flex-col gap-4 rounded-lg border border-border p-4">
      <GoalDetailHeader current={current} ownerVerified={ownerVerified} writesPending={writesPending} onClose={onClose} />
      {liveGoal.isFetching && (
        <p role="status" className="text-sm text-muted-foreground">
          {t('loadingGoalDetail')}
        </p>
      )}
      {liveGoal.isError && (
        <div role="alert" className="text-sm text-destructive">
          <p>{readError}</p>
          <Button type="button" variant="outline" size="sm" disabled={liveGoal.isFetching} onClick={() => void liveGoal.refetch()}>
            {liveGoal.isFetching ? t('loading') : t('retry')}
          </Button>
        </div>
      )}
      {editing && !isAdminViewer && current ? (
        <GoalForm
          key={current.id}
          goal={current}
          csrfToken={csrfToken}
          canSave={ownerVerified}
          onPendingChange={vm.setFormPending}
          onSuccess={(saved) => {
            queryClient.setQueryData(['goal', saved.id], saved);
            vm.setExpectedRevision(saved.revision);
            vm.setEditing(false);
          }}
          onCancel={() => {
            if (!vm.formPending) vm.setEditing(false);
          }}
        />
      ) : (
        ownerVerified && <GoalReadView vm={vm} goal={goal} isAdminViewer={isAdminViewer} onEditLinkedTask={onEditLinkedTask} />
      )}
      {vm.remove.error && <GoalDeleteError vm={vm} />}
      {!isAdminViewer && (
        <DeleteGoalDialog
          open={vm.confirmDelete}
          writesPending={writesPending}
          deleting={vm.remove.isPending}
          deleteIntent={vm.deleteIntent}
          fallbackRevision={vm.expectedRevision}
          current={current}
          disabledConfirm={!ownerVerified || writeReviewRequired}
          onOpenChange={(open) => {
            if (writesPending) return;
            vm.setConfirmDelete(open);
            if (!open) vm.setDeleteIntent(null);
          }}
          onConfirm={(intent) => vm.remove.mutate(intent)}
        />
      )}
    </section>
  );
}
