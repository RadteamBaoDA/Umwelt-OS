'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ownerFailureKey } from '@/modules/shared';
import { ApiError } from '@/core/api';
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
import { acceptPlan, getGoal } from './api';
import type { Goal, PlanAcceptanceResult, PlanProposal } from './types';

/** Lists the proposal's milestones with order, optional id, due date and linked task index. */
function ProposalMilestones({ proposal }: { proposal: PlanProposal }) {
  const t = useTranslations('taskGoal');
  const displayedProposal = proposal;
  return (
    <div>
      <h3 className="text-sm font-medium">{t('proposedMilestones', { count: displayedProposal.milestones.length })}</h3>
      <ul className="list-disc pl-5 text-sm">
        {displayedProposal.milestones.map((item, index) => (
          <li key={item.id ?? `${item.order}-${index}`}>
            {item.title} · {t('milestone.order', { order: item.order ?? 0 })}
            {item.id && ` · ${t('milestone.id', { id: item.id })}`}
            {item.due_date ? ` · ${item.due_date}` : ''}
            {item.task_index != null ? ` · ${t('proposal.taskIndex', { index: item.task_index + 1 })}` : ''}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Lists the proposal's tasks with localized status, optional description, deadlines and entity references. */
function ProposalTasks({ proposal }: { proposal: PlanProposal }) {
  const t = useTranslations('taskGoal');
  const displayedProposal = proposal;
  const taskStatus = {
    inbox: t('statusInbox'),
    todo: t('statusTodo'),
    in_progress: t('statusInProgress'),
    blocked: t('statusBlocked'),
    done: t('statusDone'),
    cancelled: t('statusCancelled'),
  };
  return (
    <div>
      <h3 className="text-sm font-medium">{t('proposedTasks', { count: displayedProposal.tasks.length })}</h3>
      <ul className="list-disc pl-5 text-sm">
        {displayedProposal.tasks.map((item, index) => (
          <li key={index}>
            <strong>{item.title}</strong> · {taskStatus[item.status ?? 'todo']}
            {item.description && <p>{item.description}</p>}
            {item.due_date && (
              <p>
                {t('dateOnlyDeadline')}: {item.due_date}
              </p>
            )}
            {item.due_at && (
              <p>
                {t('dueInstantLabel')}: {item.due_at}
              </p>
            )}
            {item.entity_ids?.length ? <p>{t('entity.references', { ids: item.entity_ids.join(', ') })}</p> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Acceptance receipt: accepted revision (flagging replays), created task ids, accepted milestone ids and missing linked task ids. */
function AcceptanceReceipt({ receipt }: { receipt: PlanAcceptanceResult }) {
  const t = useTranslations('taskGoal');
  return (
    <div role="status" className="rounded border border-primary/40 p-3 text-sm">
      <p>
        {t('acceptanceReceipt', {
          revision: receipt.accepted_goal_revision,
          replay: receipt.already_accepted ? t('replayedReceipt') : '',
        })}
      </p>
      <p>{t('createdTaskIds', { ids: receipt.created_tasks.map((task) => task.id).join(', ') || t('none') })}</p>
      <p>{t('acceptedMilestoneIds', { ids: receipt.accepted_milestone_ids.join(', ') || t('none') })}</p>
      {receipt.deleted_task_ids.length > 0 && <p>{t('missing.linkedTaskIds', { ids: receipt.deleted_task_ids.join(', ') })}</p>}
    </div>
  );
}

/** Failure panel for a rejected plan acceptance; on 409 it can load the current goal for review before the owner re-reviews the proposal. */
function AcceptError({
  error,
  conflict,
  refreshing,
  refreshError,
  currentGoal,
  onRefresh,
}: {
  error: unknown;
  conflict: boolean;
  refreshing: boolean;
  refreshError: string;
  currentGoal: Goal | null;
  onRefresh: () => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <div role="alert" className="text-sm text-destructive">
      <p>{conflict ? t('proposal.conflict') : t(ownerFailureKey(error, 'goal.ownerMissing', 'proposal.acceptFailed'))}</p>
      {conflict && (
        <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={onRefresh}>
          {refreshing ? t('loading') : t('load.currentGoal')}
        </Button>
      )}
      {refreshError && <p>{refreshError}</p>}
      {currentGoal && (
        <div className="rounded border border-border p-2">
          <p>{t('current.goalRevision', { title: currentGoal.title, revision: currentGoal.revision, progress: currentGoal.progress })}</p>
          <p>
            {currentGoal.desired_outcome ?? t('none')} · {currentGoal.deadline ?? t('no.deadline')}
          </p>
          <p>{currentGoal.entity_ids.join(', ') || t('none')}</p>
          <ul>
            {currentGoal.milestones.map((item) => (
              <li key={item.id}>
                {item.id} · {item.title} · {item.completed ? t('complete') : t('incomplete')}
                {item.task_id ? ` · ${t('linked.task', { id: item.task_id })}` : ''}
              </li>
            ))}
          </ul>
          <p>{t('proposal.mustBeRefreshed')}</p>
        </div>
      )}
    </div>
  );
}

/** True when the frozen confirmation targets this goal, the owner read is verified and the proposal's expected revision equals the live revision. */
function confirmationReady(
  confirmation: { goalId: string; proposal: PlanProposal } | null,
  goalId: string,
  ownerVerified: boolean,
  current: Goal,
): boolean {
  return !!confirmation && confirmation.goalId === goalId && ownerVerified && confirmation.proposal.expected_revision === current.revision;
}

/**
 * Final confirmation for accepting a plan. Confirm is enabled only while the frozen confirmation
 * (goal id + cloned proposal) still matches the verified live goal id and revision.
 */
function ConfirmAcceptDialog({
  open,
  pending,
  confirmation,
  goalId,
  fallbackProposal,
  current,
  ownerVerified,
  onOpenChange,
  onConfirm,
}: {
  open: boolean;
  pending: boolean;
  confirmation: { goalId: string; proposal: PlanProposal } | null;
  goalId: string;
  fallbackProposal: PlanProposal;
  current: Goal;
  ownerVerified: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (value: { goalId: string; proposal: PlanProposal }) => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('accept.planConfirmTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('accept.planConfirmDescription', {
              tasks: confirmation?.proposal.tasks.length ?? 0,
              milestones: confirmation?.proposal.milestones.length ?? 0,
              revision: confirmation?.proposal.expected_revision ?? fallbackProposal.expected_revision,
            })}{' '}
            ·{' '}
            {t('proposal.revision', {
              id: confirmation?.proposal.proposal_id ?? fallbackProposal.proposal_id,
              revision: confirmation?.proposal.expected_revision ?? fallbackProposal.expected_revision,
            })}{' '}
            · {t('goal.id')}: {confirmation?.goalId ?? goalId}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>{t('reviewAgain')}</AlertDialogCancel>
          <Button
            type="button"
            disabled={pending || !confirmationReady(confirmation, goalId, ownerVerified, current)}
            onClick={() => {
              if (
                confirmation &&
                !pending &&
                confirmationReady(confirmation, goalId, ownerVerified, current) &&
                current.id === confirmation.goalId
              )
                onConfirm(confirmation);
            }}
          >
            {pending ? t('accepting') : t('confirmAcceptance')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Review title and proposal id/revision; the Close button is disabled while an acceptance is pending. */
function ProposalReviewHeader({ proposal, pending, onClose }: { proposal: PlanProposal; pending: boolean; onClose?: () => void }) {
  const t = useTranslations('taskGoal');
  return (
    <header className="flex items-start justify-between">
      <div>
        <h2 className="text-lg font-semibold">{t('proposal.reviewTitle')}</h2>
        <p className="text-xs text-muted-foreground">
          {t('proposal.revision', { id: proposal.proposal_id, revision: proposal.expected_revision })}
        </p>
      </div>
      {onClose && (
        <Button type="button" variant="outline" disabled={pending} onClick={onClose}>
          {t('close')}
        </Button>
      )}
    </header>
  );
}

/**
 * Declares the plan-acceptance mutation. Success (the only action that materializes proposal tasks)
 * stores the receipt, closes the confirm dialog, refreshes goal/task caches and seeds the accepted
 * goal; failure just closes the confirm dialog so the error panel shows.
 */
function usePlanAcceptance({
  csrfToken,
  queryClient,
  setReceipt,
  setConfirm,
  setConfirmation,
}: {
  csrfToken: string;
  queryClient: QueryClient;
  setReceipt: (value: PlanAcceptanceResult | null) => void;
  setConfirm: (value: boolean) => void;
  setConfirmation: (value: { goalId: string; proposal: PlanProposal } | null) => void;
}) {
  return useMutation({
    mutationFn: ({ goalId, proposal: reviewedProposal }: { goalId: string; proposal: PlanProposal }) =>
      acceptPlan(goalId, reviewedProposal, csrfToken),
    onSuccess: (result, variables) => {
      // Owner acceptance is the only action that materializes proposal tasks; refresh both derived domains.
      setReceipt(result);
      setConfirm(false);
      setConfirmation(null);
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
      queryClient.setQueryData(['goal', variables.goalId], result.goal);
    },
    onError: () => {
      setConfirm(false);
      setConfirmation(null);
    },
  });
}

/** Loading indicator and read-failure panel (with retry) for the live goal query behind the proposal review. */
function GoalReadStatus({ query }: { query: { isFetching: boolean; isError: boolean; error: unknown; refetch: () => unknown } }) {
  const t = useTranslations('taskGoal');
  return (
    <>
      {query.isFetching && (
        <p role="status" className="text-sm text-muted-foreground">
          {t('loadingGoalDetail')}
        </p>
      )}
      {query.isError && (
        <div role="alert" className="text-sm text-destructive">
          <p>{t(ownerFailureKey(query.error, 'goal.ownerMissing', 'proposal.goalReadFailed'))}</p>
          <Button type="button" variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
            {query.isFetching ? t('loading') : t('retry')}
          </Button>
        </div>
      )}
    </>
  );
}

/** Pure check that every proposal task has at most one deadline kind and any instant carries a timezone designator (Z or +hh:mm). */
function proposalDeadlinesAreValid(proposal: PlanProposal): boolean {
  return proposal.tasks.every((item) => {
    if (item.due_at && item.due_date) return false;
    return !item.due_at || /(Z|[+-]\d{2}:\d{2})$/i.test(item.due_at);
  });
}

/** Opens the acceptance confirmation; shows the "stale" label when the proposal revision no longer matches the goal. */
function AcceptPlanButton({ disabled, stale, onClick }: { disabled: boolean; stale: boolean; onClick: () => void }) {
  const t = useTranslations('taskGoal');
  return (
    <Button type="button" disabled={disabled} onClick={onClick}>
      {stale ? t('proposal.stale') : t('accept.plan')}
    </Button>
  );
}

/** One-line summary (title, revision, progress) of the verified live goal. */
function CurrentGoalLine({ goal }: { goal: Goal }) {
  const t = useTranslations('taskGoal');
  return <p className="text-sm">{t('current.goalRevision', { title: goal.title, revision: goal.revision, progress: goal.progress })}</p>;
}

/** Alert shown when a proposal task has both deadline kinds or an instant without a timezone. */
function InvalidDeadlinesAlert() {
  const t = useTranslations('taskGoal');
  return (
    <p role="alert" className="text-sm text-destructive">
      {t('invalidProposalTaskDue')}
    </p>
  );
}

/** Previews a stable proposal and binds explicit owner confirmation to the displayed goal, revision, and cloned content. */
// eslint-disable-next-line complexity -- stateful form with conflict-retention and per-field validation branches that mirror independent UI/request states; extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
export function ProposalReview({
  goal,
  proposal,
  csrfToken,
  onClose,
}: {
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
  const accept = usePlanAcceptance({ csrfToken, queryClient, setReceipt, setConfirm, setConfirmation });
  const conflict = accept.error instanceof ApiError && accept.error.status === 409;
  const displayedProposal = confirm && confirmation ? confirmation.proposal : proposal;
  const proposalDeadlinesValid = proposalDeadlinesAreValid(proposal);
  /** Loads the captured proposal goal for conflict review without changing its identity, revision, or content. */
  const refreshCurrentGoal = async () => {
    setRefreshing(true);
    setRefreshError('');
    setCurrentGoal(null);
    try {
      setCurrentGoal(await getGoal(accept.variables?.goalId ?? goal.id));
    } catch (error) {
      setRefreshError(t(ownerFailureKey(error, 'goal.ownerMissing', 'goal.readFailed')));
    } finally {
      setRefreshing(false);
    }
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border p-4">
      <ProposalReviewHeader
        proposal={displayedProposal}
        pending={accept.isPending}
        onClose={
          onClose &&
          (() => {
            if (accept.isPending) return;
            setConfirm(false);
            setConfirmation(null);
            onClose();
          })
        }
      />
      <GoalReadStatus query={goalQuery} />
      {ownerVerified && <CurrentGoalLine goal={current} />}
      <ProposalMilestones proposal={displayedProposal} />
      <ProposalTasks proposal={displayedProposal} />
      {!proposalDeadlinesValid && <InvalidDeadlinesAlert />}
      {receipt && <AcceptanceReceipt receipt={receipt} />}
      {accept.error && (
        <AcceptError
          error={accept.error}
          conflict={conflict}
          refreshing={refreshing}
          refreshError={refreshError}
          currentGoal={currentGoal}
          onRefresh={() => void refreshCurrentGoal()}
        />
      )}
      {!receipt && (
        <AcceptPlanButton
          disabled={!ownerVerified || accept.isPending || proposal.expected_revision !== current.revision || !proposalDeadlinesValid}
          stale={proposal.expected_revision !== current.revision}
          onClick={() => {
            setConfirmation({ goalId: goal.id, proposal: structuredClone(proposal) });
            setConfirm(true);
          }}
        />
      )}
      <ConfirmAcceptDialog
        open={confirm}
        pending={accept.isPending}
        confirmation={confirmation}
        goalId={goal.id}
        fallbackProposal={proposal}
        current={current}
        ownerVerified={ownerVerified}
        onOpenChange={(open) => {
          if (accept.isPending) return;
          setConfirm(open);
          if (!open) setConfirmation(null);
        }}
        onConfirm={(value) => accept.mutate(value)}
      />
    </section>
  );
}
