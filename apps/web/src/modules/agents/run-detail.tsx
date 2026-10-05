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
const activityStatusKeys: Record<string, 'agentRunSucceeded' | 'agentRunFailed' | 'agentRunRunning'> = {
  succeeded: 'agentRunSucceeded', running: 'agentRunRunning',
};

/** Displays durable status, counters and answer without exposing hidden model reasoning or action payloads. */
export function AgentRunDetail({ run, onCancel, cancelling = false }: AgentRunDetailProps) {
  const t = useTranslations('aiSettings');
  const statusKey = `agentRun${run.status.replace(/(^|_)([a-z])/g, (_, _separator: string, letter: string) => letter.toUpperCase())}` as
    | 'agentRunQueued' | 'agentRunRunning' | 'agentRunWaitingApproval' | 'agentRunSucceeded' | 'agentRunFailed' | 'agentRunCancelled';
  const active = run.status === 'queued' || run.status === 'running' || run.status === 'waiting_approval';
  // Concise tool status only: names and states, never arguments or results (server omits them).
  const toolActivities = run.activities.filter((item) => item.kind === 'tool' && item.tool_name).slice(-5);
  return (
    <section className="space-y-2 rounded-lg border border-border bg-background p-3" aria-label={t('agentRunDetail')}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium text-foreground">{t('agentRunStatus')}: {t(statusKey)}</p>
          <p className="text-xs text-muted-foreground">{t('agentRunCounters', { steps: run.steps, tools: run.tool_calls, seconds: run.active_seconds })}</p>
        </div>
        {active && onCancel && <Button type="button" className="secondary" disabled={cancelling} onClick={onCancel}>{t('agentCancelRun')}</Button>}
      </div>
      {toolActivities.length > 0 && (
        <ul className="space-y-1 text-xs text-muted-foreground" aria-label={t('agentToolActivity')}>
          {toolActivities.map((item, index) => (
            <li key={`${item.created_at}-${index}`}>{item.tool_name}: {t(activityStatusKeys[item.status] ?? 'agentRunFailed')}</li>
          ))}
        </ul>
      )}
      {run.error_code && <p className="text-sm text-destructive">{t('agentRunError')}: {run.error_code}</p>}
      <div className="whitespace-pre-wrap break-words text-sm text-foreground" aria-live="polite">
        {run.answer || t('agentNoAnswer')}
      </div>
    </section>
  );
}
