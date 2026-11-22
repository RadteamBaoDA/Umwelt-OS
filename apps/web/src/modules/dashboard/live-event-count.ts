import { useSyncExternalStore } from 'react';

let current: number | null = null;
const listeners = new Set<() => void>();

/**
 * Publishes how many events the mounted Overview live map currently shows (null when it unmounts), so the page
 * header can put the figure in the statusline without a second request. Notifies subscribers only on change.
 */
export function publishLiveEventCount(next: number | null): void {
  if (next === current) return;
  current = next;
  listeners.forEach((listener) => listener());
}

/** Subscribes to the published live-event count; the server snapshot is null (no live map during SSR). */
export function useLiveEventCount(): number | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
    () => null,
  );
}
