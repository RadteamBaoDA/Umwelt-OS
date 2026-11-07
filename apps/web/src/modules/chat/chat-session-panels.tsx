'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import type { PendingMessageMutation, PendingSpecialistRun } from '@/core/app-shell/chat-controller';
import { Button } from '@/components/ui/button';
import { AgentRunDetail, ConversationAgentActivity, type AgentRun } from '@/modules/agents';
import { profileTitleKeys } from './chat-session-header';

const runStatusKeys: Record<string, 'agentRunQueued' | 'agentRunRunning' | 'agentRunWaitingApproval' | 'agentRunSucceeded' | 'agentRunFailed' | 'agentRunCancelled'> = {
  queued: 'agentRunQueued', running: 'agentRunRunning', waiting_approval: 'agentRunWaitingApproval',
  succeeded: 'agentRunSucceeded', failed: 'agentRunFailed', cancelled: 'agentRunCancelled',
};

const ACTIVE_RUN_STATUSES = ['queued', 'running', 'waiting_approval'];

/** Props of {@link AgentPanels}. */
export interface AgentPanelsProps {
  conversationId?: string | null;
  csrfToken: string;
  /** True when a specialist is selected or a profile run is pending (plain Q&A shows no agent chrome). */
  agentMode: boolean;
  pendingProfileRun: PendingSpecialistRun | null;
  activeRun: AgentRun | undefined;
  runs: { items: AgentRun[] } | undefined;
  onCancelRun: () => void;
  cancelling: boolean;
  onOpenRun: (id: string) => void;
}

/**
 * Agent chrome above the transcript: pending-revision status, conversation agent activity, the opened run and history.
 *
 * @param props - See {@link AgentPanelsProps}.
 */
export function AgentPanels(props: AgentPanelsProps) {
  const { conversationId, csrfToken, agentMode, pendingProfileRun, activeRun, runs, onCancelRun, cancelling, onOpenRun } = props;
  const tAgents = useTranslations('aiSettings');
  return (
    <>
      {pendingProfileRun ? (
        <div role="status" className="shrink-0 border-b border-border px-4 py-2 text-xs text-muted-foreground">
          {tAgents(profileTitleKeys[pendingProfileRun.profileId] ?? 'agentProfileKnowledge')} · {tAgents('agentPendingProfileRevision', { revision: pendingProfileRun.profileRevision })}
        </div>
      ) : null}
      <ConversationAgentActivity
        conversationId={conversationId}
        csrfToken={csrfToken}
        watch={Boolean(pendingProfileRun) || Boolean(runs?.items.some((run) => ACTIVE_RUN_STATUSES.includes(run.status)))}
      />
      {agentMode && activeRun && (
        <div className="shrink-0 border-b border-border p-3">
          <AgentRunDetail run={activeRun} onCancel={onCancelRun} cancelling={cancelling} />
        </div>
      )}
      {agentMode && runs?.items.length ? (
        <details className="shrink-0 border-b border-border px-4 py-2">
          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">{tAgents('agentHistory')} ({runs.items.length})</summary>
          <div className="mt-2 flex flex-wrap gap-2">
            {runs.items.map((run) => <Button key={run.id} type="button" className="secondary h-auto rounded-md px-2 py-1 text-xs font-normal" onClick={() => onOpenRun(run.id)}>{run.agent_id === 'assistant' ? tAgents('agentAssistant') : tAgents(profileTitleKeys[run.agent_id] ?? 'agentProfileKnowledge')}: {tAgents(runStatusKeys[run.status] ?? 'agentRunFailed')}</Button>)}
          </div>
        </details>
      ) : null}
    </>
  );
}

/** Props of {@link RevisionBanners}. */
export interface RevisionBannersProps {
  mode: 'drawer' | 'full';
  pendingMessageMutation: PendingMessageMutation | null;
  effectiveEditingMessageId: string | null;
  busy: boolean;
  onRetryRevision: () => void;
  onCancelEdit: () => void;
}

/**
 * Full-mode banners for an unacknowledged revision (with Retry) or a prompt being edited (with Cancel).
 *
 * @param props - See {@link RevisionBannersProps}.
 */
export function RevisionBanners({ mode, pendingMessageMutation, effectiveEditingMessageId, busy, onRetryRevision, onCancelEdit }: RevisionBannersProps) {
  const t = useTranslations('chat');
  return (
    <>
      {mode === 'full' && pendingMessageMutation && (
        <div role="status" className="flex items-center justify-between gap-3 border-b border-border bg-secondary px-4 py-2 text-xs">
          <span className="text-muted-foreground">{t('revisionUnacknowledged')}</span>
          <Button type="button" className="secondary h-8 px-2 text-xs" disabled={busy} onClick={onRetryRevision}>
            {t('retryRevision')}
          </Button>
        </div>
      )}
      {mode === 'full' && effectiveEditingMessageId && !pendingMessageMutation && (
        <div className="flex items-center justify-between gap-3 border-b border-border bg-secondary px-4 py-2 text-xs">
          <span className="text-muted-foreground">{t('editingPrompt')}</span>
          <Button type="button" className="secondary h-8 px-2 text-xs" onClick={onCancelEdit}>
            {t('cancelEdit')}
          </Button>
        </div>
      )}
    </>
  );
}
