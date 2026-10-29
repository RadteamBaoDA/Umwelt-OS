'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { ChevronRightIcon } from 'lucide-react';

/**
 * Collapsible "Thinking" block shown above an assistant answer.
 * While reasoning streams and no answer text exists yet it is open and labelled "Thinking…" with a subtle pulse
 * (motion-safe only); once answer text starts it auto-collapses and is labelled "Thought process". A user click on the
 * summary pins the open state from then on. Persisted messages mount collapsed.
 * The text is model output: it is rendered as plain text (whitespace preserved), never as markdown or HTML.
 *
 * @param props.text - Accumulated reasoning text; the block renders nothing when empty.
 * @param props.streaming - True while this reasoning belongs to the in-flight response.
 * @param props.answerStarted - True once answer text or widgets have begun streaming.
 * @returns Native details/summary disclosure with `data-testid="chat-reasoning"`.
 */
export function ChatReasoning({ text, streaming = false, answerStarted = false }: { text: string; streaming?: boolean; answerStarted?: boolean }) {
  const t = useTranslations('chat');
  // null = follow the automatic rule; a boolean = the reader's explicit choice.
  const [pinned, setPinned] = React.useState<boolean | null>(null);
  if (!text) return null;
  const live = streaming && !answerStarted;
  const open = pinned ?? live;
  return (
    <details
      open={open}
      data-testid="chat-reasoning"
      data-state={live ? 'thinking' : 'done'}
      className="mb-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
    >
      <summary
        className="flex min-h-8 cursor-pointer list-none items-center gap-1.5 font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden"
        onClick={(event) => {
          // Controlled disclosure: React owns `open`, so the native toggle is suppressed.
          event.preventDefault();
          setPinned(!open);
        }}
      >
        <ChevronRightIcon className={`size-3.5 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
        <span className={live ? 'motion-safe:animate-pulse' : undefined}>{live ? t('reasoningLive') : t('reasoningDone')}</span>
      </summary>
      <div className="mt-1.5 max-h-64 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed">{text}</div>
    </details>
  );
}
