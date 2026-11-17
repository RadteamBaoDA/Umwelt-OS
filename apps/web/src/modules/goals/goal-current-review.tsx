'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import type { Goal } from './types';

/** Read-only snapshot of the server's current goal with a button that adopts its revision for the next write. */
export function CurrentGoalReview({ goal, acceptLabel, onAccept }: { goal: Goal; acceptLabel: string; onAccept: () => void }) {
  const t = useTranslations('taskGoal');
  return (
    <div className="rounded border border-border p-2">
      <p>{t('current.goalRevision', { title: goal.title, revision: goal.revision, progress: goal.progress })}</p>
      <p>
        {goal.desired_outcome ?? t('none')} · {goal.deadline ?? t('no.deadline')} ·{' '}
        {goal.manual_progress ? t('manual') : t('milestone.derived')}
      </p>
      <p>{t('entity.references', { ids: goal.entity_ids.join(', ') || t('none') })}</p>
      <ul>
        {goal.milestones.map((item) => (
          <li key={item.id}>
            {item.title} · {item.completed ? t('complete') : t('incomplete')}
            {item.task_id ? ` · ${t('linked.task', { id: item.task_id })}` : ''}
          </li>
        ))}
      </ul>
      <Button type="button" variant="outline" size="sm" onClick={onAccept}>
        {acceptLabel}
      </Button>
    </div>
  );
}
