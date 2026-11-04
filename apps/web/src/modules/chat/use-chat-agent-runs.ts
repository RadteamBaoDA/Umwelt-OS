import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { cancelAgentRun, getAgentProfiles, getAgentRun, listAgentRuns } from '@/modules/agents';

/** Agent run statuses that keep polling. */
const ACTIVE_RUN_STATUSES = ['queued', 'running', 'waiting_approval'];

/**
 * Specialist-agent data for the chat surface: profiles, the conversation's run history, the opened run and its cancel.
 *
 * @param params.conversationId - Conversation whose runs are listed.
 * @param params.mode - Profiles load only in full mode.
 * @param params.agentMode - True when a specialist is selected or a profile run is pending; gates the run queries.
 * @param params.csrfToken - Token for the cancel mutation.
 * @returns Query results, the opened run id with its setter, and the cancel mutation.
 */
export function useChatAgentRuns(params: { conversationId?: string | null; mode: 'drawer' | 'full'; agentMode: boolean; csrfToken: string }) {
  const { conversationId, mode, agentMode, csrfToken } = params;
  const queryClient = useQueryClient();
  const [activeAgentRunId, setActiveAgentRunId] = React.useState<string | null>(null);
  const profiles = useQuery({ queryKey: ['agent-profiles'], queryFn: getAgentProfiles, enabled: mode === 'full' });
  const agentRuns = useQuery({
    queryKey: ['agent-runs', 'conversation', conversationId],
    queryFn: () => listAgentRuns({ conversation_id: conversationId!, limit: 25 }),
    enabled: agentMode && Boolean(conversationId),
    refetchInterval: (query) => query.state.data?.items.some((run) => ACTIVE_RUN_STATUSES.includes(run.status)) ? 3000 : false,
  });
  const activeAgentRun = useQuery({
    queryKey: ['agent-run', activeAgentRunId],
    queryFn: () => getAgentRun(activeAgentRunId!),
    enabled: Boolean(activeAgentRunId),
    refetchInterval: (query) => query.state.data && ACTIVE_RUN_STATUSES.includes(query.state.data.status) ? 3000 : false,
  });
  const cancelAgent = useMutation({
    mutationFn: () => cancelAgentRun(activeAgentRunId!, csrfToken),
    onSuccess: (run) => {
      queryClient.setQueryData(['agent-run', run.id], run);
      void queryClient.invalidateQueries({ queryKey: ['agent-runs', 'conversation', conversationId] });
    },
  });
  return { profiles, agentRuns, activeAgentRun, cancelAgent, activeAgentRunId, setActiveAgentRunId };
}
