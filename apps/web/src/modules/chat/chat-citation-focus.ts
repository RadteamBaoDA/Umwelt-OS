'use client';

import * as React from 'react';
import { citationIdentity } from './citation-panel';
import type { Citation } from './api';

/** Sources the reader asked to see in the evidence rail; `nonce` changes on every request, even for the same keys. */
interface CitationFocus {
  keys: readonly string[];
  nonce: number;
}

const EMPTY: CitationFocus = { keys: [], nonce: 0 };
let current: CitationFocus = EMPTY;
const listeners = new Set<() => void>();

/**
 * Asks the evidence rail (desktop) or sources sheet (mobile) to open on the given citations. The transcript and the
 * rail are siblings in different owners (session vs. page), so this tiny module store is their only coupling.
 *
 * @param citations - Citations to highlight; the first one is scrolled into view. Empty input is ignored.
 */
export function focusCitations(citations: readonly Citation[]): void {
  if (citations.length === 0) return;
  current = { keys: citations.map(citationIdentity), nonce: current.nonce + 1 };
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
  return () => { listeners.delete(callback); };
}

/** Subscribes React to the latest focus request (for highlighting and scroll targets). */
export function useCitationFocus(): CitationFocus {
  return React.useSyncExternalStore(onCitationFocus, () => current, () => EMPTY);
}
