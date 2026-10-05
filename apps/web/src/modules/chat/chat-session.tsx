'use client';

import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { FileTextIcon, PlusIcon, XIcon } from 'lucide-react';
import {
  cancelResponse,
  chatKeys,
  createConversation,
  getConversation,
  sendMessage,
  streamResponseEvents,
  type ChatMessage,
  type Citation,
} from '@/modules/chat/api';
import { ChatComposer } from './chat-composer';
import { ChatTranscript } from './chat-transcript';
import { ConversationAgentActivity } from '@/modules/agents/conversation-agent-activity';
import { useChatController, type PendingSpecialistRun } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { cancelAgentRun, getAgentProfiles, getAgentRun, listAgentRuns, startAgentRun } from '@/modules/agents/api';
import { AgentRunDetail } from '@/modules/agents/run-detail';

const profileTitleKeys: Record<string, 'agentProfileSupervisor' | 'agentProfileKnowledge' | 'agentProfileResearch' | 'agentProfilePersonal' | 'agentProfileProject' | 'agentProfileNews' | 'agentProfilePlanning' | 'agentProfileAutomation'> = {
  supervisor: 'agentProfileSupervisor', knowledge: 'agentProfileKnowledge', research: 'agentProfileResearch',
  personal: 'agentProfilePersonal', project: 'agentProfileProject', news: 'agentProfileNews',
  planning: 'agentProfilePlanning', automation: 'agentProfileAutomation',
};
const runStatusKeys: Record<string, 'agentRunQueued' | 'agentRunRunning' | 'agentRunWaitingApproval' | 'agentRunSucceeded' | 'agentRunFailed' | 'agentRunCancelled'> = {
  queued: 'agentRunQueued', running: 'agentRunRunning', waiting_approval: 'agentRunWaitingApproval',
  succeeded: 'agentRunSucceeded', failed: 'agentRunFailed', cancelled: 'agentRunCancelled',
};

export interface ChatSessionProps {
  /** Target conversation ID to render, or null/undefined for a fresh unsaved thread. */
  conversationId?: string | null;
  /** Callback invoked when a new conversation is created on the server. */
  onConversationCreated?: (id: string) => void;
  /** Mode indicating whether ChatSession is hosted inside the quick drawer or full /chat route. */
  mode?: 'drawer' | 'full';
  /** Optional callback to close the drawer presentation when hosted inside ChatDrawer. */
  onCloseDrawer?: () => void;
}

/**
 * Shared core chat session component powering both the quick-chat Sheet drawer and the /chat route.
 * Coordinates conversation querying, message sending with client_request_id idempotency,
 * active SSE event stream consumption, citation extraction, cancellation controls, and full-page
 * profile run selection/status/history while keeping the quick drawer's legacy assistant flow.
 * Specialist retries retain the exact prompt, profile revision, conversation, and request key in the shared
 * Chat controller until acknowledged, including across sidebar changes, drawer remounts, and page navigation.
 * Late acknowledgements refresh their target conversation, while visible state updates require the
 * mounted send and conversation-selection generation that initiated the request.
 *
 * @param props - ChatSessionProps interface.
 * @returns Unified chat session surface.
 */
export function ChatSession({
  conversationId,
  onConversationCreated,
  mode = 'drawer',
}: ChatSessionProps) {
  const t = useTranslations('chat');
  const tAgents = useTranslations('aiSettings');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const chatCtrl = useChatController();

  const [streamingText, setStreamingText] = React.useState<string>('');
  const [streamingCitations, setStreamingCitations] = React.useState<Citation[]>([]);
  const [isStreaming, setIsStreaming] = React.useState<boolean>(false);
  const [isPending, setIsPending] = React.useState<boolean>(false);
  const [activeResponseId, setActiveResponseId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [selectedProfileId, setSelectedProfileId] = React.useState('assistant');
  const [activeAgentRunId, setActiveAgentRunId] = React.useState<string | null>(null);
  const pendingProfileRun = conversationId
    ? chatCtrl.pendingSpecialistRuns[conversationId] ?? null
    : null;

  const abortControllerRef = React.useRef<AbortController | null>(null);
  const sendGenerationRef = React.useRef(0);
  const mountedRef = React.useRef(false);
  const promotedSelectionRef = React.useRef<{
    conversationId: string;
    generation: number;
    sendGeneration: number;
  } | null>(null);
  const selectionRef = React.useRef({
    conversationId: chatCtrl.activeConversationId,
    generation: chatCtrl.activeConversationGeneration,
  });
  selectionRef.current = {
    conversationId: chatCtrl.activeConversationId,
    generation: chatCtrl.activeConversationGeneration,
  };

  React.useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // Invalidates local continuations when this view is removed; the server run is left alone.
      sendGenerationRef.current += 1;
    };
  }, []);

  // Fetch full conversation details if conversationId is defined
  const { data: conversationDetail, refetch: refetchDetail } = useQuery({
    queryKey: chatKeys.conversation(conversationId ?? ''),
    queryFn: () => getConversation(conversationId!),
    enabled: Boolean(conversationId),
  });

  const messages: ChatMessage[] = React.useMemo(() => {
    return conversationDetail?.messages ?? [];
  }, [conversationDetail?.messages]);

  const profiles = useQuery({ queryKey: ['agent-profiles'], queryFn: getAgentProfiles, enabled: mode === 'full' });
  const agentRuns = useQuery({
    queryKey: ['agent-runs', 'conversation', conversationId],
    queryFn: () => listAgentRuns({ conversation_id: conversationId!, limit: 25 }),
    enabled: mode === 'full' && Boolean(conversationId),
    refetchInterval: (query) => query.state.data?.items.some((run) => ['queued', 'running', 'waiting_approval'].includes(run.status)) ? 3000 : false,
  });
  const activeAgentRun = useQuery({
    queryKey: ['agent-run', activeAgentRunId],
    queryFn: () => getAgentRun(activeAgentRunId!),
    enabled: Boolean(activeAgentRunId),
    refetchInterval: (query) => query.state.data && ['queued', 'running', 'waiting_approval'].includes(query.state.data.status) ? 3000 : false,
  });
  const cancelAgent = useMutation({
    mutationFn: () => cancelAgentRun(activeAgentRunId!, session.csrfToken),
    onSuccess: (run) => {
      queryClient.setQueryData(['agent-run', run.id], run);
      void queryClient.invalidateQueries({ queryKey: ['agent-runs', 'conversation', conversationId] });
    },
  });
  React.useEffect(() => {
    // Detach the previous view's local stream; this does not send a server cancellation request.
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    setActiveAgentRunId(null);
    setIsStreaming(false);
    setStreamingText('');
    setStreamingCitations([]);
    setActiveResponseId(null);
    const promoted = promotedSelectionRef.current;
    if (promoted && promoted.sendGeneration === sendGenerationRef.current &&
        promoted.conversationId === (conversationId ?? null) &&
        promoted.generation === chatCtrl.activeConversationGeneration) {
      // Preserve the pending state while this request promotes its fresh conversation.
      promotedSelectionRef.current = null;
      return;
    }
    promotedSelectionRef.current = null;
    setIsPending(false);
    setError(null);
  }, [conversationId, chatCtrl.activeConversationGeneration]);

  /**
   * Dispatches a message to an existing or newly initialized conversation, retaining an unresolved
   * specialist's immutable retry envelope in shared memory before awaiting the durable start response.
   */
  const handleSend = React.useCallback(
    async (content: string) => {
      const sendGeneration = ++sendGenerationRef.current;
      let viewGeneration = selectionRef.current.generation;
      let viewConversationId = selectionRef.current.conversationId;

      /**
       * Allows local UI changes only while this send still owns the mounted selection generation.
       */
      const canUpdateCurrentView = () => mountedRef.current &&
        sendGenerationRef.current === sendGeneration &&
        selectionRef.current.generation === viewGeneration &&
        selectionRef.current.conversationId === viewConversationId;

      setError(null);
      setIsPending(true);
      const otherPending = Object.values(chatCtrl.pendingSpecialistRuns).find((run) => run.prompt === content) ?? null;
      const unresolved = pendingProfileRun ?? otherPending;
      if (unresolved && (unresolved !== pendingProfileRun || unresolved.prompt !== content || mode !== 'full')) {
        chatCtrl.setDraft(unresolved.prompt);
        setIsPending(false);
        setError(tAgents('agentRetryPending'));
        return;
      }

      const retryEnvelope = pendingProfileRun?.prompt === content ? pendingProfileRun : null;
      let targetId = retryEnvelope?.conversationId ?? conversationId;
      let attemptedProfileRun: PendingSpecialistRun | null = null;

      try {
        const selected = mode === 'full' && selectedProfileId !== 'assistant'
          ? profiles.data?.find((profile) => profile.id === selectedProfileId)
          : undefined;
        if (mode === 'full' && selectedProfileId !== 'assistant' && !retryEnvelope &&
            (!selected || !selected.enabled || selected.capability === 'unavailable')) {
          throw new Error(tAgents('agentUnavailable'));
        }
        // If no active conversation exists, create one first with initial title from message excerpt
        if (!targetId) {
          const titleExcerpt = content.slice(0, 48).trim() || t('newConversation');
          const created = await createConversation(
            {
              title: titleExcerpt,
              context_kind: chatCtrl.context?.kind,
              context_resource_id: chatCtrl.context?.resource_id,
            },
            session.csrfToken,
          );
          targetId = created.id;
          if (canUpdateCurrentView()) {
            const nextGeneration = viewGeneration + 1;
            promotedSelectionRef.current = {
              conversationId: targetId,
              generation: nextGeneration,
              sendGeneration,
            };
            if (onConversationCreated) onConversationCreated(targetId);
            else chatCtrl.setActiveConversationId(targetId);
            viewGeneration = nextGeneration;
            viewConversationId = targetId;
          }
          void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
        }

        if (mode === 'full' && (selectedProfileId !== 'assistant' || retryEnvelope)) {
          const profile = selected;
          if (!retryEnvelope && !profile) throw new Error(tAgents('agentUnavailable'));
          const envelope: PendingSpecialistRun = retryEnvelope ?? {
            prompt: content,
            profileId: profile!.id,
            profileRevision: profile!.revision,
            conversationId: targetId!,
            clientRequestId: typeof crypto !== 'undefined' && crypto.randomUUID
              ? crypto.randomUUID()
              : `req-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`,
          };
          // Keep this immutable envelope in shared memory before awaiting a response that may be lost.
          attemptedProfileRun = envelope;
          chatCtrl.setPendingSpecialistRun(envelope);
          const run = await startAgentRun(envelope.profileId, {
            prompt: envelope.prompt,
            expected_profile_revision: envelope.profileRevision,
            conversation_id: envelope.conversationId,
            client_request_id: envelope.clientRequestId,
          }, session.csrfToken);
          chatCtrl.clearPendingSpecialistRun(envelope.conversationId);
          void queryClient.invalidateQueries({ queryKey: ['agent-runs', 'conversation', targetId] });
          if (canUpdateCurrentView()) {
            chatCtrl.setDraft('');
            setActiveAgentRunId(run.id);
            setIsPending(false);
          }
          return;
        }

        // Optimistically clear draft
        if (canUpdateCurrentView()) chatCtrl.setDraft('');

        // Generate deterministic client_request_id for idempotency
        const clientRequestId = typeof crypto !== 'undefined' && crypto.randomUUID
          ? crypto.randomUUID()
          : `req-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
        const responseConversationId = targetId;

        const responseRun = await sendMessage(
          responseConversationId,
          {
            content,
            client_request_id: clientRequestId,
            context: chatCtrl.context ?? undefined,
          },
          session.csrfToken,
        );

        if (!canUpdateCurrentView()) {
          void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(responseConversationId) });
          void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
          return;
        }

        setActiveResponseId(responseRun.response_id);
        setIsPending(false);
        setIsStreaming(true);
        setStreamingText('');
        setStreamingCitations([]);

        // Start SSE stream
        const abortCtrl = new AbortController();
        abortControllerRef.current = abortCtrl;

        await streamResponseEvents(responseRun.response_id, {
          signal: abortCtrl.signal,
          onDelta: (chunk) => {
            if (canUpdateCurrentView()) setStreamingText((prev) => prev + chunk);
          },
          onCitations: (cites) => {
            if (canUpdateCurrentView()) setStreamingCitations(cites);
          },
          onDone: () => {
            if (canUpdateCurrentView()) {
              setIsStreaming(false);
              setStreamingText('');
              setStreamingCitations([]);
              setActiveResponseId(null);
            }
            void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(responseConversationId) });
            void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
          },
          onStatus: (statusVal) => {
            if (statusVal === 'cancelled') {
              if (canUpdateCurrentView()) {
                setIsStreaming(false);
                setActiveResponseId(null);
                setError(t('cancelled'));
              }
              void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(responseConversationId) });
            } else if (statusVal === 'auth_expired') {
              if (canUpdateCurrentView()) {
                setIsStreaming(false);
                setActiveResponseId(null);
                setError('Session expired');
              }
            }
          },
          onError: (err) => {
            if (!abortCtrl.signal.aborted && canUpdateCurrentView()) {
              setIsStreaming(false);
              setIsPending(false);
              setActiveResponseId(null);
              setError(err.message || t('errorSending'));
            }
          },
        });
      } catch (err: unknown) {
        if (canUpdateCurrentView()) {
          setIsPending(false);
          setIsStreaming(false);
          setActiveResponseId(null);
          const errMsg = attemptedProfileRun?.prompt === content
            ? tAgents('agentStartFailed')
            : err instanceof Error ? err.message : t('errorSending');
          setError(errMsg);
        }
      }
    },
    [
      conversationId,
      chatCtrl,
      onConversationCreated,
      queryClient,
      refetchDetail,
      session.csrfToken,
      t,
      mode,
      selectedProfileId,
      profiles.data,
      tAgents,
    ],
  );

  /**
   * Explicitly cancels active response generation without losing transcript state.
   */
  const handleStop = React.useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }

    if (activeResponseId) {
      try {
        await cancelResponse(activeResponseId, session.csrfToken);
      } catch (err) {
        console.warn('Failed to dispatch cancel endpoint', err);
      }
    }

    setIsStreaming(false);
    setIsPending(false);
    setActiveResponseId(null);
    setError(t('cancelled'));
    void refetchDetail();
  }, [activeResponseId, refetchDetail, session.csrfToken, t]);

  /**
   * Starts a clean new conversation thread.
   */
  const handleNewChat = React.useCallback(() => {
    sendGenerationRef.current += 1;
    chatCtrl.resetConversation();
    setError(null);
    setStreamingText('');
    setStreamingCitations([]);
    setIsStreaming(false);
    setIsPending(false);
  }, [chatCtrl]);

  return (
    <div className="flex flex-col h-full bg-background relative overflow-hidden">
      {/* Session header if inside drawer or full mode */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-surface shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-semibold text-sm truncate text-foreground">
            {conversationDetail?.title || t('title')}
          </span>
          {chatCtrl.context && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] bg-accent/15 text-accent font-medium shrink-0">
              <FileTextIcon className="size-3" />
              <span>{chatCtrl.context.kind}</span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {mode === 'full' && (
            <Select value={pendingProfileRun?.profileId ?? selectedProfileId} onValueChange={setSelectedProfileId}>
              <SelectTrigger aria-label={tAgents('agentSelect')} className="w-40" disabled={Boolean(pendingProfileRun)}><SelectValue placeholder={tAgents('agentSelect')} /></SelectTrigger>
              <SelectContent>
                <SelectItem value="assistant">{tAgents('agentAssistant')}</SelectItem>
                {(profiles.data ?? []).map((profile) => (
                  <SelectItem key={profile.id} value={profile.id} disabled={!profile.enabled || profile.capability === 'unavailable'}>
                    {tAgents(profileTitleKeys[profile.id] ?? 'agentProfileKnowledge')}{profile.capability === 'partial' ? ` · ${tAgents('agentPartial')}` : profile.capability === 'unavailable' ? ` · ${tAgents('agentUnavailable')}` : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button
            type="button"
            onClick={handleNewChat}
            className="secondary inline-flex h-auto items-center gap-1 px-2.5 py-1.5 text-xs font-medium transition-colors"
            title={t('newChat')}
          >
            <PlusIcon className="size-3.5" />
            <span className="hidden sm:inline">{t('newChat')}</span>
          </Button>
        </div>
      </div>

      {pendingProfileRun ? (
        <div role="status" className="shrink-0 border-b border-border px-4 py-2 text-xs text-muted-foreground">
          {tAgents(profileTitleKeys[pendingProfileRun.profileId] ?? 'agentProfileKnowledge')} · {tAgents('agentPendingProfileRevision', { revision: pendingProfileRun.profileRevision })}
        </div>
      ) : null}

      {mode === 'full' && (
        <ConversationAgentActivity conversationId={conversationId} csrfToken={session.csrfToken} />
      )}
      {mode === 'full' && activeAgentRun.data && (
        <div className="shrink-0 border-b border-border p-3">
          <AgentRunDetail run={activeAgentRun.data} onCancel={() => cancelAgent.mutate()} cancelling={cancelAgent.isPending} />
        </div>
      )}
      {mode === 'full' && agentRuns.data?.items.length ? (
        <details className="shrink-0 border-b border-border px-4 py-2">
          <summary className="cursor-pointer text-xs font-medium text-muted-foreground">{tAgents('agentHistory')} ({agentRuns.data.items.length})</summary>
          <div className="mt-2 flex flex-wrap gap-2">
            {agentRuns.data.items.map((run) => <Button key={run.id} type="button" className="secondary h-auto rounded-md px-2 py-1 text-xs font-normal" onClick={() => setActiveAgentRunId(run.id)}>{run.agent_id === 'assistant' ? tAgents('agentAssistant') : tAgents(profileTitleKeys[run.agent_id] ?? 'agentProfileKnowledge')}: {tAgents(runStatusKeys[run.status] ?? 'agentRunFailed')}</Button>)}
          </div>
        </details>
      ) : null}

      {/* Grounded context indicator banner if present */}
      {chatCtrl.context && (
        <div className="flex items-center justify-between gap-2 px-4 py-2 bg-accent/5 border-b border-accent/15 text-xs text-foreground shrink-0">
          <span className="truncate">
            {chatCtrl.context.kind === 'document'
              ? t('contextDocument', { title: String(chatCtrl.context.resource_id ?? '') })
              : chatCtrl.context.kind === 'entity'
                ? t('contextEntity', { name: String(chatCtrl.context.resource_id ?? '') })
                : t('contextSelection', { count: chatCtrl.context.items?.length ?? 1 })}
          </span>
          <button
            type="button"
            onClick={() => chatCtrl.setContext(null)}
            className="p-1 rounded-md text-muted-foreground hover:text-foreground"
            title={t('clearContext')}
            aria-label={t('clearContext')}
          >
            <XIcon className="size-3.5" />
          </button>
        </div>
      )}

      {/* Transcript area */}
      <ChatTranscript
        messages={messages}
        streamingText={streamingText}
        streamingCitations={streamingCitations}
        isStreaming={isStreaming}
        isPending={isPending}
        error={error}
        onRetry={() => {
          const retryDraft = pendingProfileRun?.prompt ?? chatCtrl.draft;
          if (retryDraft) {
            void handleSend(retryDraft);
          }
        }}
      />

      {/* Bottom composer */}
      <ChatComposer
        value={pendingProfileRun?.prompt ?? chatCtrl.draft}
        onChange={pendingProfileRun ? () => undefined : chatCtrl.setDraft}
        onSend={handleSend}
        onStop={handleStop}
        isStreaming={isStreaming}
        disabled={isPending}
      />
    </div>
  );
}
