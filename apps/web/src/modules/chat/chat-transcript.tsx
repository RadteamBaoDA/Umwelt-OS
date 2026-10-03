'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { AlertCircleIcon, BotIcon, Loader2Icon, UserIcon } from 'lucide-react';
import type { ChatMessage, Citation } from '@/modules/chat/api';
import { CitationPanel } from './citation-panel';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';

export interface ChatTranscriptProps {
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
  /** Optional callback when user clicks a citation badge. */
  onSelectCitation?: (citation: Citation) => void;
}

/**
 * Safely parses and renders a string containing basic markdown syntax
 * including headings, code blocks, lists, blockquotes, and inline emphasis.
 *
 * @param content - Raw text containing markdown formatting.
 * @returns React elements representing the formatted content.
 */
function FormattedMessageContent({ content }: { content: string }) {
  const parts = React.useMemo(() => {
    // Split by code blocks: ```lang ... ```
    const codeBlockRegex = /```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g;
    const segments: Array<{ type: 'code' | 'text'; lang?: string; text: string }> = [];
    let lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = codeBlockRegex.exec(content)) !== null) {
      if (match.index > lastIndex) {
        segments.push({
          type: 'text',
          text: content.slice(lastIndex, match.index),
        });
      }
      segments.push({
        type: 'code',
        lang: match[1] || 'text',
        text: match[2],
      });
      lastIndex = match.index + match[0].length;
    }

    if (lastIndex < content.length) {
      segments.push({
        type: 'text',
        text: content.slice(lastIndex),
      });
    }

    return segments;
  }, [content]);

  return (
    <div className="flex flex-col gap-2.5 text-sm leading-relaxed text-foreground break-words overflow-hidden">
      {parts.map((segment, segIdx) => {
        if (segment.type === 'code') {
          return (
            <div
              key={segIdx}
              className="relative my-2 rounded-lg border border-border bg-zinc-900 dark:bg-black p-3 text-xs text-zinc-100 overflow-x-auto font-mono"
            >
              {segment.lang && segment.lang !== 'text' && (
                <span className="absolute top-2 right-2 text-[10px] text-zinc-400 uppercase select-none">
                  {segment.lang}
                </span>
              )}
              <pre>
                <code>{segment.text}</code>
              </pre>
            </div>
          );
        }

        // Render text segment paragraphs and bullet lists
        const lines = segment.text.split('\n');
        return (
          <div key={segIdx} className="flex flex-col gap-1.5">
            {lines.map((line, lineIdx) => {
              const trimmed = line.trim();
              if (!trimmed) {
                return <div key={lineIdx} className="h-1" />;
              }

              // Heading check
              if (trimmed.startsWith('### ')) {
                return (
                  <h4 key={lineIdx} className="font-bold text-sm mt-2 text-foreground">
                    {trimmed.slice(4)}
                  </h4>
                );
              }
              if (trimmed.startsWith('## ')) {
                return (
                  <h3 key={lineIdx} className="font-bold text-base mt-2 text-foreground">
                    {trimmed.slice(3)}
                  </h3>
                );
              }
              if (trimmed.startsWith('# ')) {
                return (
                  <h2 key={lineIdx} className="font-bold text-lg mt-3 text-foreground">
                    {trimmed.slice(2)}
                  </h2>
                );
              }

              // Bullet item
              if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
                return (
                  <div key={lineIdx} className="flex items-start gap-2 pl-2">
                    <span className="text-accent shrink-0">•</span>
                    <span>{trimmed.slice(2)}</span>
                  </div>
                );
              }

              // Blockquote
              if (trimmed.startsWith('> ')) {
                return (
                  <blockquote
                    key={lineIdx}
                    className="pl-3 border-l-2 border-accent text-muted-foreground italic my-1"
                  >
                    {trimmed.slice(2)}
                  </blockquote>
                );
              }

              return <p key={lineIdx}>{trimmed}</p>;
            })}
          </div>
        );
      })}
    </div>
  );
}

/**
 * Conversation transcript rendering chronologically ordered user/assistant message bubbles,
 * grounded citation references, streaming delta tokens with animated indicator, and error states.
 *
 * @param props - ChatTranscriptProps interface.
 * @returns Accessible conversation transcript viewport.
 */
export function ChatTranscript({
  messages,
  streamingText,
  streamingCitations,
  isStreaming = false,
  isPending = false,
  error = null,
  onRetry,
}: ChatTranscriptProps) {
  const t = useTranslations('chat');
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';
  const bottomRef = React.useRef<HTMLDivElement>(null);
  const containerRef = React.useRef<HTMLDivElement>(null);

  /**
   * Automatically scrolls to the newest message or streaming delta unless the user scrolled up.
   */
  React.useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [messages.length, streamingText, isStreaming, isPending]);

  return (
    <div
      ref={containerRef}
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

              <FormattedMessageContent content={msg.content} />

              {!isUser && msg.citations && msg.citations.length > 0 && (
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

            <FormattedMessageContent content={streamingText || ''} />

            {streamingCitations && streamingCitations.length > 0 && (
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

      <div ref={bottomRef} />
    </div>
  );
}
