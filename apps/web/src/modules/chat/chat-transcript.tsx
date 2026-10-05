'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { AlertCircleIcon, BotIcon, CheckIcon, CopyIcon, Loader2Icon, PencilIcon, RefreshCwIcon, UserIcon } from 'lucide-react';
import type { ChatMessage, Citation } from '@/modules/chat/api';
import { Button } from '@/components/ui/button';
import { CitationPanel } from './citation-panel';
import { ChatMarkdown } from './chat-markdown';
import { useChatScroll } from './use-chat-scroll';
import { useCopyMessage } from './use-copy-message';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';

export interface ChatTranscriptProps {
  /** Currently selected conversation, used to reset the reading position between threads. */
  conversationId?: string | null;
  /** Surface mode; advanced mutation actions are available only on the full Chat page. */
  mode?: 'drawer' | 'full';
  /** Ordered list of persistent messages in the conversation. */
  messages: ChatMessage[];
  /** Active response generation streaming text, if currently in flight. */
  streamingText?: string;
  /** Citations retrieved for the active response run, if available. */
  streamingCitations?: Citation[];
  /** Whether response generation is actively streaming tokens. */
  isStreaming?: boolean;
  /** Whether a response run has been dispatched and is awaiting initial tokens. */
  isPending?: boolean;
  /** Error message string if generation or sending encountered a failure. */
  error?: string | null;
  /** Callback to retry sending after an error. */
  onRetry?: () => void;
  /** Starts an append-only edit from a user prompt in full Chat. */
  onEditMessage?: (message: ChatMessage) => void;
  /** Starts an append-only regeneration from an assistant answer in full Chat. */
  onRegenerateMessage?: (message: ChatMessage) => void;
  /** Optional callback when user clicks a citation badge. */
  onSelectCitation?: (citation: Citation) => void;
}

/**
 * Conversation transcript rendering ordered messages, append-only revision relationships,
 * full-page copy/edit/regenerate actions, full-page grounded citations, streaming deltas, and error states.
 *
 * @param props - ChatTranscriptProps interface; mutation actions are rendered only for full Chat.
 * @returns Accessible conversation transcript viewport.
 */
export function ChatTranscript({
  conversationId,
  mode = 'drawer',
  messages,
  streamingText,
  streamingCitations,
  isStreaming = false,
  isPending = false,
  error = null,
  onRetry,
  onEditMessage,
  onRegenerateMessage,
}: ChatTranscriptProps) {
  const t = useTranslations('chat');
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';
  const { containerRef, scrollHandlers } = useChatScroll({
    conversationId,
    messageCount: messages.length,
    lastMessageId: messages[messages.length - 1]?.id,
    lastMessageRole: messages[messages.length - 1]?.role,
    isStreaming: isStreaming || isPending,
  });
  const { copyMessage, copiedMessageId, failedMessageId } = useCopyMessage(conversationId);
  const messageById = React.useMemo(() => new Map(messages.map((message) => [message.id, message])), [messages]);
  const revisedMessageIds = React.useMemo(
    () => new Set(messages.flatMap((message) => message.revision_of_message_id ? [message.revision_of_message_id] : [])),
    [messages],
  );

  return (
    <div
      ref={containerRef}
      {...scrollHandlers}
      className="flex-1 overflow-y-auto p-4 flex flex-col gap-4 focus:outline-none"
      role="log"
      aria-live="polite"
      aria-label={t('title')}
    >
      {messages.length === 0 && !streamingText && !isPending && (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center text-muted-foreground my-auto">
          <div className="size-12 rounded-full bg-accent/10 text-accent flex items-center justify-center mb-3">
            <BotIcon className="size-6" />
          </div>
          <h3 className="text-base font-semibold text-foreground mb-1">
            {t('emptyStateTitle')}
          </h3>
          <p className="text-xs max-w-sm text-muted-foreground leading-normal">
            {t('emptyStateDescription')}
          </p>
        </div>
      )}

      {messages.map((msg) => {
        const isUser = msg.role === 'user';
        const revisedMessage = msg.revision_of_message_id
          ? messageById.get(msg.revision_of_message_id)
          : undefined;
        const relationshipLabel = revisedMessage?.role === 'assistant'
          ? t(isUser ? 'regeneratedPrompt' : 'regeneratedAnswer')
          : revisedMessage?.role === 'user'
            ? t(isUser ? 'editedPrompt' : 'editedPromptAnswer')
            : null;
        const canEdit = mode === 'full' && isUser && Boolean(msg.response_id)
          && !revisedMessageIds.has(msg.id) && Boolean(onEditMessage);
        const canRegenerate = mode === 'full' && !isUser && msg.role === 'assistant'
          && Boolean(msg.response_id) && !revisedMessageIds.has(msg.id) && Boolean(onRegenerateMessage);
        const formattedDate = msg.created_at
          ? formatDateTime(msg.created_at, locale, timezone)
          : null;

        return (
          <div
            key={msg.id}
            className={`flex gap-3 max-w-[90%] sm:max-w-[85%] ${
              isUser ? 'ml-auto flex-row-reverse' : 'mr-auto flex-row'
            }`}
          >
            <div
              className={`size-8 rounded-full flex items-center justify-center shrink-0 text-xs font-semibold ${
                isUser
                  ? 'bg-accent text-white dark:text-zinc-900'
                  : 'bg-surface border border-border text-foreground'
              }`}
            >
              {isUser ? <UserIcon className="size-4" /> : <BotIcon className="size-4" />}
            </div>

            <div
              className={`flex flex-col gap-1 p-3.5 rounded-2xl shadow-xs text-sm ${
                isUser
                  ? 'bg-accent/10 border border-accent/20 text-foreground rounded-tr-xs'
                  : 'bg-surface border border-border text-foreground rounded-tl-xs'
              }`}
            >
              <div className="flex items-center justify-between gap-4 text-[11px] text-muted-foreground mb-0.5">
                <span className="font-semibold">
                  {isUser ? 'You' : msg.model_identity || 'BBD-OS Assistant'}
                </span>
                {formattedDate && <span>{formattedDate}</span>}
              </div>

              {relationshipLabel && (
                <p className="text-[11px] text-muted-foreground" aria-label={relationshipLabel}>
                  {relationshipLabel}
                </p>
              )}

              <ChatMarkdown content={msg.content} />

              <div className="flex items-center gap-2">
                {mode === 'full' && <button
                  type="button"
                  onClick={() => void copyMessage(msg.id, msg.content)}
                  className="inline-flex min-h-8 items-center gap-1 rounded-md px-2 text-[11px] text-muted-foreground hover:bg-accent/10 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={t('copyMessage')}
                >
                  {copiedMessageId === msg.id ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
                  <span>{copiedMessageId === msg.id ? t('copiedMessage') : t('copyMessage')}</span>
                </button>}
                {mode === 'full' && failedMessageId === msg.id && (
                  <span role="status" className="text-[11px] text-destructive">{t('copyFailed')}</span>
                )}
                {canEdit && (
                  <Button
                    type="button"
                    className="secondary inline-flex min-h-8 items-center gap-1 px-2 text-[11px]"
                    onClick={() => onEditMessage?.(msg)}
                    aria-label={t('editPrompt')}
                  >
                    <PencilIcon className="size-3.5" />
                    <span>{t('editPrompt')}</span>
                  </Button>
                )}
                {canRegenerate && (
                  <Button
                    type="button"
                    className="secondary inline-flex min-h-8 items-center gap-1 px-2 text-[11px]"
                    onClick={() => onRegenerateMessage?.(msg)}
                    aria-label={t('regenerateAnswer')}
                  >
                    <RefreshCwIcon className="size-3.5" />
                    <span>{t('regenerateAnswer')}</span>
                  </Button>
                )}
              </div>

              {mode === 'full' && !isUser && msg.citations && msg.citations.length > 0 && (
                <CitationPanel citations={msg.citations} variant="inline" />
              )}
            </div>
          </div>
        );
      })}

      {/* Pending status indicator */}
      {isPending && !streamingText && (
        <div className="flex gap-3 max-w-[85%] mr-auto items-center text-xs text-muted-foreground p-3 rounded-xl bg-surface/50 border border-border">
          <Loader2Icon className="size-4 animate-spin text-accent" />
          <span>{t('thinking')}</span>
        </div>
      )}

      {/* Active streaming assistant bubble */}
      {(streamingText || isStreaming) && (
        <div className="flex gap-3 max-w-[90%] sm:max-w-[85%] mr-auto flex-row">
          <div className="size-8 rounded-full flex items-center justify-center shrink-0 bg-surface border border-border text-foreground text-xs font-semibold">
            <BotIcon className="size-4 text-accent" />
          </div>

          <div className="flex flex-col gap-1 p-3.5 rounded-2xl shadow-xs bg-surface border border-border text-foreground rounded-tl-xs">
            <div className="flex items-center gap-2 text-[11px] text-muted-foreground mb-0.5">
              <span className="font-semibold">BBD-OS Assistant</span>
              <span className="inline-flex items-center gap-1 text-[10px] text-accent">
                <span className="size-1.5 rounded-full bg-accent animate-pulse" />
                {t('streaming')}
              </span>
            </div>

            <ChatMarkdown content={streamingText || ''} />

            {mode === 'full' && streamingCitations && streamingCitations.length > 0 && (
              <CitationPanel citations={streamingCitations} variant="inline" />
            )}
          </div>
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div className="flex items-center justify-between gap-2 p-3 rounded-lg border border-destructive/30 bg-destructive/10 text-destructive text-xs">
          <div className="flex items-center gap-2">
            <AlertCircleIcon className="size-4 shrink-0" />
            <span>{error}</span>
          </div>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="px-2 py-1 rounded bg-destructive text-white hover:opacity-90 font-medium text-[11px]"
            >
              {t('retry')}
            </button>
          )}
        </div>
      )}

    </div>
  );
}
