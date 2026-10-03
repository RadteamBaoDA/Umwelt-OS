'use client';

import * as React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
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
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';

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
 * active SSE event stream consumption, citation extraction, and cancellation controls.
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
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const chatCtrl = useChatController();

  const [streamingText, setStreamingText] = React.useState<string>('');
  const [streamingCitations, setStreamingCitations] = React.useState<Citation[]>([]);
  const [isStreaming, setIsStreaming] = React.useState<boolean>(false);
  const [isPending, setIsPending] = React.useState<boolean>(false);
  const [activeResponseId, setActiveResponseId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const abortControllerRef = React.useRef<AbortController | null>(null);

  // Fetch full conversation details if conversationId is defined
  const { data: conversationDetail, refetch: refetchDetail } = useQuery({
    queryKey: chatKeys.conversation(conversationId ?? ''),
    queryFn: () => getConversation(conversationId!),
    enabled: Boolean(conversationId),
  });

  const messages: ChatMessage[] = React.useMemo(() => {
    return conversationDetail?.messages ?? [];
  }, [conversationDetail?.messages]);

  /**
   * Dispatches a message to an existing or newly initialized conversation,
   * then launches background SSE token streaming.
   */
  const handleSend = React.useCallback(
    async (content: string) => {
      setError(null);
      setIsPending(true);

      let targetId = conversationId;

      try {
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
          chatCtrl.setActiveConversationId(targetId);
          onConversationCreated?.(targetId);
          void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
        }

        // Optimistically clear draft
        chatCtrl.setDraft('');

        // Generate deterministic client_request_id for idempotency
        const clientRequestId = typeof crypto !== 'undefined' && crypto.randomUUID
          ? crypto.randomUUID()
          : `req-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;

        const responseRun = await sendMessage(
          targetId,
          {
            content,
            client_request_id: clientRequestId,
            context: chatCtrl.context ?? undefined,
          },
          session.csrfToken,
        );

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
            setStreamingText((prev) => prev + chunk);
          },
          onCitations: (cites) => {
            setStreamingCitations(cites);
          },
          onDone: () => {
            setIsStreaming(false);
            setStreamingText('');
            setStreamingCitations([]);
            setActiveResponseId(null);
            void refetchDetail();
            void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
          },
          onStatus: (statusVal) => {
            if (statusVal === 'cancelled') {
              setIsStreaming(false);
              setActiveResponseId(null);
              setError(t('cancelled'));
              void refetchDetail();
            } else if (statusVal === 'auth_expired') {
              setIsStreaming(false);
              setActiveResponseId(null);
              setError('Session expired');
            }
          },
          onError: (err) => {
            if (!abortCtrl.signal.aborted) {
              setIsStreaming(false);
              setIsPending(false);
              setActiveResponseId(null);
              setError(err.message || t('errorSending'));
            }
          },
        });
      } catch (err: unknown) {
        setIsPending(false);
        setIsStreaming(false);
        setActiveResponseId(null);
        const errMsg = err instanceof Error ? err.message : t('errorSending');
        setError(errMsg);
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
          <button
            type="button"
            onClick={handleNewChat}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-border bg-background hover:bg-accent/10 text-xs font-medium text-foreground transition-colors"
            title={t('newChat')}
          >
            <PlusIcon className="size-3.5" />
            <span className="hidden sm:inline">{t('newChat')}</span>
          </button>
        </div>
      </div>

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
          if (chatCtrl.draft) {
            void handleSend(chatCtrl.draft);
          }
        }}
      />

      {/* Bottom composer */}
      <ChatComposer
        value={chatCtrl.draft}
        onChange={chatCtrl.setDraft}
        onSend={handleSend}
        onStop={handleStop}
        isStreaming={isStreaming}
        disabled={isPending}
      />
    </div>
  );
}
