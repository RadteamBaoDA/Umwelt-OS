/** Key prefix of persisted composer drafts; one entry per conversation id, plus `new` for an unsaved thread. */
const PREFIX = 'umwelt.chat.draft.';

/** Draft slot of a conversation (`null` is the unsaved new-chat thread). */
function slot(conversationId: string | null): string {
  return `${PREFIX}${conversationId ?? 'new'}`;
}

/**
 * Reads the draft saved for a conversation in this browser tab. Storage failures (private mode, quota, SSR) read as no draft.
 *
 * @param conversationId - Conversation id, or null for the new-chat thread.
 * @returns The saved text, or an empty string.
 */
export function readDraft(conversationId: string | null): string {
  try {
    return window.sessionStorage.getItem(slot(conversationId)) ?? '';
  } catch {
    return '';
  }
}

/**
 * Saves or clears a conversation's draft. Drafts live in `sessionStorage` only: they survive a reload of the tab but
 * never outlive it, and are removed as soon as the text is empty (including after a successful send).
 *
 * @param conversationId - Conversation id, or null for the new-chat thread.
 * @param value - Draft text; empty removes the entry.
 */
export function writeDraft(conversationId: string | null, value: string): void {
  try {
    if (value) window.sessionStorage.setItem(slot(conversationId), value);
    else window.sessionStorage.removeItem(slot(conversationId));
  } catch {
    // Persistence is best-effort; the in-memory draft still works.
  }
}
