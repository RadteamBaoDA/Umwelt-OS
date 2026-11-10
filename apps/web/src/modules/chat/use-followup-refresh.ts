import * as React from 'react';
import type { ChatMessage } from './api';

/** Delays (ms after the answer settled) of the two best-effort refreshes that pick up the auto title and suggestions. */
export const FOLLOWUP_REFRESH_DELAYS_MS = [4000, 10000] as const;
/** An answer older than this is never polled for suggestions (reloading an old conversation must not refetch). */
const FRESH_ANSWER_MS = 2 * 60 * 1000;

/** Inputs of {@link useFollowupRefresh}. */
export interface FollowupRefreshParams {
  conversationId?: string | null;
  messages: ChatMessage[];
  /** True while a send is pending or streaming; the refresh only arms once the run has settled. */
  busy: boolean;
  /** Refetches the conversation detail and the history list; must be referentially stable (it is not an effect dependency). */
  refresh: () => void;
}

/**
 * The title and follow-up suggestions are written by a model call that runs after `message.done`, and the SSE
 * stream normally closes before they exist, so persisted values are authoritative. This hook refetches once after ~4s
 * and again after ~10s while the latest, freshly created assistant answer still has no suggestions.
 *
 * Timers are tied to the effect: switching conversation, a new message, suggestions arriving, a new send or unmounting
 * cancels them.
 *
 * @param params - See {@link FollowupRefreshParams}.
 */
export function useFollowupRefresh({ conversationId, messages, busy, refresh }: FollowupRefreshParams): void {
  const latest = messages[messages.length - 1];
  const waiting = Boolean(conversationId && !busy && latest?.role === 'assistant' && !latest.suggestions?.length);
  const latestId = latest?.id;
  const createdAt = latest?.created_at;
  React.useEffect(() => {
    // Freshness is read inside the effect (not during render) so an old answer never arms the timers.
    if (!waiting || !createdAt || Date.now() - Date.parse(createdAt) >= FRESH_ANSWER_MS) return undefined;
    const timers = FOLLOWUP_REFRESH_DELAYS_MS.map((delay) => window.setTimeout(refresh, delay));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
    // `refresh` is stable by contract; depending on it would restart the delays on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waiting, conversationId, latestId, createdAt]);
}
