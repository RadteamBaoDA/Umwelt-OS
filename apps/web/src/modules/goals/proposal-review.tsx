'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ApiError } from '@/core/api';
import { Button } from '@/components/ui/button';
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { acceptPlan, getGoal } from './api';
import type { Goal, PlanAcceptanceResult, PlanProposal } from './types';

/** Previews a stable proposal and binds explicit owner confirmation to the displayed goal, revision, and cloned content. */
export function ProposalReview({ goal, proposal, csrfToken, onClose }: {
  goal: Goal;
  proposal: PlanProposal;
  csrfToken: string;
  onClose?: () => void;
}) {
  const t = useTranslations('taskGoal');
  const [confirm, setConfirm] = useState(false);
  const [confirmation, setConfirmation] = useState<{ goalId: string; proposal: PlanProposal } | null>(null);
  const [receipt, setReceipt] = useState<PlanAcceptanceResult | null>(null);
  const [currentGoal, setCurrentGoal] = useState<Goal | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const queryClient = useQueryClient();
  const goalQuery = useQuery({ queryKey: ['goal', goal.id], queryFn: () => getGoal(goal.id), initialData: goal, initialDataUpdatedAt: 0 });
  const current = goalQuery.data;
  const ownerVerified = Boolean(current && goalQuery.dataUpdatedAt > 0 && !goalQuery.isFetching && !goalQuery.isError);
  const accept = useMutation({
    mutationFn: ({ goalId, proposal: reviewedProposal }: { goalId: string; proposal: PlanProposal }) => acceptPlan(goalId, reviewedProposal, csrfToken),
    onSuccess: (result, variables) => {
      // Owner acceptance is the only action that materializes proposal tasks; refresh both derived domains.
      setReceipt(result);
      setConfirm(false);
      setConfirmation(null);
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
      queryClient.setQueryData(['goal', variables.goalId], result.goal);
    },
    onError: () => { setConfirm(false); setConfirmation(null); },
  });
  const conflict = accept.error instanceof ApiError && accept.error.status === 409;
  const displayedProposal = confirm && confirmation ? confirmation.proposal : proposal;
  const proposalDeadlinesValid = proposal.tasks.every((item) => {
    if (item.due_at && item.due_date) return false;
    return !item.due_at || /(Z|[+-]\d{2}:\d{2})$/i.test(item.due_at);
  });
  /** Loads the captured proposal goal for conflict review without changing its identity, revision, or content. */
  const refreshCurrentGoal = async () => {
    setRefreshing(true);
    setRefreshError('');
    setCurrentGoal(null);
    try { setCurrentGoal(await getGoal(accept.variables?.goalId ?? goal.id)); }
    catch (error) { setRefreshError(error instanceof ApiError && error.status === 404 ? t('goalOwnerMissing') : error instanceof ApiError && [401, 403].includes(error.status) ? t('ownerAccessDenied') : t('goalReadFailed')); }
    finally { setRefreshing(false); }
  };
  const taskStatus = { inbox: t('statusInbox'), todo: t('statusTodo'), in_progress: t('statusInProgress'), blocked: t('statusBlocked'), done: t('statusDone'), cancelled: t('statusCancelled') };
  return <section className="flex flex-col gap-3 rounded-lg border border-border p-4">
    <header className="flex items-start justify-between"><div><h2 className="text-lg font-semibold">{t('proposalReviewTitle')}</h2><p className="text-xs text-muted-foreground">{t('proposalRevision', { id: displayedProposal.proposal_id, revision: displayedProposal.expected_revision })}</p></div>{onClose && <Button type="button" variant="outline" disabled={accept.isPending} onClick={() => { if (accept.isPending) return; setConfirm(false); setConfirmation(null); onClose(); }}>{t('close')}</Button>}</header>
    {goalQuery.isFetching && <p role="status" className="text-sm text-muted-foreground">{t('loadingGoalDetail')}</p>}
    {goalQuery.isError && <div role="alert" className="text-sm text-destructive"><p>{goalQuery.error instanceof ApiError && goalQuery.error.status === 404 ? t('goalOwnerMissing') : goalQuery.error instanceof ApiError && [401, 403].includes(goalQuery.error.status) ? t('ownerAccessDenied') : t('proposalGoalReadFailed')}</p><Button type="button" variant="outline" size="sm" disabled={goalQuery.isFetching} onClick={() => void goalQuery.refetch()}>{goalQuery.isFetching ? t('loading') : t('retry')}</Button></div>}
    {ownerVerified && <p className="text-sm">{t('currentGoalRevision', { title: current.title, revision: current.revision, progress: current.progress })}</p>}
    <div><h3 className="text-sm font-medium">{t('proposedMilestones', { count: displayedProposal.milestones.length })}</h3><ul className="list-disc pl-5 text-sm">{displayedProposal.milestones.map((item, index) => <li key={item.id ?? `${item.order}-${index}`}>{item.title} · {t('milestoneOrder', { order: item.order ?? 0 })}{item.id && ` · ${t('milestoneId', { id: item.id })}`}{item.due_date ? ` · ${item.due_date}` : ''}{item.task_index != null ? ` · ${t('proposalTaskIndex', { index: item.task_index + 1 })}` : ''}</li>)}</ul></div>
    <div><h3 className="text-sm font-medium">{t('proposedTasks', { count: displayedProposal.tasks.length })}</h3><ul className="list-disc pl-5 text-sm">{displayedProposal.tasks.map((item, index) => <li key={index}><strong>{item.title}</strong> · {taskStatus[item.status ?? 'todo']}{item.description && <p>{item.description}</p>}{item.due_date && <p>{t('dateOnlyDeadline')}: {item.due_date}</p>}{item.due_at && <p>{t('dueInstantLabel')}: {item.due_at}</p>}{item.entity_ids?.length ? <p>{t('entityReferences', { ids: item.entity_ids.join(', ') })}</p> : null}</li>)}</ul></div>
    {!proposalDeadlinesValid && <p role="alert" className="text-sm text-destructive">{t('invalidProposalTaskDue')}</p>}
    {receipt && <div role="status" className="rounded border border-primary/40 p-3 text-sm"><p>{t('acceptanceReceipt', { revision: receipt.accepted_goal_revision, replay: receipt.already_accepted ? t('replayedReceipt') : '' })}</p><p>{t('createdTaskIds', { ids: receipt.created_tasks.map((task) => task.id).join(', ') || t('none') })}</p><p>{t('acceptedMilestoneIds', { ids: receipt.accepted_milestone_ids.join(', ') || t('none') })}</p>{receipt.deleted_task_ids.length > 0 && <p>{t('missingLinkedTaskIds', { ids: receipt.deleted_task_ids.join(', ') })}</p>}</div>}
    {accept.error && <div role="alert" className="text-sm text-destructive"><p>{conflict ? t('proposalConflict') : accept.error instanceof ApiError && accept.error.status === 404 ? t('goalOwnerMissing') : accept.error instanceof ApiError && [401, 403].includes(accept.error.status) ? t('ownerAccessDenied') : t('proposalAcceptFailed')}</p>{conflict && <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={() => void refreshCurrentGoal()}>{refreshing ? t('loading') : t('loadCurrentGoal')}</Button>}{refreshError && <p>{refreshError}</p>}{currentGoal && <div className="rounded border border-border p-2"><p>{t('currentGoalRevision', { title: currentGoal.title, revision: currentGoal.revision, progress: currentGoal.progress })}</p><p>{currentGoal.desired_outcome ?? t('none')} · {currentGoal.deadline ?? t('noDeadline')}</p><p>{currentGoal.entity_ids.join(', ') || t('none')}</p><ul>{currentGoal.milestones.map((item) => <li key={item.id}>{item.id} · {item.title} · {item.completed ? t('complete') : t('incomplete')}{item.task_id ? ` · ${t('linkedTask', { id: item.task_id })}` : ''}</li>)}</ul><p>{t('proposalMustBeRefreshed')}</p></div>}</div>}
    {!receipt && <Button type="button" disabled={!ownerVerified || accept.isPending || proposal.expected_revision !== current.revision || !proposalDeadlinesValid} onClick={() => { setConfirmation({ goalId: goal.id, proposal: structuredClone(proposal) }); setConfirm(true); }}>{proposal.expected_revision !== current.revision ? t('proposalStale') : t('acceptPlan')}</Button>}
    <AlertDialog open={confirm} onOpenChange={(open) => { if (accept.isPending) return; setConfirm(open); if (!open) setConfirmation(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('acceptPlanConfirmTitle')}</AlertDialogTitle><AlertDialogDescription>{t('acceptPlanConfirmDescription', { tasks: confirmation?.proposal.tasks.length ?? 0, milestones: confirmation?.proposal.milestones.length ?? 0, revision: confirmation?.proposal.expected_revision ?? proposal.expected_revision })} · {t('proposalRevision', { id: confirmation?.proposal.proposal_id ?? proposal.proposal_id, revision: confirmation?.proposal.expected_revision ?? proposal.expected_revision })} · {t('goalId')}: {confirmation?.goalId ?? goal.id}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={accept.isPending}>{t('reviewAgain')}</AlertDialogCancel><Button type="button" disabled={accept.isPending || !confirmation || confirmation.goalId !== goal.id || !ownerVerified || confirmation.proposal.expected_revision !== current.revision} onClick={() => { if (confirmation && !accept.isPending && confirmation.goalId === goal.id && ownerVerified && current && current.id === confirmation.goalId && confirmation.proposal.expected_revision === current.revision) accept.mutate(confirmation); }}>{accept.isPending ? t('accepting') : t('confirmAcceptance')}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </section>;
}
