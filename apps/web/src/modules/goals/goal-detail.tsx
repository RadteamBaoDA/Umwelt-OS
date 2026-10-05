'use client';

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@/core/api';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { deleteGoal, getGoal, updateGoal } from './api';
import { GoalForm } from './goal-form';
import type { Goal } from './types';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Checkbox } from '@/components/ui/checkbox';

/** Displays owner goal data with retained refetch drafts and serializes milestone, form, and delete writes by verified identity and revision. */
export function GoalDetail({ goal, csrfToken, onClose, onEditLinkedTask }: {
  goal: Goal;
  csrfToken: string;
  onClose?: () => void;
  onEditLinkedTask?: (taskId: string) => void;
}) {
  const t = useTranslations('taskGoal');
  const goalStatusLabels = { active: t('goalStatusActive'), completed: t('goalStatusCompleted'), paused: t('goalStatusPaused'), cancelled: t('goalStatusCancelled') };
  const liveGoal = useQuery({ queryKey: ['goal', goal.id], queryFn: () => getGoal(goal.id), initialData: goal, initialDataUpdatedAt: 0 });
  const current = liveGoal.data;
  const [editing, setEditing] = useState(false);
  const [formPending, setFormPending] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [currentGoal, setCurrentGoal] = useState<Goal | null>(null);
  const [expectedRevision, setExpectedRevision] = useState(goal.revision);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const [deleteIntent, setDeleteIntent] = useState<{ id: string; revision: number; title: string } | null>(null);
  const queryClient = useQueryClient();
  const ownerVerified = Boolean(current && liveGoal.dataUpdatedAt > 0 && !liveGoal.isFetching && !liveGoal.isError);
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
    onError: () => { setConfirmDelete(false); setDeleteIntent(null); },
  });
  const conflict = remove.error instanceof ApiError && remove.error.status === 409;
  const milestoneMutation = useMutation({
    mutationFn: ({ id, revision, milestones }: { id: string; revision: number; milestones: Goal['milestones'] }) => updateGoal(id, { milestones, expected_revision: revision }, csrfToken),
    onSuccess: (saved) => { queryClient.setQueryData(['goal', saved.id], saved); setExpectedRevision(saved.revision); void queryClient.invalidateQueries({ queryKey: ['goals'] }); },
  });
  const milestoneConflict = milestoneMutation.error instanceof ApiError && milestoneMutation.error.status === 409;
  /** Tracks fresh revisions only outside an active owner conflict or frozen delete confirmation. */
  useEffect(() => { if (ownerVerified && current && !conflict && !milestoneConflict && !confirmDelete) setExpectedRevision(current.revision); }, [ownerVerified, current, conflict, milestoneConflict, confirmDelete]);
  const writesPending = remove.isPending || milestoneMutation.isPending || formPending;
  const writeReviewRequired = conflict || milestoneConflict;
  /** Fetches the current goal projection for review without advancing a pending deletion revision. */
  const refreshCurrentGoal = async () => {
    setRefreshing(true);
    setRefreshError('');
    setCurrentGoal(null);
    try { setCurrentGoal(await getGoal(goal.id)); }
    catch (error) { setRefreshError(error instanceof ApiError && error.status === 404 ? t('goalOwnerMissing') : error instanceof ApiError && [401, 403].includes(error.status) ? t('ownerAccessDenied') : t('goalReadFailed')); }
    finally { setRefreshing(false); }
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
  const readError = liveGoal.error instanceof ApiError && liveGoal.error.status === 404 ? t('goalOwnerMissing') : liveGoal.error instanceof ApiError && [401, 403].includes(liveGoal.error.status) ? t('ownerAccessDenied') : t('goalReadFailed');
  return <section className="flex flex-col gap-4 rounded-lg border border-border p-4">
    <header className="flex items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">{ownerVerified ? current.title : t('goalDetailTitle')}</h2>{ownerVerified && <><p className="text-xs text-muted-foreground">{goalStatusLabels[current.status]} · {t('revisionNumber', { revision: current.revision })}</p><p className="text-xs text-muted-foreground">{t('goalId')}: {current.id}</p></>}</div>{onClose && <Button type="button" variant="outline" disabled={writesPending} onClick={onClose}>{t('back')}</Button>}</header>
    {liveGoal.isFetching && <p role="status" className="text-sm text-muted-foreground">{t('loadingGoalDetail')}</p>}
    {liveGoal.isError && <div role="alert" className="text-sm text-destructive"><p>{readError}</p><Button type="button" variant="outline" size="sm" disabled={liveGoal.isFetching} onClick={() => void liveGoal.refetch()}>{liveGoal.isFetching ? t('loading') : t('retry')}</Button></div>}
    {editing && current
      ? <GoalForm key={current.id} goal={current} csrfToken={csrfToken} canSave={ownerVerified} onPendingChange={setFormPending} onSuccess={(saved) => { queryClient.setQueryData(['goal', saved.id], saved); setExpectedRevision(saved.revision); setEditing(false); }} onCancel={() => { if (!formPending) setEditing(false); }} />
      : ownerVerified && <>
        {current.description && <p className="whitespace-pre-wrap text-sm">{current.description}</p>}
        {current.desired_outcome && <p className="text-sm"><strong>{t('desiredOutcome')}:</strong> {current.desired_outcome}</p>}
        <p className="text-sm">{t('deadline')}: {current.deadline ?? t('none')} · {t('progressValue', { progress: current.progress, mode: current.manual_progress ? t('manual') : t('milestoneDerived') })}</p>
        <p className="text-xs text-muted-foreground">{t('entityReferences', { ids: current.entity_ids.length ? current.entity_ids.join(', ') : t('none') })}</p>
        <div><h3 className="mb-2 text-sm font-medium">{t('milestones')}</h3>{current.milestones.length ? <ul className="flex flex-col gap-2">{current.milestones.map((milestone) => <li key={milestone.id} className="flex items-center gap-2 rounded border border-border p-2 text-sm">{milestone.task_id ? <><Checkbox checked={milestone.completed} disabled aria-label={t('linkedCompletionAria', { title: milestone.title })} /><span className="flex-1">{milestone.title} · {milestone.completed ? t('taskComplete') : t('taskOpen')}</span><Button type="button" variant="outline" size="sm" disabled={writesPending} onClick={() => { if (!writesPending && ownerVerified) onEditLinkedTask?.(milestone.task_id!); }}>{t('openTask')}</Button></> : <><Checkbox checked={milestone.completed} disabled={!ownerVerified || writesPending || writeReviewRequired} onCheckedChange={(checked) => { if (ownerVerified && !writesPending && !writeReviewRequired && current.id === goal.id && current.revision === liveGoal.data?.revision) milestoneMutation.mutate({ id: current.id, revision: current.revision, milestones: current.milestones.map((item) => item.id === milestone.id ? { ...item, completed: checked === true } : item) }); }} aria-label={t('toggleMilestone', { title: milestone.title, state: milestone.completed ? t('incomplete') : t('complete') })} /><span className="flex-1">{milestone.title} {milestone.due_date && <span className="text-muted-foreground">· {milestone.due_date}</span>}</span></>}</li>)}</ul> : <p className="text-sm text-muted-foreground">{t('noMilestones')}</p>}</div>
        {milestoneMutation.error && <div role="alert" className="text-sm text-destructive"><p>{milestoneMutation.error instanceof ApiError && milestoneMutation.error.status === 409 ? t('milestoneUpdateFailed', { revision: current.revision }) : t('goalMutationFailed')}</p><Button type="button" variant="outline" size="sm" disabled={refreshing || writesPending} onClick={() => void refreshCurrentGoal()}>{refreshing ? t('loading') : t('refreshCurrentGoal')}</Button>{refreshError && <p>{refreshError}</p>}{currentGoal && <div className="rounded border border-border p-2"><p>{t('currentGoalRevision', { title: currentGoal.title, revision: currentGoal.revision, progress: currentGoal.progress })}</p><p>{currentGoal.desired_outcome ?? t('none')} · {currentGoal.deadline ?? t('noDeadline')} · {currentGoal.manual_progress ? t('manual') : t('milestoneDerived')}</p><p>{t('entityReferences', { ids: currentGoal.entity_ids.join(', ') || t('none') })}</p><ul>{currentGoal.milestones.map((item) => <li key={item.id}>{item.title} · {item.completed ? t('complete') : t('incomplete')}{item.task_id ? ` · ${t('linkedTask', { id: item.task_id })}` : ''}</li>)}</ul><Button type="button" variant="outline" size="sm" onClick={acceptCurrentGoalRevision}>{t('useCurrentGoalRevision')}</Button></div>}</div>}
        <div className="flex gap-2"><Button type="button" disabled={!ownerVerified || writesPending || writeReviewRequired} onClick={() => { if (ownerVerified && !writesPending && !writeReviewRequired) setEditing(true); }}>{t('editGoal')}</Button><Button type="button" variant="destructive" disabled={!ownerVerified || writesPending || writeReviewRequired} onClick={() => { if (!ownerVerified || writesPending || writeReviewRequired) return; setExpectedRevision(current.revision); setDeleteIntent({ id: current.id, revision: current.revision, title: current.title }); setConfirmDelete(true); }}>{t('deleteGoal')}</Button></div>
       </>
       }
    {remove.error && <div role="alert" className="text-sm text-destructive"><p>{conflict ? t('goalDeleteConflict') : remove.error instanceof ApiError && remove.error.status === 404 ? t('goalOwnerMissing') : remove.error instanceof ApiError && [401, 403].includes(remove.error.status) ? t('ownerAccessDenied') : t('goalDeleteFailed')}</p>{conflict && <Button type="button" variant="outline" size="sm" disabled={refreshing || writesPending} onClick={() => void refreshCurrentGoal()}>{refreshing ? t('loading') : t('refreshCurrentGoal')}</Button>}{refreshError && <p>{refreshError}</p>}{currentGoal && <div className="rounded border border-border p-2"><p>{t('currentGoalRevision', { title: currentGoal.title, revision: currentGoal.revision, progress: currentGoal.progress })}</p><p>{currentGoal.desired_outcome ?? t('none')} · {currentGoal.deadline ?? t('noDeadline')} · {currentGoal.manual_progress ? t('manual') : t('milestoneDerived')}</p><p>{t('entityReferences', { ids: currentGoal.entity_ids.join(', ') || t('none') })}</p><ul>{currentGoal.milestones.map((item) => <li key={item.id}>{item.title} · {item.completed ? t('complete') : t('incomplete')}{item.task_id ? ` · ${t('linkedTask', { id: item.task_id })}` : ''}</li>)}</ul><Button type="button" variant="outline" size="sm" onClick={acceptCurrentGoalRevision}>{t('useCurrentRevisionForDelete', { revision: currentGoal.revision })}</Button></div>}</div>}
    <AlertDialog open={confirmDelete} onOpenChange={(open) => { if (writesPending) return; setConfirmDelete(open); if (!open) setDeleteIntent(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('deleteGoalConfirmTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteGoalConfirmDescription', { title: deleteIntent?.title ?? '' })} · {t('revisionNumber', { revision: deleteIntent?.revision ?? expectedRevision })}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={writesPending}>{t('cancel')}</AlertDialogCancel><Button type="button" variant="destructive" disabled={!ownerVerified || writesPending || !deleteIntent || deleteIntent.id !== current?.id || deleteIntent.revision !== current?.revision || writeReviewRequired} onClick={() => { if (deleteIntent && ownerVerified && current && !writesPending && !writeReviewRequired && deleteIntent.id === current.id && deleteIntent.revision === current.revision) remove.mutate(deleteIntent); }}>{remove.isPending ? t('deleting') : t('deleteGoal')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </section>;
}
