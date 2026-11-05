import * as React from 'react';

/** A registered live SSE attachment for one response run. */
export interface AttachedResponseStream {
  responseId: string;
  conversationId: string;
  controller: AbortController;
  promise: Promise<void>;
}

/** A fresh conversation created by an in-flight send, preserved across the selection change it triggers. */
export interface PromotedSelection {
  conversationId: string;
  generation: number;
  sendGeneration: number;
}

/** The controller's active selection mirrored for async callbacks. */
export interface SelectionSnapshot {
  conversationId: string | null;
  generation: number;
}

/** Entry point re-attaching a response stream (self reference used for recovery and reattach). */
export type StreamRunFn = (id: string, conversationId: string, isCurrentView: () => boolean) => Promise<void>;

/**
 * Mutable, render-independent runtime state shared by the stream, send and selection hooks of one ChatSession.
 *
 * Plain fields are read directly; every write goes through a method so hooks never assign to their arguments (the
 * React compiler lint rejects that) and each mutation point stays greppable. None of this state triggers renders.
 */
export class ChatSessionRuntime {
  /** Local SSE reader of the view; aborting it does not cancel the durable server run. */
  abortController: AbortController | null = null;
  /** Currently registered response stream attachment, used to join an identical attach instead of duplicating it. */
  attachedStream: AttachedResponseStream | null = null;
  /** Bumped by each send/reattach/unmount; a continuation updates the UI only while its captured value is current. */
  sendGeneration = 0;
  /** Last content handed to handleSend, so Retry can resend it even after the draft was cleared. */
  lastSent: string | null = null;
  /** Response ids already reattached once after a stream ended without a terminal event. */
  readonly reattached = new Set<string>();
  /** Streams started per response id; recovery reattaches only when this stream was the sole attach so far. */
  readonly attachCount = new Map<string, number>();
  mounted = false;
  promotedSelection: PromotedSelection | null = null;
  selection: SelectionSnapshot;
  streamRun: StreamRunFn | null = null;
  /** Render-time mirror of `isPending` for effects that must not re-run when pending flips. */
  isPending = false;

  /** @param initialSelection - Controller selection used to seed the selection snapshot. */
  constructor(initialSelection: SelectionSnapshot) {
    this.selection = initialSelection;
  }

  /** Aborts and forgets the local SSE reader (no server cancellation is sent). */
  detachStream(): void {
    this.abortController?.abort();
    this.abortController = null;
  }

  /** Replaces the local SSE reader. */
  setAbortController(controller: AbortController | null): void {
    this.abortController = controller;
  }

  /** Registers or clears the live attachment. */
  setAttachedStream(attachment: AttachedResponseStream | null): void {
    this.attachedStream = attachment;
  }

  /** Orphans all in-flight continuations and returns the new generation. */
  bumpSendGeneration(): number {
    this.sendGeneration += 1;
    return this.sendGeneration;
  }

  /** Remembers the last sent content for Retry. */
  setLastSent(content: string | null): void {
    this.lastSent = content;
  }

  /** Marks the view mounted/unmounted. */
  setMounted(mounted: boolean): void {
    this.mounted = mounted;
  }

  /** Records (or clears) a selection promoted by an in-flight send. */
  setPromotedSelection(promoted: PromotedSelection | null): void {
    this.promotedSelection = promoted;
  }

  /** Updates the selection snapshot read by async callbacks. */
  setSelection(selection: SelectionSnapshot): void {
    this.selection = selection;
  }

  /** Registers the stream entry point used for recovery. */
  setStreamRun(fn: StreamRunFn | null): void {
    this.streamRun = fn;
  }

  /** Mirrors the render-time pending flag. */
  setPendingMirror(pending: boolean): void {
    this.isPending = pending;
  }
}

/**
 * Creates the session runtime once.
 *
 * @param initialSelection - Controller selection used to seed the selection snapshot.
 * @returns A referentially stable runtime (identity never changes across renders).
 */
export function useChatSessionRefs(initialSelection: SelectionSnapshot): ChatSessionRuntime {
  const [runtime] = React.useState(() => new ChatSessionRuntime(initialSelection));
  return runtime;
}

/** Alias kept for hook signatures. */
export type ChatSessionRefs = ChatSessionRuntime;
