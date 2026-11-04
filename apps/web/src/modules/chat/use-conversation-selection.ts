import * as React from 'react';
import type { ChatLiveActions } from './chat-live-state';
import type { ChatSessionRefs } from './chat-session-refs';
import type { IsCurrentView } from './use-chat-stream';

/** Inputs of {@link useConversationSelection}. */
export interface ConversationSelectionParams {
  refs: ChatSessionRefs;
  live: ChatLiveActions;
  isPending: boolean;
  /** Conversation rendered by this surface (null/undefined for a fresh unsaved thread). */
  conversationId?: string | null;
  /** Controller's selected conversation id and its generation counter. */
  activeConversationId: string | null;
  activeConversationGeneration: number;
  /** Owner-reported durable run to reattach to, if any. */
  activeResponseId?: string | null;
  streamResponseRun: (responseId: string, conversationId: string, isCurrentView: IsCurrentView) => Promise<void>;
  /** Clears view-local state that belongs to the previous selection (agent run, edited message). */
  onSelectionReset: () => void;
}

/**
 * Tracks the mounted conversation selection and keeps local stream state consistent with it.
 *
 * Responsibilities, in effect order: mount/unmount bookkeeping (detach the browser reader, orphan continuations),
 * the selection-change reset (abort the previous stream, clear live output, honour a selection promoted by an
 * in-flight send), and reattaching to an owner-reported durable run when the surface mounts or returns.
 *
 * @param params - See {@link ConversationSelectionParams}.
 */
export function useConversationSelection(params: ConversationSelectionParams): void {
  const { refs, live, isPending, conversationId, activeConversationId, activeConversationGeneration, activeResponseId, streamResponseRun, onSelectionReset } = params;

  // Latest-value refs consumed only by async callbacks; they mirror state without re-rendering.
  refs.setSelection({ conversationId: activeConversationId, generation: activeConversationGeneration });
  refs.setPendingMirror(isPending);

  React.useEffect(() => {
    refs.setMounted(true);
    return () => {
      refs.setMounted(false);
      // Detach this browser reader on unmount; the durable worker run continues on the server.
      refs.detachStream();
      // Invalidates local continuations when this view is removed; the server run is left alone.
      refs.bumpSendGeneration();
    };
  }, [refs]);

  // Selection change must reset local stream state synchronously with the new conversation.
  React.useEffect(() => {
    // Detach the previous view's local stream; this does not send a server cancellation request.
    refs.detachStream();
    onSelectionReset();
    live.clearOutput();
    live.patch({ isStreaming: false, activeResponseId: null });
    const promoted = refs.promotedSelection;
    if (promoted && promoted.sendGeneration === refs.sendGeneration
        && promoted.conversationId === (conversationId ?? null)
        && promoted.generation === activeConversationGeneration) {
      // Preserve the pending state while this request promotes its fresh conversation.
      refs.setPromotedSelection(null);
      return;
    }
    refs.setPromotedSelection(null);
    live.patch({ isPending: false, error: null, cancelled: false });
  }, [conversationId, activeConversationGeneration, refs, live, onSelectionReset]);

  /** Reattaches to the owner-reported durable run when this conversation surface mounts or returns. */
  React.useEffect(() => {
    const responseId = activeResponseId;
    if (!conversationId || !responseId || refs.selection.conversationId !== conversationId) return;
    // A local send owns this view until its stream registers: a detail fetch that races the send (new
    // conversation) must not bump sendGeneration, which would orphan the send and reset its live state.
    if (refs.isPending) return;
    const attached = refs.attachedStream;
    if (attached?.responseId === responseId && attached.conversationId === conversationId && !attached.controller.signal.aborted) return;

    const viewGeneration = refs.selection.generation;
    const sendGeneration = refs.bumpSendGeneration();
    const isCurrentView = () => refs.mounted
      && refs.sendGeneration === sendGeneration
      && refs.selection.generation === viewGeneration
      && refs.selection.conversationId === conversationId;
    live.clearOutput();
    live.patch({ activeResponseId: responseId, isPending: false, isStreaming: true, error: null, cancelled: false });
    void streamResponseRun(responseId, conversationId, isCurrentView).catch(() => {
      // streamResponseRun reports visible failures through its guarded error path.
    });
  }, [conversationId, activeResponseId, activeConversationGeneration, streamResponseRun, refs, live]);
}
