'use client';

import * as React from 'react';
import { citationIdentity } from './citation-panel';
import type { Citation } from './api';

/**
 * Sources the reader asked to see in the evidence rail. `scope` is the opening answer's own citation list (numbered the
 * way its inline chips are) or null for "all sources"; `keys` are the cards to emphasize. `nonce` changes on every
 * request, even for the same keys. `conversationId` is the conversation the request came from: the store is module-global,
 * so a consumer showing another conversation must treat the request as empty.
 */
interface CitationFocus {
  conversationId: string | null;
  scope: readonly Citation[] | null;
  keys: readonly string[];
  nonce: number;
}

const EMPTY: CitationFocus = { conversationId: null, scope: null, keys: [], nonce: 0 };
let current: CitationFocus = EMPTY;
const listeners = new Set<() => void>();

/**
 * Asks the evidence rail (desktop) or sources sheet (mobile) to open on one answer's sources. The transcript and the
 * rail are siblings in different owners (session vs. page), so this tiny module store is their only coupling.
 *
 * @param citations - The answer's citations, in the order its inline chips number them. Empty input is ignored.
 * @param highlighted - The clicked citation to emphasize and scroll into view; omit when opened from the sources button.
 * @param conversationId - Conversation the answer belongs to; rails of any other conversation ignore the request.
 */
export function focusCitations(citations: readonly Citation[], highlighted?: Citation, conversationId?: string | null): void {
  if (citations.length === 0) return;
  current = {
    conversationId: conversationId ?? null,
    scope: citations,
    keys: highlighted ? [citationIdentity(highlighted)] : [],
    nonce: current.nonce + 1,
  };
  listeners.forEach((listener) => listener());
}

/** Drops the per-answer scope so the rail lists every source of the conversation again ("View all"). */
export function showAllCitations(): void {
  current = { conversationId: null, scope: null, keys: [], nonce: current.nonce + 1 };
  listeners.forEach((listener) => listener());
}

/**
 * Subscribes to focus requests without re-rendering; the callback may call the subscriber's own state setters.
 *
 * @param callback - Invoked once per {@link focusCitations} call.
 * @returns Unsubscribe function.
 */
export function onCitationFocus(callback: () => void): () => void {
  listeners.add(callback);
  return () => {
    listeners.delete(callback);
  };
}

/**
 * Subscribes React to the latest focus request (for highlighting and scroll targets).
 *
 * @param conversationId - Conversation the caller shows; a request made for a different conversation reads as empty
 * (no scope, no highlighted keys) so another conversation's emphasis never leaks into this one.
 */
export function useCitationFocus(conversationId?: string | null): CitationFocus {
  const focus = React.useSyncExternalStore(
    onCitationFocus,
    () => current,
    () => EMPTY,
  );
  return focus.scope !== null && focus.conversationId !== (conversationId ?? null) ? EMPTY : focus;
}
