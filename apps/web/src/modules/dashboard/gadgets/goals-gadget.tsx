'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest } from '@/core/api';
import type { GadgetInstance } from '../api';
import { GoalForm } from '@/modules/goals/goal-form';
import { GoalList } from '@/modules/goals/goal-list';
import { GoalDetail } from '@/modules/goals/goal-detail';
import { ProposalReview } from '@/modules/goals/proposal-review';
import { getTask } from '@/modules/tasks/api';
import { TaskDetail } from '@/modules/tasks/task-detail';
import type { Goal, PlanProposal } from '@/modules/goals/types';

/** Optional owner-prepared proposal input; proposal display alone never writes tasks. */
export type GoalProposalInput = { goalId: string; proposal: PlanProposal };

/** Props for a configured goals gadget and its optional stable plan proposal review. */
export interface GoalsGadgetProps { instance: GadgetInstance; proposal?: GoalProposalInput }

/** Renders configured owner goals and opens forms, details, linked task owners, or explicit proposal review. */
export function GoalsGadget({ instance, proposal }: GoalsGadgetProps) {
  const t = useTranslations('taskGoal');
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<Goal | null>(null);
  const [linkedTaskId, setLinkedTaskId] = useState<string | null>(null);
  const session = useQuery({ queryKey: ['session'], queryFn: () => apiRequest<{ authenticated: true; csrfToken: string }>('/api/v1/auth/session') });
  const linkedTask = useQuery({ queryKey: ['task', linkedTaskId], queryFn: () => getTask(linkedTaskId!), enabled: !!linkedTaskId });
  const definition = instance.definition;
  const ids = definition.scope.source_item_ids ?? [];
  const filters = definition.filters;
  const unsupported = Object.keys(filters).filter((key) => !['keywords', 'exclude_keywords', 'limit'].includes(key));
  const scopeKey = `${instance.id}:${definition.id}:${definition.revision}`;
  if (unsupported.length) return <div role="status" className="p-3 text-sm text-destructive">{t('unsupportedGoalFilters', { fields: unsupported.join(', ') })}</div>;
  if (session.isLoading) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('loadingSession')}</p>;
  if (!session.data) return <p role="alert" className="p-3 text-sm text-destructive">{t('sessionUnavailable')}</p>;
  const token = session.data.csrfToken;
  if (linkedTaskId) return linkedTask.data
    ? <TaskDetail task={linkedTask.data} csrfToken={token} onClose={() => setLinkedTaskId(null)} onTaskChanged={() => { void linkedTask.refetch(); }} />
    : <div className="p-3 text-sm" role={linkedTask.isError ? 'alert' : 'status'}>{linkedTask.isLoading ? t('loadingTask') : t('linkedTaskUnavailable')} <Button type="button" variant="outline" onClick={() => setLinkedTaskId(null)}>{t('back')}</Button></div>;
  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-3">
    {definition.source_ids.length > 0 && <p role="status" className="rounded border border-border p-2 text-xs text-muted-foreground">{t('goalSourceFilterUnavailable', { count: definition.source_ids.length })}</p>}
    {selected ? proposal && selected.id === proposal.goalId ? <ProposalReview goal={selected} proposal={proposal.proposal} csrfToken={token} onClose={() => setSelected(null)} /> : <GoalDetail goal={selected} csrfToken={token} onClose={() => setSelected(null)} onEditLinkedTask={setLinkedTaskId} /> : creating ? <GoalForm csrfToken={token} onSuccess={() => setCreating(false)} onCancel={() => setCreating(false)} /> : <>
      <header className="flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">{instance.title || t('goalsTitle')}</h2><Button type="button" size="sm" onClick={() => setCreating(true)}>{t('newGoal')}</Button></header>
      <GoalList ids={ids.length ? ids : undefined} filters={filters} limit={filters.limit ?? 50} cacheScope={scopeKey} onSelectGoal={setSelected} />
      {proposal && <p className="text-xs text-muted-foreground">{t('proposalAvailable')}</p>}
    </>}
  </section>;
}
