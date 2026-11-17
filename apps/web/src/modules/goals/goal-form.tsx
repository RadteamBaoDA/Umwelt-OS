'use client';

import { useState, type Dispatch, type SetStateAction } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { ownerFailureKey } from '@/modules/shared';
import { ApiError } from '@/core/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { CurrentGoalReview } from './goal-current-review';
import { createGoal, getGoal, updateGoal } from './api';
import type { Goal, GoalStatus, Milestone } from './types';
import { uuid7 } from '@/core/uuid7';

const GOAL_STATUSES: GoalStatus[] = ['active', 'completed', 'paused', 'cancelled'];

/**
 * Draft milestone editor. Task-linked milestones are shown read-only (completion is owner-derived);
 * others can be renamed, re-dated or removed (re-numbering `order`), and a new one added up to 100.
 */
function MilestoneEditor({
  milestones,
  setMilestones,
  newMilestone,
  setNewMilestone,
  onAdd,
}: {
  milestones: Milestone[];
  setMilestones: Dispatch<SetStateAction<Milestone[]>>;
  newMilestone: string;
  setNewMilestone: (value: string) => void;
  onAdd: () => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="text-sm font-medium">{t('milestones')}</legend>
      {milestones.map((item, index) => (
        <div key={item.id} className="flex items-center gap-2">
          {item.task_id ? (
            <span className="min-w-0 flex-1 text-sm">
              {item.title} · {t('linked.task', { id: item.task_id })}
            </span>
          ) : (
            <>
              <Input
                aria-label={t('milestone.title')}
                value={item.title}
                onChange={(event) =>
                  setMilestones((items) => items.map((value, i) => (i === index ? { ...value, title: event.target.value } : value)))
                }
                maxLength={500}
              />
              <Input
                aria-label={t('milestone.deadline')}
                type="date"
                value={item.due_date ?? ''}
                onChange={(event) =>
                  setMilestones((items) =>
                    items.map((value, i) => (i === index ? { ...value, due_date: event.target.value || null } : value)),
                  )
                }
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setMilestones((items) => items.filter((_, i) => i !== index).map((value, order) => ({ ...value, order })))}
              >
                {t('remove')}
              </Button>
            </>
          )}
          {item.task_id && <span className="text-xs text-muted-foreground">{t('linked.completionOwner')}</span>}
        </div>
      ))}
      <div className="flex gap-2">
        <Input
          aria-label={t('new.milestone')}
          value={newMilestone}
          onChange={(event) => setNewMilestone(event.target.value)}
          maxLength={500}
        />
        <Button type="button" variant="outline" onClick={onAdd} disabled={!newMilestone.trim() || milestones.length >= 100}>
          {t('addMilestone')}
        </Button>
      </div>
    </fieldset>
  );
}

/** Failure panel for a rejected goal save; on 409 it can load the latest goal and adopt its revision while keeping the draft. */
function GoalSaveError({
  conflict,
  saveError,
  refreshing,
  refreshError,
  currentGoal,
  onRefresh,
  onUseRevision,
}: {
  conflict: boolean;
  saveError: string;
  refreshing: boolean;
  refreshError: string;
  currentGoal: Goal | null;
  onRefresh: () => void;
  onUseRevision: (goal: Goal) => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <div role="alert" className="text-sm text-destructive">
      <p>{conflict ? t('goal.conflictDraftRetained') : saveError}</p>
      {conflict && (
        <Button type="button" variant="outline" size="sm" disabled={refreshing} onClick={onRefresh}>
          {refreshing ? t('loading') : t('load.currentGoal')}
        </Button>
      )}
      {refreshError && <p>{refreshError}</p>}
      {currentGoal && (
        <CurrentGoalReview
          goal={currentGoal}
          acceptLabel={t('use.currentRevisionKeepDraft', { revision: currentGoal.revision })}
          onAccept={() => onUseRevision(currentGoal)}
        />
      )}
    </div>
  );
}

type GoalFieldValues = {
  title: string;
  description: string;
  outcome: string;
  deadline: string;
  entityIds: string;
  status: GoalStatus;
  manual: boolean;
  progress: string;
};

type GoalFieldSetters = {
  setTitle: (value: string) => void;
  setDescription: (value: string) => void;
  setOutcome: (value: string) => void;
  setDeadline: (value: string) => void;
  setEntityIds: (value: string) => void;
  setStatus: (value: GoalStatus) => void;
  setManual: (value: boolean) => void;
  setProgress: (value: string) => void;
};

/** Controlled scalar goal inputs (title through manual progress); the progress input shows only in manual mode. */
function GoalFields({ values, setters }: { values: GoalFieldValues; setters: GoalFieldSetters }) {
  const t = useTranslations('taskGoal');
  const { title, description, outcome, deadline, entityIds, status, manual, progress } = values;
  const { setTitle, setDescription, setOutcome, setDeadline, setEntityIds, setStatus, setManual, setProgress } = setters;
  const goalStatusLabels: Record<GoalStatus, string> = {
    active: t('goal.statusActive'),
    completed: t('goal.statusCompleted'),
    paused: t('goal.statusPaused'),
    cancelled: t('goal.statusCancelled'),
  };
  return (
    <>
      <div>
        <Label htmlFor="goal-title">{t('title')}</Label>
        <Input id="goal-title" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={500} />
      </div>
      <div>
        <Label htmlFor="goal-description">{t('description')}</Label>
        <Input id="goal-description" value={description} onChange={(event) => setDescription(event.target.value)} />
      </div>
      <div>
        <Label htmlFor="goal-outcome">{t('desiredOutcome')}</Label>
        <Input id="goal-outcome" value={outcome} onChange={(event) => setOutcome(event.target.value)} />
      </div>
      <div>
        <Label htmlFor="goal-deadline">{t('deadline')}</Label>
        <Input id="goal-deadline" type="date" value={deadline} onChange={(event) => setDeadline(event.target.value)} />
      </div>
      <div>
        <Label htmlFor="goal-entities">{t('entity.referencesLabel')}</Label>
        <Input
          id="goal-entities"
          value={entityIds}
          onChange={(event) => setEntityIds(event.target.value)}
          aria-describedby="goal-entities-help"
        />
        <p id="goal-entities-help" className="text-xs text-muted-foreground">
          {t('entity.referencesHelp')}
        </p>
      </div>
      <div>
        <Label htmlFor="goal-status">{t('status')}</Label>
        <Select value={status} onValueChange={(value) => setStatus(value as GoalStatus)}>
          <SelectTrigger id="goal-status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {GOAL_STATUSES.map((item) => (
              <SelectItem key={item} value={item}>
                {goalStatusLabels[item]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <Checkbox checked={manual} onCheckedChange={(checked) => setManual(checked === true)} /> {t('manualProgress')}
      </label>
      {manual && (
        <div>
          <Label htmlFor="goal-progress">{t('progressRange')}</Label>
          <Input
            id="goal-progress"
            type="number"
            min="0"
            max="100"
            step="0.1"
            value={progress}
            onChange={(event) => setProgress(event.target.value)}
          />
        </div>
      )}
    </>
  );
}

/**
 * Declares the create/update mutation for the goal form. Updates send `expectedRevision` (falling
 * back to the goal's own revision) so concurrent edits surface as 409; success refreshes caches,
 * then calls `onSuccess`. `onPendingChange` fires true on mutate and false on settle.
 */
function useGoalSaveMutation({
  goal,
  csrfToken,
  values: draft,
  expectedRevision,
  onSuccess,
  onPendingChange,
}: {
  goal?: Goal;
  csrfToken: string;
  values: {
    title: string;
    description: string;
    outcome: string;
    deadline: string;
    status: GoalStatus;
    milestones: Milestone[];
    entityIds: string;
    manual: boolean;
    progress: string;
  };
  expectedRevision: number | null;
  onSuccess?: (goal: Goal) => void;
  onPendingChange?: (pending: boolean) => void;
}) {
  const { title, description, outcome, deadline, status, milestones, entityIds, manual, progress } = draft;
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => {
      const values = {
        title: title.trim(),
        description: description.trim() || null,
        desired_outcome: outcome.trim() || null,
        deadline: deadline || null,
        status,
        milestones,
        entity_ids: [...new Set(entityIds.split(/[\s,]+/).filter(Boolean))],
      };
      return goal
        ? updateGoal(
            goal.id,
            {
              ...values,
              manual_progress: manual,
              progress: manual ? Number(progress) : null,
              expected_revision: expectedRevision ?? goal.revision,
            },
            csrfToken,
          )
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
}

/** Cancel (when provided) and submit buttons; submit text reflects pending/edit state. */
function GoalFormActions({
  pending,
  canSubmit,
  isEdit,
  onCancel,
}: {
  pending: boolean;
  canSubmit: boolean;
  isEdit: boolean;
  onCancel?: () => void;
}) {
  const t = useTranslations('taskGoal');
  return (
    <div className="flex justify-end gap-2">
      {onCancel && (
        <Button type="button" variant="outline" disabled={pending} onClick={onCancel}>
          {t('cancel')}
        </Button>
      )}
      <Button type="submit" disabled={!canSubmit || pending}>
        {pending ? t('saving') : isEdit ? t('saveGoal') : t('createGoal')}
      </Button>
    </div>
  );
}

/** Builds an unsaved, task-unlinked milestone with a fresh uuid7 id at position `order`. */
function draftMilestone(title: string, order: number): Milestone {
  return { id: uuid7(), title, completed: false, due_date: null, order, task_id: null };
}

/** Creates or edits goal fields with conflict draft retention; save requires current owner verification and pending state is reported to the parent. */
// eslint-disable-next-line complexity -- stateful form with conflict-retention and per-field validation branches that mirror independent UI/request states; extraction into per-state components is tracked as follow-up R3.6 so this refactor stays behaviour-neutral
export function GoalForm({
  goal,
  csrfToken,
  onSuccess,
  onCancel,
  canSave = true,
  onPendingChange,
}: {
  goal?: Goal;
  csrfToken: string;
  onSuccess?: (goal: Goal) => void;
  onCancel?: () => void;
  canSave?: boolean;
  onPendingChange?: (pending: boolean) => void;
}) {
  const t = useTranslations('taskGoal');
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
  const mutation = useGoalSaveMutation({
    goal,
    csrfToken,
    values: { title, description, outcome, deadline, status, milestones, entityIds, manual, progress },
    expectedRevision,
    onSuccess,
    onPendingChange,
  });
  const conflict = mutation.error instanceof ApiError && mutation.error.status === 409;
  const saveError = t(ownerFailureKey(mutation.error, 'goal.ownerMissing', 'goal.writeFailed'));

  /** Loads the latest goal revision for review while leaving every draft field unchanged. */
  const refreshCurrentGoal = async () => {
    if (!goal) return;
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

  /** Adds a stable draft milestone while preserving task-linked completion as owner-derived state. */
  const addMilestone = () => {
    const value = newMilestone.trim();
    if (!value || milestones.length >= 100) return;
    setMilestones((items) => [...items, draftMilestone(value, items.length)]);
    setNewMilestone('');
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (canSave && !mutation.isPending && title.trim()) mutation.mutate();
      }}
      className="flex flex-col gap-3 rounded-lg border border-border p-4"
    >
      {goal && <p className="text-xs text-muted-foreground">{t('editingRevision', { revision: expectedRevision ?? goal.revision })}</p>}
      <GoalFields
        values={{ title, description, outcome, deadline, entityIds, status, manual, progress }}
        setters={{ setTitle, setDescription, setOutcome, setDeadline, setEntityIds, setStatus, setManual, setProgress }}
      />
      <MilestoneEditor
        milestones={milestones}
        setMilestones={setMilestones}
        newMilestone={newMilestone}
        setNewMilestone={setNewMilestone}
        onAdd={addMilestone}
      />
      {mutation.error && (
        <GoalSaveError
          conflict={conflict}
          saveError={saveError}
          refreshing={refreshing}
          refreshError={refreshError}
          currentGoal={currentGoal}
          onRefresh={() => void refreshCurrentGoal()}
          onUseRevision={(latest) => {
            setExpectedRevision(latest.revision);
            setCurrentGoal(null);
            mutation.reset();
          }}
        />
      )}
      <GoalFormActions pending={mutation.isPending} canSubmit={canSave && !!title.trim()} isEdit={!!goal} onCancel={onCancel} />
    </form>
  );
}
