'use client';

import * as React from 'react';

/** Open overlays that own Escape: dialogs, menus and listboxes (Radix renders these with ARIA roles). */
const OVERLAY_SELECTOR = '[role="dialog"],[role="alertdialog"],[role="menu"],[role="listbox"]';

/**
 * Stops an in-flight generation when Escape is pressed while focus is inside the chat surface (or nowhere).
 * Escape is left alone when a dialog/menu/listbox is open, when another handler already consumed it, and
 * during IME composition, so it never hijacks dismissal of other UI.
 *
 * @param rootRef - Chat surface element; focus must be inside it or on the document body.
 * @param options - `enabled` gates the listener (full page only); `isStreaming` and `onStop` drive the action.
 */
export function useStopOnEscape(
  rootRef: React.RefObject<HTMLElement | null>,
  { enabled, isStreaming, onStop }: { enabled: boolean; isStreaming: boolean; onStop: () => void | Promise<void> },
): void {
  React.useEffect(() => {
    if (!enabled || !isStreaming) return;
    /** Window-level keydown: Escape stops generation under the guards described above. */
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
      const active = document.activeElement;
      const inChat = !active || active === document.body || Boolean(rootRef.current?.contains(active));
      if (!inChat || document.querySelector(OVERLAY_SELECTOR)) return;
      event.preventDefault();
      void onStop();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [enabled, isStreaming, onStop, rootRef]);
}
