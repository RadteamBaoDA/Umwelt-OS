'use client';

import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { decideApproval, getConversationApprovals, type AgentApproval } from './api';
import { ApprovalCard } from './approval-card';

export interface ConversationAgentActivityProps {
  conversationId: string | null | undefined;
  csrfToken: string;
}

/** Discover linked approvals in full Chat and poll every ten seconds while the conversation is open.
 * Failed refreshes hide cached protected cards until a successful owner-scoped response arrives.
 */
export function ConversationAgentActivity({ conversationId, csrfToken }: ConversationAgentActivityProps) {
  const t = useTranslations('chat');
  const queryClient = useQueryClient();
  const queryKey = ['agent-approvals', conversationId];
  const approvals = useQuery({
    queryKey,
    queryFn: () => getConversationApprovals(conversationId!),
    enabled: Boolean(conversationId),
    // New actions arrive through a separate run endpoint while this conversation stays open.
    refetchInterval: 10_000,
  });
  const mutation = useMutation({
    mutationFn: ({ approval, decision }: { approval: AgentApproval; decision: 'approve' | 'deny' }) =>
      decideApproval(approval, decision, csrfToken),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey });
    },
  });

  if (!conversationId || (!approvals.data?.length && !approvals.isError)) return null;
  return (
    <aside aria-label={t('agentActivity')} className="shrink-0 space-y-3 border-b border-border p-4">
      <h2 className="text-sm font-semibold text-foreground">{t('agentActivity')}</h2>
      {approvals.isError && <p role="status" className="text-sm text-destructive">{t('approvalLoadFailed')}</p>}
      {mutation.isError && <p role="alert" className="text-sm text-destructive">{t('approvalDecisionFailed')}</p>}
      {!approvals.isError && approvals.data?.map((approval) => (
        <ApprovalCard
          key={approval.id}
          approval={approval}
          disabled={approvals.isError}
          busy={mutation.isPending && mutation.variables?.approval.id === approval.id}
          onDecision={(item, decision) => mutation.mutate({ approval: item, decision })}
        />
      ))}
    </aside>
  );
}
