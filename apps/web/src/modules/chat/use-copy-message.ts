'use client';

import * as React from 'react';
import { useResetOnChange } from '@/modules/shared';

// Copied-state feedback adapts AnythingLLM's frontend/src/hooks/useCopyText.js;
// this port writes plain text only and has no model-thought or rich-HTML contract.

/**
 * Provides per-message plain-text clipboard feedback without persisting transcript content.
 * Clipboard permission failures are reported to the caller; pending timers are cleared on
 * the next message action and on unmount so stale feedback cannot affect another conversation.
 */
export function useCopyMessage(conversationId: string | null | undefined) {
  const [copiedMessageId, setCopiedMessageId] = React.useState<string | null>(null);
  const [failedMessageId, setFailedMessageId] = React.useState<string | null>(null);
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const generationRef = React.useRef(0);

  /** Copies visible message text as plain text and reports browser clipboard denial. */
  const copyMessage = React.useCallback(async (messageId: string, content: string) => {
    const generation = ++generationRef.current;
    if (timerRef.current) clearTimeout(timerRef.current);
    setCopiedMessageId(null);
    setFailedMessageId(null);
    try {
      await navigator.clipboard.writeText(content);
      if (generation !== generationRef.current) return;
      setCopiedMessageId(messageId);
      timerRef.current = setTimeout(() => {
        setCopiedMessageId(null);
        timerRef.current = null;
      }, 2000);
    } catch {
      if (generation !== generationRef.current) return;
      setFailedMessageId(messageId);
    }
  }, []);

  // Clear copy feedback when the conversation changes so it cannot leak across threads.
  useResetOnChange([conversationId], () => {
    setCopiedMessageId(null);
    setFailedMessageId(null);
  });
  // The effect only invalidates in-flight copies and timers on conversation change/unmount (no state writes).
  React.useEffect(() => {
    return () => {
      generationRef.current += 1;
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [conversationId]);

  // Stable identity while nothing changed, so memoized transcript messages are not re-rendered by unrelated updates.
  return React.useMemo(() => ({ copyMessage, copiedMessageId, failedMessageId }), [copyMessage, copiedMessageId, failedMessageId]);
}
