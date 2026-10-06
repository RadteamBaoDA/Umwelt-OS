'use client';

import * as React from 'react';

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
  /* eslint-disable react-hooks/set-state-in-effect */
  React.useEffect(() => {
    setCopiedMessageId(null);
    setFailedMessageId(null);
    return () => {
      generationRef.current += 1;
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [conversationId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  return { copyMessage, copiedMessageId, failedMessageId };
}
