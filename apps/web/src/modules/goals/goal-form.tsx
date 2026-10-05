'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ApiError } from '@/core/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { createGoal, getGoal, updateGoal } from './api';
import type { Goal, GoalCreate, GoalStatus, Milestone } from './types';

const GOAL_STATUSES: GoalStatus[] = ['active', 'completed', 'paused', 'cancelled'];

/** Creates or edits goal fields with conflict draft retention; save requires current owner verification and pending state is reported to the parent. */
export function GoalForm({ goal, csrfToken, onSuccess, onCancel, canSave = true, onPendingChange }: {
  goal?: Goal;
  csrfToken: string;
  onSuccess?: (goal: Goal) => void;
  onCancel?: () => void;
  canSave?: boolean;
  onPendingChange?: (pending: boolean) => void;
}) {
  const t = useTranslations('taskGoal');
  const goalStatusLabels: Record<GoalStatus, string> = { active: t('goalStatusActive'), completed: t('goalStatusCompleted'), paused: t('goalStatusPaused'), cancelled: t('goalStatusCancelled') };
  const [title, setTitle] = useState(goal?.title ?? '');
  const [description, setDescription] = useState(goal?.description ?? '');
  const [outcome, setOutcome] = useState(goal?.desired_outcome ?? '');
  const [deadline, setDeadline] = useState(goal?.deadline ?? '');
  const [manual, setManual] = useState(goal?.manual_progress ?? false);
  const [progress, setProgress] = useState(String(goal?.progress ?? 0));
  const [status, setStatus] = useState<GoalStatus>(goal?.status ?? 'active');
  const [entityIds, setEntityIds] = useState(goal?.entity_ids.join(', ') ?? '');
  const [expectedRevision, setExpectedRevision] = useState(goal?.revision ?? null);
  const [milestones, setMilestones] = useState<Milestone[]>(goal?.milestones ?? []);
  const [newMilestone, setNewMilestone] = useState('');
  const [currentGoal, setCurrentGoal] = useState<Goal | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => {
      const values = { title: title.trim(), description: description.trim() || null, desired_outcome: outcome.trim() || null, deadline: deadline || null, status, milestones, entity_ids: [...new Set(entityIds.split(/[\s,]+/).filter(Boolean))] };
      return goal
        ? updateGoal(goal.id, { ...values, manual_progress: manual, progress: manual ? Number(progress) : null, expected_revision: expectedRevision ?? goal.revision }, csrfToken)
        : createGoal({ ...values, manual_progress: manual, progress: manual ? Number(progress) : undefined }, csrfToken);
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: ['goals'] });
      queryClient.setQueryData(['goal', saved.id], saved);
      onSuccess?.(saved);
    },
    onMutate: () => onPendingChange?.(true),
    onSettled: () => onPendingChange?.(false),
  });
  const conflict = mutation.error instanceof ApiError && mutation.error.status === 409;
  const saveError = mutation.error instanceof ApiError && mutation.error.status === 404 ? t('goalOwnerMissing') : mutation.error instanceof ApiError && [401, 403].includes(mutation.error.status) ? t('ownerAccessDenied') : t('goalWriteFailed');

  /** Loads the latest goal revision for review while leaving every draft field unchanged. */
  const refreshCurrentGoal = async () => {
    if (!goal) return;
    setRefreshing(true);
    setRefreshError('');
    setCurrentGoal(null);
    try { setCurrentGoal(await getGoal(goal.id)); }
    catch (error) { setRefreshError(error instanceof ApiError && error.status === 404 ? t('goalOwnerMissing') : error instanceof ApiError && [401, 403].includes(error.status) ? t('ownerAccessDenied') : t('goalReadFailed')); }
    finally { setRefreshing(false); }
  };

  /** Adds a stable draft milestone while preserving task-linked completion as owner-derived state. */
  const addMilestone = () => {
    const value = newMilestone.trim();
    if (!value || milestones.length >= 100) return;
    setMilestones((items) => [...items, { id: crypto.randomUUID(), title: value, completed: false, due_date: null, order: items.length, task_id: null }]);
    setNewMilestone('');
  };

  return <form onSubmit={(event) => { event.preventDefault(); if (canSave && !mutation.isPending && title.trim()) mutation.mutate(); }} className="flex flex-col gap-3 rounded-lg border border-border p-4">
    {goal && <p className="text-xs text-muted-foreground">{t('editingRevision', { revision: expectedRevision ?? goal.revision })}</p>}
    <div><Label htmlFor="goal-title">{t('title')}</Label><Input id="goal-title" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={500} /></div>
    <div><Label htmlFor="goal-description">{t('description')}</Label><Input id="goal-description" value={description} onChange={(event) => setDescription(event.target.value)} /></div>
    <div><Label htmlFor="goal-outcome">{t('desiredOutcome')}</Label><Input id="goal-outcome" value={outcome} onChange={(event) => setOutcome(event.target.value)} /></div>
    <div><Label htmlFor="goal-deadline">{t('deadline')}</Label><Input id="goal-deadline" type="date" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></div>
    <div><Label htmlFor="goal-entities">{t('entityReferencesLabel')}</Label><Input id="goal-entities" value={entityIds} onChange={(event) => setEntityIds(event.target.value)} aria-describedby="goal-entities-help" /><p id="goal-entities-help" className="text-xs text-muted-foreground">{t('entityReferencesHelp')}</p></div>
    <div><Label htmlFor="goal-status">{t('status')}</Label><Select value={status} onValueChange={(value) => setStatus(value as GoalStatus)}><SelectTrigger id="goal-status"><SelectValue /></SelectTrigger><SelectContent>{GOAL_STATUSES.map((item) => <SelectItem key={item} value={item}>{goalStatusLabels[item]}</SelectItem>)}</SelectContent></Select></div>
    <label className="flex items-center gap-2 text-sm"><Checkbox checked={manual} onCheckedChange={(checked) => setManual(checked === true)} /> {t('manualProgress')}</label>
    {manual && <div><Label htmlFor="goal-progress">{t('progressRange')}</Label><Input id="goal-progress" type="number" min="0" max="100" step="0.1" value={progress} onChange={(event) => setProgress(event.target.value)} /></div>}
    <fieldset className="flex flex-col gap-2"><legend className="text-sm font-medium">{t('milestones')}</legend>{milestones.map((item, index) => <div key={item.id} className="flex items-center gap-2">{item.task_id ? <span className="min-w-0 flex-1 text-sm">{item.title} · {t('linkedTask', { id: item.task_id })}</span> : <><Input aria-label={t('milestoneTitle')} value={item.title} onChange={(event) => setMilestones((items) => items.map((value, i) => i === index ? { ...value, title: event.target.value } : value))} maxLength={500} /><Input aria-label={t('milestoneDeadline')} type="date" value={item.due_date ?? ''} onChange={(event) => setMilestones((items) => items.map((value, i) => i === index ? { ...value, due_date: event.target.value || null } : value))} /><Button type="button" variant="outline" size="sm" onClick={() => setMilestones((items) => items.filter((_, i) => i !== index).map((value, order) => ({ ...value, order })))}>{t('remove')}</Button></>}{item.task_id && <span className="text-xs text-muted-foreground">{t('linkedCompletionOwner')}</span>}</div>)}<div className="flex gap-2"><Input aria-label={t('newMilestone')} value={newMilestone} onChange={(event) => setNewMilestone(event.target.value)} maxLength={500} /><Button type="button" variant="outline" onClick={addMilestone} disabled={!newMilestone.trim() || milestones.length >= 100}>{t('addMilestone')}</Button></div></fieldset>
    {mutation.error && <div role="alert" className="text-sm text-destructive"><p>{conflict ? t('goalConflictDraftRetained') : saveError}</p>{conflict && <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={() => void refreshCurrentGoal()}>{refreshing ? t('loading') : t('loadCurrentGoal')}</Button>}{refreshError && <p>{refreshError}</p>}{currentGoal && <div className="rounded border border-border p-2"><p>{t('currentGoalRevision', { title: currentGoal.title, revision: currentGoal.revision, progress: currentGoal.progress })}</p><p>{currentGoal.desired_outcome ?? t('none')} · {currentGoal.deadline ?? t('noDeadline')} · {currentGoal.manual_progress ? t('manual') : t('milestoneDerived')}</p><p>{t('entityReferences', { ids: currentGoal.entity_ids.join(', ') || t('none') })}</p><ul>{currentGoal.milestones.map((item) => <li key={item.id}>{item.title} · {item.completed ? t('complete') : t('incomplete')}{item.task_id ? ` · ${t('linkedTask', { id: item.task_id })}` : ''}</li>)}</ul><Button type="button" variant="outline" size="sm" onClick={() => { setExpectedRevision(currentGoal.revision); setCurrentGoal(null); mutation.reset(); }}>{t('useCurrentRevisionKeepDraft', { revision: currentGoal.revision })}</Button></div>}</div>}
    <div className="flex justify-end gap-2">{onCancel && <Button type="button" variant="outline" disabled={mutation.isPending} onClick={onCancel}>{t('cancel')}</Button>}<Button type="submit" disabled={!canSave || mutation.isPending || !title.trim()}>{mutation.isPending ? t('saving') : goal ? t('saveGoal') : t('createGoal')}</Button></div>
  </form>;
}
