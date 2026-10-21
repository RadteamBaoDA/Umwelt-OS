import { uuid7 } from '@/core/uuid7';

/** Same-origin signal carrying no identity or bearer data. */
const ERASURE_SIGNAL = 'umwelt:account-erasure';
const ERASURE_EVENT = 'bbd:account-erased';

/** Remove app-owned preferences and private drafts; leave unrelated same-origin storage alone. */
export function clearAccountBrowserStorage(): void {
  for (const storageName of ['localStorage', 'sessionStorage'] as const) {
    try {
      const storage = window[storageName];
      for (const key of Object.keys(storage)) {
        if (key === 'theme' || key.startsWith('umwelt:') || key.startsWith('bbd:')
          || key.startsWith('umwelt.genui.templates.')) storage.removeItem(key);
      }
    } catch { /* Blocked browser storage contains no accessible app state to clear. */ }
  }
}

/** Notify this tab and other same-origin tabs after acceptance, without persisting a receipt. */
export function announceAccountErasure(): void {
  try {
    if (typeof BroadcastChannel !== 'undefined') {
      const channel = new BroadcastChannel(ERASURE_SIGNAL);
      channel.postMessage(ERASURE_SIGNAL);
      channel.close();
    }
  } catch { /* Storage events provide the other-tab fallback. */ }
  try {
    localStorage.setItem(ERASURE_SIGNAL, uuid7());
    localStorage.removeItem(ERASURE_SIGNAL);
  } catch { /* The current tab must still erase when storage is blocked. */ }
  window.dispatchEvent(new Event(ERASURE_EVENT));
}

/** Subscribe at the application provider boundary; return cleanup for every listener. */
export function subscribeToAccountErasure(onErasure: () => void): () => void {
  let channel: BroadcastChannel | undefined;
  /** Only the dedicated transient storage signal invalidates another tab's account state. */
  const storageChanged = (event: StorageEvent) => {
    if (event.key === ERASURE_SIGNAL && event.newValue !== null) onErasure();
  };
  try {
    if (typeof BroadcastChannel !== 'undefined') {
      channel = new BroadcastChannel(ERASURE_SIGNAL);
      channel.onmessage = (event: MessageEvent<unknown>) => { if (event.data === ERASURE_SIGNAL) onErasure(); };
    }
  } catch { /* Storage events still work where BroadcastChannel is unavailable. */ }
  window.addEventListener(ERASURE_EVENT, onErasure);
  window.addEventListener('storage', storageChanged);
  return () => {
    channel?.close();
    window.removeEventListener(ERASURE_EVENT, onErasure);
    window.removeEventListener('storage', storageChanged);
  };
}
