'use client';

import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { PlusIcon } from 'lucide-react';
import {
  cancelResponse,
  chatKeys,
  createConversation,
  getConversation,
  hashMessageContent,
  mutateMessage,
  sendMessage,
  streamResponseEvents,
  type ChatMessage,
  type Citation,
} from '@/modules/chat/api';
import { ChatComposer } from './chat-composer';
import { ChatTranscript } from './chat-transcript';
import { ApiError } from '@/core/api';
import { ChatContextBar } from './chat-context-bar';
import { ConversationAgentActivity } from '@/modules/agents/conversation-agent-activity';
import {
  PendingMessageMutationConflictError,
  useChatController,
  type PendingSpecialistRun,
} from '@/core/app-shell/chat-controller';
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
 * Full Chat appends prompt edits and answer regenerations with stable shared request identities;
 * the quick drawer does not expose these advanced message actions.
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
  const [editingMessageId, setEditingMessageId] = React.useState<string | null>(null);
  const [selectedProfileId, setSelectedProfileId] = React.useState('assistant');
  const [activeAgentRunId, setActiveAgentRunId] = React.useState<string | null>(null);
  const pendingProfileRun = conversationId
    ? chatCtrl.pendingSpecialistRuns[conversationId] ?? null
    : null;
  const pendingMessageMutation = conversationId
    ? chatCtrl.pendingMessageMutations[conversationId] ?? null
    : null;
  const effectiveEditingMessageId = editingMessageId
    ?? (pendingMessageMutation?.action === 'edit' ? pendingMessageMutation.targetMessageId : null);

  const abortControllerRef = React.useRef<AbortController | null>(null);
  const attachedResponseStreamRef = React.useRef<{
    responseId: string;
    conversationId: string;
    controller: AbortController;
    promise: Promise<void>;
  } | null>(null);
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
  // Latest-value ref consumed only by async callbacks; mirrors controller selection without re-render.
  // eslint-disable-next-line react-hooks/refs
  selectionRef.current = {
    conversationId: chatCtrl.activeConversationId,
    generation: chatCtrl.activeConversationGeneration,
  };

  React.useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // Detach this browser reader on unmount; the durable worker run continues on the server.
      abortControllerRef.current?.abort();
      abortControllerRef.current = null;
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

  const latestAssistant = React.useMemo(() => [...messages].reverse().find((message) => message.role === 'assistant'), [messages]);

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
  // Selection change must reset local stream state synchronously with the new conversation.
  /* eslint-disable react-hooks/set-state-in-effect */
  React.useEffect(() => {
    // Detach the previous view's local stream; this does not send a server cancellation request.
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    setActiveAgentRunId(null);
    setIsStreaming(false);
    setStreamingText('');
    setStreamingCitations([]);
    setActiveResponseId(null);
    setEditingMessageId(null);
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
  /* eslint-enable react-hooks/set-state-in-effect */

  /**
   * Streams a durable response into the currently selected view and refreshes its transcript on exit.
   *
   * @param responseId - Persisted response run to consume.
   * @param responseConversationId - Conversation whose history is refreshed after the stream.
   * @param isCurrentView - Guard that prevents late events updating another selected conversation.
   * @remarks A fresh attachment starts from the first retained SSE event; it never resends a prompt.
   */
  const streamResponseRun = React.useCallback(async (
    responseId: string,
    responseConversationId: string,
    isCurrentView: () => boolean,
  ) => {
    const attached = attachedResponseStreamRef.current;
    if (
      attached?.responseId === responseId
      && attached.conversationId === responseConversationId
      && !attached.controller.signal.aborted
    ) {
      await attached.promise;
      return;
    }
    attached?.controller.abort();
    const abortCtrl = new AbortController();
    abortControllerRef.current = abortCtrl;
    const streamPromise = streamResponseEvents(responseId, {
      signal: abortCtrl.signal,
      onDelta: (chunk) => {
        if (isCurrentView()) setStreamingText((prev) => prev + chunk);
      },
      onCitations: (cites) => {
        if (isCurrentView()) setStreamingCitations(cites);
      },
      onDone: () => {
        if (isCurrentView()) {
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
          if (isCurrentView()) {
            setIsStreaming(false);
            setIsPending(false);
            setStreamingText('');
            setStreamingCitations([]);
            setActiveResponseId(null);
            setError(t('cancelled'));
          }
          void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(responseConversationId) });
          void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
        } else if (statusVal === 'auth_expired' && isCurrentView()) {
          setIsStreaming(false);
          setActiveResponseId(null);
          setError(t('sessionExpired'));
        }
      },
      onError: (streamError) => {
        if (!abortCtrl.signal.aborted && isCurrentView()) {
          setIsStreaming(false);
          setIsPending(false);
          setActiveResponseId(null);
          setError(streamError.message || t('errorSending'));
        }
      },
    });
    const streamAttachment = {
      responseId,
      conversationId: responseConversationId,
      controller: abortCtrl,
      promise: streamPromise,
    };
    attachedResponseStreamRef.current = streamAttachment;
    try {
      await streamPromise;
    } catch (streamError: unknown) {
      // fetch() can reject before streamResponseEvents reaches its SSE reader error callback.
      if (!abortCtrl.signal.aborted && isCurrentView()) {
        setIsStreaming(false);
        setIsPending(false);
        setActiveResponseId(null);
        setError(streamError instanceof Error ? streamError.message : t('errorSending'));
      }
      throw streamError;
    } finally {
      if (attachedResponseStreamRef.current === streamAttachment) {
        attachedResponseStreamRef.current = null;
        if (abortControllerRef.current === abortCtrl) abortControllerRef.current = null;
      }
    }
  }, [queryClient, t]);

  /** Reattaches to the owner-reported durable run when this conversation surface mounts or returns. */
  React.useEffect(() => {
    const responseId = conversationDetail?.active_response_id;
    if (!conversationId || !responseId || selectionRef.current.conversationId !== conversationId) return;
    const attached = attachedResponseStreamRef.current;
    if (
      attached?.responseId === responseId
      && attached.conversationId === conversationId
      && !attached.controller.signal.aborted
    ) return;

    const viewGeneration = selectionRef.current.generation;
    const sendGeneration = ++sendGenerationRef.current;
    const isCurrentView = () => mountedRef.current
      && sendGenerationRef.current === sendGeneration
      && selectionRef.current.generation === viewGeneration
      && selectionRef.current.conversationId === conversationId;
    setActiveResponseId(responseId);
    setIsPending(false);
    setIsStreaming(true);
    setStreamingText('');
    setStreamingCitations([]);
    setError(null);
    void streamResponseRun(responseId, conversationId, isCurrentView).catch(() => {
      // streamResponseRun reports visible failures through its guarded error path.
    });
  }, [conversationId, conversationDetail?.active_response_id, chatCtrl.activeConversationGeneration, streamResponseRun]);

  /**
   * Submits an edit or regeneration using a stable shared request envelope until acknowledged.
   *
   * @param action - Whether to append a changed prompt or reuse the original prompt.
   * @param targetMessageId - User prompt to edit or assistant response to regenerate.
   * @param content - Replacement prompt text for an edit; omitted for regeneration.
   */
  const handleMessageMutation = React.useCallback(async (
    action: 'edit' | 'regenerate',
    targetMessageId: string,
    content?: string,
  ) => {
    const targetConversationId = conversationId ?? selectionRef.current.conversationId;
    const target = messages.find((message) => message.id === targetMessageId);
    if (!targetConversationId || !target || (isPending || isStreaming)) return;
    const sendGeneration = ++sendGenerationRef.current;
    const viewGeneration = selectionRef.current.generation;
    const viewConversationId = selectionRef.current.conversationId;
    const isCurrentView = () => mountedRef.current
      && sendGenerationRef.current === sendGeneration
      && selectionRef.current.generation === viewGeneration
      && selectionRef.current.conversationId === viewConversationId;

    setError(null);
    setIsPending(true);
    try {
      const baseContentHash = await hashMessageContent(target.content);
      const envelope = chatCtrl.getOrCreatePendingMessageMutation(targetConversationId, {
        action,
        targetMessageId,
        baseContentHash,
        content,
      });
      const run = await mutateMessage(
        targetConversationId,
        targetMessageId,
        {
          action: envelope.action,
          base_content_hash: envelope.baseContentHash,
          client_request_id: envelope.clientRequestId,
          ...(envelope.content !== undefined ? { content: envelope.content } : {}),
        },
        session.csrfToken,
      );
      chatCtrl.clearPendingMessageMutation(targetConversationId, envelope.clientRequestId);
      if (isCurrentView()) {
        setEditingMessageId(null);
        chatCtrl.setDraft('');
      }
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
      if (!isCurrentView()) {
        void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(targetConversationId) });
        return;
      }
      setActiveResponseId(run.response_id);
      setIsPending(false);
      setIsStreaming(true);
      setStreamingText('');
      setStreamingCitations([]);
      await streamResponseRun(run.response_id, targetConversationId, isCurrentView);
    } catch (mutationError: unknown) {
      if (isCurrentView()) {
        setIsPending(false);
        setError(mutationError instanceof PendingMessageMutationConflictError
          ? t('retryOriginalRevisionFirst')
          : mutationError instanceof Error ? mutationError.message : t('revisionFailed'));
      }
    }
  }, [chatCtrl, conversationId, isPending, isStreaming, messages, queryClient, session.csrfToken, streamResponseRun, t]);

  /** Opens the full Chat composer with a user message's content for an append-only edit. */
  const handleEditMessage = React.useCallback((message: ChatMessage) => {
    if (mode !== 'full' || message.role !== 'user') return;
    setEditingMessageId(message.id);
    chatCtrl.setDraft(message.content);
  }, [chatCtrl, mode]);

  /** Reuses the parent assistant message's captured context for a new answer branch. */
  const handleRegenerateMessage = React.useCallback((message: ChatMessage) => {
    if (mode === 'full' && message.role === 'assistant') {
      void handleMessageMutation('regenerate', message.id);
    }
  }, [handleMessageMutation, mode]);

  /**
   * Dispatches a message to an existing or newly initialized conversation, retaining an unresolved
   * specialist's immutable retry envelope in shared memory before awaiting the durable start response.
   */
  const handleSend = React.useCallback(
    async (content: string) => {
      if (mode === 'full' && effectiveEditingMessageId) {
        await handleMessageMutation('edit', effectiveEditingMessageId, content);
        return;
      }
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
      // Day captured before any await: a day conversation is bound to it no matter where the user navigates.
      const day = chatCtrl.context?.kind === 'day' && chatCtrl.context.date && chatCtrl.context.timezone
        ? { date: chatCtrl.context.date, timezone: chatCtrl.context.timezone }
        : null;

      const selItems = chatCtrl.context?.kind === 'selection' ? chatCtrl.context.items ?? [] : [];
      try {
        // Pre-validate the server's 1..32 distinct-document limit before the draft is cleared.
        if (selItems.length > 32 || new Set(selItems.map((i) => i.documentId)).size !== selItems.length) {
          throw new ApiError(422, 'selection limit');
        }
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
              // A day conversation stores its immutable day so it can be recognized later.
              metadata: day ? { date: day.date, timezone: day.timezone } : undefined,
            },
            session.csrfToken,
          );
          targetId = created.id;
          if (day) chatCtrl.bindDayConversation(day, targetId);
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

        await streamResponseRun(responseRun.response_id, responseConversationId, canUpdateCurrentView);
      } catch (err: unknown) {
        if (canUpdateCurrentView()) {
          setIsPending(false);
          setIsStreaming(false);
          setActiveResponseId(null);
          const selectionFailure = selItems.length > 0 && err instanceof ApiError && (err.status === 409 || err.status === 422);
          // Server detail strings are English-only, so selection failures get localized copy and keep the draft.
          if (selectionFailure) chatCtrl.setDraft(content);
          const errMsg = selectionFailure
            ? t(err.status === 422 ? 'contextSelectionTooLarge' : 'contextSelectionUnavailable')
            : attemptedProfileRun?.prompt === content
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
      effectiveEditingMessageId,
      handleMessageMutation,
      streamResponseRun,
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
    setEditingMessageId(null);
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

      <ConversationAgentActivity conversationId={conversationId} csrfToken={session.csrfToken} />
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

      <ChatContextBar context={chatCtrl.context} onRemove={() => chatCtrl.setContext(null)} onChange={mode === 'full' ? chatCtrl.setContext : undefined} />

      {mode === 'full' && pendingMessageMutation && (
        <div role="status" className="flex items-center justify-between gap-3 border-b border-border bg-secondary px-4 py-2 text-xs">
          <span className="text-muted-foreground">{t('revisionUnacknowledged')}</span>
          <Button
            type="button"
            className="secondary h-8 px-2 text-xs"
            disabled={isPending || isStreaming}
            onClick={() => void handleMessageMutation(
              pendingMessageMutation.action,
              pendingMessageMutation.targetMessageId,
              pendingMessageMutation.content,
            )}
          >
            {t('retryRevision')}
          </Button>
        </div>
      )}

      {mode === 'full' && effectiveEditingMessageId && !pendingMessageMutation && (
        <div className="flex items-center justify-between gap-3 border-b border-border bg-secondary px-4 py-2 text-xs">
          <span className="text-muted-foreground">{t('editingPrompt')}</span>
          <Button
            type="button"
            className="secondary h-8 px-2 text-xs"
            onClick={() => {
              setEditingMessageId(null);
              chatCtrl.setDraft('');
            }}
          >
            {t('cancelEdit')}
          </Button>
        </div>
      )}

      {/* Transcript area */}
      <ChatTranscript
        conversationId={conversationId}
        mode={mode}
        messages={messages}
        streamingText={streamingText}
        streamingCitations={streamingCitations}
        isStreaming={isStreaming}
        isPending={isPending}
        error={error}
        onEditMessage={mode === 'full' ? handleEditMessage : undefined}
        onRegenerateMessage={mode === 'full' ? handleRegenerateMessage : undefined}
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
        modelLabel={latestAssistant?.model_identity ?? null}
        sourcesCount={latestAssistant?.citations?.length ?? 0}
        showCapabilityNote={mode === 'full'}
      />
    </div>
  );
}
