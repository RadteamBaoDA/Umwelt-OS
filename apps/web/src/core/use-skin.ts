'use client';

import { useSyncExternalStore } from 'react';
import { readSkin, type SkinId } from './skin';

/**
 * Subscribes to `data-skin` changes on <html> (the skin picker applies a skin without a reload).
 *
 * @param onChange Called after every `data-skin` mutation.
 * @returns Unsubscribe function that disconnects the observer.
 */
function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-skin'] });
  return () => observer.disconnect();
}

/**
 * Current skin, re-rendering when it changes. The server snapshot is `default`, so the first client render matches
 * SSR and the saved terminal skin (already applied by the pre-paint init script) is picked up right after hydration.
 *
 * @returns The skin applied to <html>.
 */
export function useSkin(): SkinId {
  return useSyncExternalStore(subscribe, readSkin, () => 'default');
}
