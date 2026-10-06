'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/core/api';
import { useChatController } from '@/core/app-shell/chat-controller';
import { decideAction, getRuleConversationId, listRuns, type Automation, type Run, type RunAction } from './api';

const activeStatuses = new Set(['queued', 'running', 'awaiting_approval']);

/** Renders one action outcome; a pending approval shows its exact target and the owner decision buttons. */
function ActionRow({ rule, run, action, csrfToken, onOpenChat }: {
  rule: Automation; run: Run; action: RunAction; csrfToken: string; onOpenChat: () => void;
}) {
  const t = useTranslations('automations');
  const client = useQueryClient();
  const decide = useMutation({
    mutationFn: (decision: 'approve' | 'deny') => decideAction(run.id, action.ordinal, decision, csrfToken),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['automation-runs', rule.id] }); },
  });
  // The rule definition is only comparable while the run's revision is still current; a pending
  // approval for an older revision has already been dropped by the server fence.
  const spec = run.revision === rule.revision ? rule.actions[action.ordinal - 1] : undefined;
  const pending = action.status === 'awaiting_approval';
  const [mountedAt] = useState(() => Date.now());
  const expired = pending && action.approval_expires_at !== null && Date.parse(action.approval_expires_at) <= mountedAt;
  const agentRun = action.result_reference?.startsWith('agent_run:');
  return <li className="space-y-2 rounded-md border border-border p-3 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <strong>{action.ordinal}. {t(`action_${action.type}`)}</strong>
      <span className="rounded-full bg-muted px-2 py-1 text-xs">{t(`actionStatus_${action.status}`)}</span>
    </div>
    {action.error_code && <p className="text-xs text-muted-foreground">{t('errorCode', { code: action.error_code })}</p>}
    {action.result_reference && <p className="break-all text-xs text-muted-foreground">{t('resultReference', { reference: action.result_reference })}</p>}
    {pending && <div role="group" aria-label={t('approvalTitle')} className="space-y-2 rounded-md border border-border bg-surface p-3">
      <p className="font-medium">{t('approvalTitle')}</p>
      {spec?.type === 'call_webhook' && <p>{t('approvalWebhook', { alias: spec.alias ?? '', event: spec.event ?? '' })}</p>}
      {spec?.type === 'run_agent' && <p>{t('approvalAgent', { profile: spec.profile_id ?? '', instruction: spec.instruction ?? '' })}</p>}
      {!spec && <p className="text-destructive">{t('approvalRevisionChanged')}</p>}
      {action.approval_expires_at && <p className="text-xs text-muted-foreground">{t('approvalExpires', { time: new Date(action.approval_expires_at).toLocaleString() })}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={decide.isPending || expired || !spec} onClick={() => decide.mutate('approve')}>{t('approve')}</Button>
        <Button type="button" variant="outline" disabled={decide.isPending} onClick={() => decide.mutate('deny')}>{t('deny')}</Button>
      </div>
      {decide.error && <p role="alert" className="text-sm text-destructive">{decide.error instanceof ApiError && decide.error.status === 409 ? t('decisionConflict') : t('decisionFailed')}</p>}
    </div>}
    {agentRun && <Button type="button" variant="outline" size="sm" onClick={onOpenChat}>{t('openInChat')}</Button>}
  </li>;
}

/** Run history for one rule: status, trigger, per-action outcomes, decidable approvals and Chat links. */
export function RunDetail({ rule, csrfToken, onBack }: { rule: Automation; csrfToken: string; onBack: () => void }) {
  const t = useTranslations('automations');
  const chat = useChatController();
  const runs = useQuery({
    queryKey: ['automation-runs', rule.id], queryFn: () => listRuns(rule.id),
    // Poll only while something is in flight so history stays live without constant traffic.
    refetchInterval: (query) => (query.state.data?.items.some((item) => activeStatuses.has(item.status)) ? 4000 : false),
  });
  const [chatMissing, setChatMissing] = useState(false);
  /** Resolves the rule conversation at click time (it may not exist when this view mounted) and opens it in Chat. */
  const openChat = async () => {
    try {
      const id = await getRuleConversationId(rule.id);
      setChatMissing(id === null);
      if (id) chat.openDrawer({ conversationId: id });
    } catch { setChatMissing(true); }
  };
  return <section className="space-y-4" aria-label={t('runHistory')}>
    <header className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-lg font-semibold">{t('runHistoryFor', { name: rule.name })}</h3>
      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={() => void runs.refetch()}>{t('refresh')}</Button>
        <Button type="button" variant="outline" onClick={onBack}>{t('back')}</Button>
      </div>
    </header>
    {chatMissing && <p role="status" className="text-sm text-destructive">{t('chatUnavailable')}</p>}
    {runs.isPending && <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p>}
    {runs.isError && <p role="alert" className="text-sm text-destructive">{t('loadFailed')}</p>}
    {runs.data && runs.data.items.length === 0 && <p className="text-sm text-muted-foreground">{t('noRuns')}</p>}
    <ul className="space-y-3">{runs.data?.items.map((run) => <li key={run.id} className="space-y-2 rounded-lg border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <strong className="text-sm">{t(`trigger_${run.trigger_type}`)} · {new Date(run.created_at).toLocaleString()}</strong>
        <span className="rounded-full bg-muted px-2 py-1 text-xs">{t(`runStatus_${run.status}`)}</span>
      </div>
      <p className="text-xs text-muted-foreground">{t('runMeta', { revision: run.revision, depth: run.depth, attempts: run.attempts })}{run.reason ? ` · ${t('reason', { code: run.reason })}` : ''}</p>
      {run.actions.length > 0 && <ul className="space-y-2">{run.actions.map((action) => <ActionRow key={action.ordinal} rule={rule} run={run} action={action} csrfToken={csrfToken} onOpenChat={() => void openChat()} />)}</ul>}
    </li>)}</ul>
  </section>;
}
