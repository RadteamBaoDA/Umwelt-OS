'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { useWorkspace } from '@/core/workspace-context';
import { unsupportedListFilters, type GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { GoalForm } from '@/modules/goals';
import { GoalList } from '@/modules/goals';
import { GoalDetail } from '@/modules/goals';
import { ProposalReview } from '@/modules/goals';
import { getTask } from '@/modules/tasks';
import { TaskDetail } from '@/modules/tasks';
import type { Goal, PlanProposal } from '@/modules/goals';
import { useSession } from '@/core/session';

/** Optional owner-prepared proposal input; proposal display alone never writes tasks. */
export type GoalProposalInput = { goalId: string; proposal: PlanProposal };

/** Props for a configured goals gadget and its optional stable plan proposal review. */
export interface GoalsGadgetProps {
  instance: GadgetInstance;
  proposal?: GoalProposalInput;
}

/** Inputs of the goals gadget body. */
interface GoalsBodyProps {
  selected: Goal | null;
  setSelected: (goal: Goal | null) => void;
  creating: boolean;
  setCreating: (creating: boolean) => void;
  isAdminViewer: boolean;
  proposal?: GoalProposalInput;
  token: string;
  setLinkedTaskId: (id: string | null) => void;
  ids: string[];
  filters: GadgetInstance['definition']['filters'];
  scopeKey: string;
}

/**
 * Body of the goals gadget: the selected goal (proposal review or detail), the create form, or the goal list.
 * Administrator viewers never see the create form or the proposal review.
 */
function GoalsBody({
  selected,
  setSelected,
  creating,
  setCreating,
  isAdminViewer,
  proposal,
  token,
  setLinkedTaskId,
  ids,
  filters,
  scopeKey,
}: GoalsBodyProps) {
  const t = useTranslations('taskGoal');
  if (selected) {
    return !isAdminViewer && proposal && selected.id === proposal.goalId ? (
      <ProposalReview goal={selected} proposal={proposal.proposal} csrfToken={token} onClose={() => setSelected(null)} />
    ) : (
      <GoalDetail goal={selected} csrfToken={token} onClose={() => setSelected(null)} onEditLinkedTask={setLinkedTaskId} />
    );
  }
  if (creating && !isAdminViewer)
    return <GoalForm csrfToken={token} onSuccess={() => setCreating(false)} onCancel={() => setCreating(false)} />;
  return (
    <>
      {/* The gadget frame already shows the title; only the create action remains. */}
      <header className="flex items-center justify-end gap-2">
        {!isAdminViewer && (
          <Button type="button" size="sm" onClick={() => setCreating(true)}>
            {t('newGoal')}
          </Button>
        )}
      </header>
      <GoalList
        hideTitle
        ids={ids.length ? ids : undefined}
        filters={filters}
        limit={filters.limit ?? 50}
        cacheScope={scopeKey}
        onSelectGoal={setSelected}
      />
      {proposal && <p className="text-xs text-muted-foreground">{t('proposalAvailable')}</p>}
    </>
  );
}

/** Renders configured goals and opens details; administrator viewers cannot create goals or act on proposals. */
export function GoalsGadget({ instance, proposal }: GoalsGadgetProps) {
  const t = useTranslations('taskGoal');
  const { isAdminViewer } = useWorkspace();
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Goal | null>(null);
  const [linkedTaskId, setLinkedTaskId] = useState<string | null>(null);
  const session = useSession();
  const linkedTask = useQuery({ queryKey: ['task', linkedTaskId], queryFn: () => getTask(linkedTaskId!), enabled: !!linkedTaskId });
  const definition = instance.definition;
  const ids = definition.scope.source_item_ids ?? [];
  const filters = definition.filters;
  const unsupported = unsupportedListFilters(filters);
  const scopeKey = `${instance.id}:${definition.id}:${definition.revision}`;
  if (unsupported.length) return <GadgetNotice kind="error" message={t('unsupportedGoalFilters', { fields: unsupported.join(', ') })} />;
  if (session.isLoading) return <GadgetSkeleton variant="list" label={t('loadingSession')} />;
  if (!session.data) return <GadgetNotice kind="error" message={t('sessionUnavailable')} />;
  const token = session.data.csrfToken;
  if (linkedTaskId)
    return linkedTask.data ? (
      <TaskDetail
        task={linkedTask.data}
        csrfToken={token}
        onClose={() => setLinkedTaskId(null)}
        onTaskChanged={() => {
          void linkedTask.refetch();
        }}
      />
    ) : (
      <div className="p-3 text-sm" role={linkedTask.isError ? 'alert' : 'status'}>
        {linkedTask.isLoading ? t('loadingTask') : t('linkedTaskUnavailable')}{' '}
        <Button type="button" variant="outline" onClick={() => setLinkedTaskId(null)}>
          {t('back')}
        </Button>
      </div>
    );
  return (
    <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-3">
      {definition.source_ids.length > 0 && (
        <p role="status" className="rounded border border-border p-2 text-xs text-muted-foreground">
          {t('goalSourceFilterUnavailable', { count: definition.source_ids.length })}
        </p>
      )}
      <GoalsBody
        selected={selected}
        setSelected={setSelected}
        creating={creating}
        setCreating={setCreating}
        isAdminViewer={isAdminViewer}
        proposal={proposal}
        token={token}
        setLinkedTaskId={setLinkedTaskId}
        ids={ids}
        filters={filters}
        scopeKey={scopeKey}
      />
    </section>
  );
}
