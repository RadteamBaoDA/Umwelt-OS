'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import type { AgentRun } from './api';

/** Bounded run detail view shared by Settings history and full Chat. */
export interface AgentRunDetailProps {
  run: AgentRun;
  onCancel?: () => void;
  cancelling?: boolean;
}

/** Localized label keys for tool activity states; denied and review-only outcomes read as failed. */
const activityStatusKeys: Record<string, 'agent.runSucceeded' | 'agent.runFailed' | 'agent.runRunning'> = {
  succeeded: 'agent.runSucceeded',
  running: 'agent.runRunning',
};

/** Literal catalog key per durable run status (typed lookup instead of building the key from the status string). */
const RUN_STATUS_KEYS = {
  queued: 'agent.runQueued',
  running: 'agent.runRunning',
  waiting_approval: 'agent.runWaitingApproval',
  succeeded: 'agent.runSucceeded',
  failed: 'agent.runFailed',
  cancelled: 'agent.runCancelled',
} as const;

/** Displays durable status, counters and answer without exposing hidden model reasoning or action payloads. */
export function AgentRunDetail({ run, onCancel, cancelling = false }: AgentRunDetailProps) {
  const t = useTranslations('aiSettings');
  const statusKey = RUN_STATUS_KEYS[run.status];
  const active = run.status === 'queued' || run.status === 'running' || run.status === 'waiting_approval';
  // Concise tool status only: names and states, never arguments or results (server omits them).
  const toolActivities = run.activities.filter((item) => item.kind === 'tool' && item.tool_name).slice(-5);
  return (
    <section className="space-y-2 rounded-lg border border-border bg-background p-3" aria-label={t('agent.runDetail')}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium text-foreground">
            {t('agent.runStatus')}: {t(statusKey)}
          </p>
          <p className="text-xs text-muted-foreground">
            {t('agent.runCounters', { steps: run.steps, tools: run.tool_calls, seconds: run.active_seconds })}
          </p>
        </div>
        {active && onCancel && (
          <Button type="button" variant="outline" disabled={cancelling} onClick={onCancel}>
            {t('agent.cancelRun')}
          </Button>
        )}
      </div>
      {toolActivities.length > 0 && (
        <ul className="space-y-1 text-xs text-muted-foreground" aria-label={t('agent.toolActivity')}>
          {toolActivities.map((item, index) => (
            <li key={`${item.created_at}-${index}`}>
              {item.tool_name}: {t(activityStatusKeys[item.status] ?? 'agent.runFailed')}
            </li>
          ))}
        </ul>
      )}
      {run.error_code && (
        <p className="text-sm text-destructive">
          {t('agent.runError')}: {run.error_code}
        </p>
      )}
      <div className="whitespace-pre-wrap break-words text-sm text-foreground" aria-live="polite">
        {run.answer || t('agent.noAnswer')}
      </div>
    </section>
  );
}
