import * as React from 'react';
import { apiRequest } from '@/core/api';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { chatKeys, getConversation, streamResponseEvents, type ConversationDetail, type MessageSuggestions, type StreamEventsOptions } from './api';
import type { ChatLiveActions } from './chat-live-state';
import { chatFailureKey } from './chat-errors';
import type { ChatSessionRefs } from './chat-session-refs';

/** Max automatic stream resumes after a transient network/5xx failure, each resuming from the last event id. */
export const MAX_RESUME_ATTEMPTS = 3;
/** Base delay of the exponential resume backoff (500ms, 1s, 2s). */
export const RESUME_BACKOFF_MS = 500;

/** Guard that is true only while the stream's originating view (mounted, send generation, selection) is still current. */
export type IsCurrentView = () => boolean;

/** Mutable bookkeeping shared by the handlers of one stream attachment. */
interface StreamRunState {
  /** The run reached message.done or a terminal status; anything else is a lost stream to recover. */
  terminal: boolean;
  /** The server reported it cannot serve this stream. */
  unavailable: boolean;
  /** Resolves when the resume loop finishes (set once the loop starts). */
  promise: Promise<void> | null;
  /** Registration of this attachment in `attachedStream`. */
  attachment: { responseId: string; conversationId: string; controller: AbortController; promise: Promise<void> } | null;
}

/** Inputs shared by every stream handler builder. */
interface StreamContext {
  responseId: string;
  conversationId: string;
  isCurrentView: IsCurrentView;
  live: ChatLiveActions;
  refs: ChatSessionRefs;
  queryClient: QueryClient;
  t: (key: string, values?: Record<string, string | number>) => string;
  abortCtrl: AbortController;
  run: StreamRunState;
}

/**
 * Builds the handlers that mirror live SSE content (text, reasoning, tools, citations, widgets, input requests)
 * into the live state, each guarded by the view guard so a stale stream never touches another conversation.
 *
 * @param live - Live-state actions.
 * @param isCurrentView - View guard.
 * @param setThemeRequest - Receives a transient theme requested by the assistant.
 * @returns Partial stream options (content handlers only).
 */
export function buildContentHandlers(
  live: ChatLiveActions,
  isCurrentView: IsCurrentView,
  setThemeRequest: (mode: 'light' | 'dark' | 'system') => void,
): StreamEventsOptions {
  const { dispatch } = live;
  return {
    onReasoning: (chunk) => { if (isCurrentView()) dispatch({ type: 'appendReasoning', chunk }); },
    onDelta: (chunk) => { if (isCurrentView()) dispatch({ type: 'appendDelta', chunk }); },
    onToolStatus: ({ tool, state }) => { if (isCurrentView()) dispatch({ type: 'toolStatus', tool, state }); },
    onToolStep: (step) => { if (isCurrentView()) dispatch({ type: 'toolStep', step }); },
    onWebSearch: (outcome) => { if (isCurrentView()) dispatch({ type: 'webSearch', outcome }); },
    onCitations: (cites) => { if (isCurrentView()) live.patch({ streamingCitations: cites }); },
    // One entry per widget part, merged by part_id so parts stream and complete independently.
    onUiDelta: (ui) => { if (isCurrentView()) dispatch({ type: 'uiUpsert', ui }); },
    onUiDone: (ui) => { if (isCurrentView()) dispatch({ type: 'uiUpsert', ui }); },
    onUiDiscard: (partId) => { if (isCurrentView()) dispatch({ type: 'uiRemove', partId }); },
    onThemeRequest: (mode) => { if (isCurrentView()) setThemeRequest(mode); },
    onInputRequest: (request) => { if (isCurrentView()) live.patch({ inputRequest: { request, state: 'pending' } }); },
    onInputResolved: (resolved) => {
      if (!isCurrentView()) return;
      dispatch({ type: 'inputResolved', resolved });
    },
  };
}

/** Invalidates the transcript of the stream's conversation and the conversation list. */
function invalidateConversation(queryClient: QueryClient, conversationId: string): void {
  void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(conversationId) });
  void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
}

/**
 * Writes a streamed auto title into the cached conversation detail and refreshes the history list.
 *
 * @param queryClient - Query client owning the chat caches.
 * @param conversationId - Conversation the stream belongs to (never the currently selected one by assumption).
 * @param title - New title from `conversation.title`.
 */
function applyLiveTitle(queryClient: QueryClient, conversationId: string, title: string): void {
  queryClient.setQueryData<ConversationDetail>(chatKeys.conversation(conversationId), (old) => (old ? { ...old, title } : old));
  void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
}

/**
 * Writes streamed follow-up suggestions onto the matching message of the cached conversation detail.
 *
 * @param queryClient - Query client owning the chat caches.
 * @param conversationId - Conversation the stream belongs to.
 * @param payload - `message.suggestions` payload; ignored when the message is not (yet) in the cache.
 */
function applyLiveSuggestions(queryClient: QueryClient, conversationId: string, payload: MessageSuggestions): void {
  queryClient.setQueryData<ConversationDetail>(chatKeys.conversation(conversationId), (old) => (old
    ? { ...old, messages: old.messages.map((m) => (m.id === payload.message_id ? { ...m, suggestions: payload.suggestions } : m)) }
    : old));
}

/**
 * Handles the stream's terminal `message.done`: clears all live output and refreshes the transcript.
 *
 * @param ctx - Stream context.
 */
function handleDone(ctx: StreamContext): void {
  ctx.run.terminal = true;
  if (ctx.isCurrentView()) {
    ctx.live.clearOutput();
    ctx.live.patch({ isStreaming: false, inputRequest: null, activeResponseId: null });
  }
  invalidateConversation(ctx.queryClient, ctx.conversationId);
}

/** Resets the live state after a run that ended by cancellation or failure (view-guarded by the caller). */
function resetAfterTerminalStatus(ctx: StreamContext): void {
  ctx.live.clearOutput();
  ctx.live.dispatch({ type: 'cancelPendingInput' });
  ctx.live.patch({ isStreaming: false, isPending: false, activeResponseId: null });
}

/**
 * Handles a `failed` terminal status: the server sends the vetted, secret-redacted provider/policy reason, shown so the
 * owner can act (otherwise the stream stayed on "Generating response…").
 */
function handleFailed(ctx: StreamContext, payload: unknown): void {
  if (ctx.isCurrentView()) {
    resetAfterTerminalStatus(ctx);
    const raw = (payload as { error?: unknown } | undefined)?.error;
    const detail = typeof raw === 'string' ? raw.trim() : '';
    ctx.live.patch({ error: detail ? ctx.t('responseFailedDetail', { detail }) : ctx.t('responseFailed') });
  }
  invalidateConversation(ctx.queryClient, ctx.conversationId);
}

/**
 * Handles `auth_expired`: password rotation also ends the stream's old session. A 200 probe means the session was only
 * rotated, so the durable run continues and the view reattaches (the prompt is never resent).
 */
function handleAuthExpired(ctx: StreamContext): void {
  ctx.live.patch({ isStreaming: false, activeResponseId: null, error: ctx.t('sessionExpired') });
  // apiRequest dispatches umwelt:unauthorized only on a real 401.
  void apiRequest('/api/v1/auth/session').then(async () => {
    await ctx.run.promise?.catch(() => undefined);
    if (ctx.refs.attachedStream === ctx.run.attachment) ctx.refs.setAttachedStream(null);
    if (!ctx.isCurrentView() || ctx.refs.attachedStream) return;
    ctx.live.dispatch({ type: 'clearPartial' });
    ctx.live.patch({ error: null, cancelled: false, activeResponseId: ctx.responseId, isStreaming: true });
    await ctx.refs.streamRun?.(ctx.responseId, ctx.conversationId, ctx.isCurrentView);
  }).catch(() => undefined);
}

/**
 * Handles `message.status` events, mapping terminal statuses to live-state resets.
 *
 * @param ctx - Stream context.
 * @param status - Status keyword from the server.
 * @param payload - Raw event payload (may carry a redacted `error`).
 */
function handleStatus(ctx: StreamContext, status: string, payload: unknown): void {
  if (status === 'cancelled' || status === 'failed' || status === 'auth_expired') ctx.run.terminal = true;
  if (status === 'unavailable') {
    // The server cannot serve this stream: stop reading and let the post-stream recovery reset the UI.
    ctx.run.unavailable = true;
    ctx.abortCtrl.abort();
    return;
  }
  if (status === 'cancelled') {
    if (ctx.isCurrentView()) {
      resetAfterTerminalStatus(ctx);
      ctx.live.patch({ cancelled: true });
    }
    invalidateConversation(ctx.queryClient, ctx.conversationId);
  } else if (status === 'failed') {
    handleFailed(ctx, payload);
  } else if (status === 'auth_expired' && ctx.isCurrentView()) {
    handleAuthExpired(ctx);
  }
}

/**
 * Runs the SSE consumer, resuming from the last event id after transient failures (network, 5xx) with exponential
 * backoff. 4xx, terminal runs and aborts are rethrown / end the loop immediately.
 *
 * @param ctx - Stream context.
 * @param streamOptions - The (mutable) options object; `lastEventId` is advanced by the consumer.
 */
async function consumeWithResume(ctx: StreamContext, streamOptions: StreamEventsOptions): Promise<void> {
  const { abortCtrl } = ctx;
  for (let attempt = 0; ; attempt += 1) {
    try {
      await streamResponseEvents(ctx.responseId, streamOptions);
      return;
    } catch (err: unknown) {
      const status = (err as { status?: number }).status;
      // Only transient failures (network, 5xx) resume; 4xx means the server rejected this stream.
      if (abortCtrl.signal.aborted || ctx.run.terminal || (status !== undefined && status < 500) || attempt >= MAX_RESUME_ATTEMPTS) throw err;
      // Without a cursor the server replays from the start, so drop partial local output to avoid duplicates.
      if (!streamOptions.lastEventId && ctx.isCurrentView()) ctx.live.dispatch({ type: 'clearPartial' });
      await new Promise<void>((resolve) => {
        const timer = window.setTimeout(resolve, RESUME_BACKOFF_MS * 2 ** attempt);
        abortCtrl.signal.addEventListener('abort', () => { window.clearTimeout(timer); resolve(); }, { once: true });
      });
      if (abortCtrl.signal.aborted) return;
    }
  }
}

/**
 * Stuck-"Generating" recovery: the stream ended (or reported `unavailable`) without a terminal event. Clears the live
 * state, refreshes the transcript, and reattaches once if the server still reports the run active.
 *
 * @param ctx - Stream context.
 */
async function recoverLostStream(ctx: StreamContext): Promise<void> {
  const { responseId, isCurrentView, live, refs } = ctx;
  if (isCurrentView()) {
    live.clearOutput();
    live.patch({ isStreaming: false, isPending: false, activeResponseId: null });
  }
  // Only the first lost stream of a response may reattach; later ones just reset the UI (no retry storm).
  const firstRecovery = !refs.reattached.has(responseId);
  refs.reattached.add(responseId);
  void ctx.queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
  const detail = await ctx.queryClient.fetchQuery({
    queryKey: chatKeys.conversation(ctx.conversationId),
    queryFn: () => getConversation(ctx.conversationId),
    staleTime: 0,
  }).catch(() => null);
  if (firstRecovery && refs.attachCount.get(responseId) === 1 && detail?.active_response_id === responseId && !refs.attachedStream && isCurrentView()) {
    live.patch({ activeResponseId: responseId, isStreaming: true });
    await refs.streamRun?.(responseId, ctx.conversationId, isCurrentView).catch(() => undefined);
  }
}

/**
 * Hook owning durable-response stream consumption for the chat surface.
 *
 * @param refs - Shared mutable session refs.
 * @param live - Live-state actions.
 * @param setThemeRequest - Receives theme requests from the assistant.
 * @returns `streamResponseRun(responseId, conversationId, isCurrentView)`, stable per query client / translator. A fresh
 * attachment starts from the first retained SSE event and never resends a prompt; an identical live attachment is joined.
 */
export function useChatStream(
  refs: ChatSessionRefs,
  live: ChatLiveActions,
  setThemeRequest: (mode: 'light' | 'dark' | 'system') => void,
): (responseId: string, conversationId: string, isCurrentView: IsCurrentView) => Promise<void> {
  const queryClient = useQueryClient();
  const tChat = useTranslations('chat');
  const t = tChat as unknown as StreamContext['t'];
  const streamResponseRun = React.useCallback(async (
    responseId: string,
    responseConversationId: string,
    isCurrentView: IsCurrentView,
  ) => {
    const attached = refs.attachedStream;
    if (attached?.responseId === responseId && attached.conversationId === responseConversationId && !attached.controller.signal.aborted) {
      await attached.promise;
      return;
    }
    attached?.controller.abort();
    refs.attachCount.set(responseId, (refs.attachCount.get(responseId) ?? 0) + 1);
    const abortCtrl = new AbortController();
    refs.setAbortController(abortCtrl);
    const run: StreamRunState = { terminal: false, unavailable: false, promise: null, attachment: null };
    const ctx: StreamContext = { responseId, conversationId: responseConversationId, isCurrentView, live, refs, queryClient, t, abortCtrl, run };
    const streamOptions: StreamEventsOptions = {
      signal: abortCtrl.signal,
      ...buildContentHandlers(live, isCurrentView, setThemeRequest),
      // Live title/suggestions (rare: the stream usually closes first) go straight into the cache; persisted values win later.
      onConversationTitle: (title) => applyLiveTitle(queryClient, responseConversationId, title),
      onSuggestions: (payload) => applyLiveSuggestions(queryClient, responseConversationId, payload),
      onDone: () => handleDone(ctx),
      onStatus: (status, payload) => handleStatus(ctx, status, payload),
      // Failures are surfaced once, by the resume loop, after automatic resumes are exhausted.
      onError: () => undefined,
    };
    const streamPromise = consumeWithResume(ctx, streamOptions);
    run.promise = streamPromise;
    const streamAttachment = { responseId, conversationId: responseConversationId, controller: abortCtrl, promise: streamPromise };
    run.attachment = streamAttachment;
    refs.setAttachedStream(streamAttachment);
    try {
      await streamPromise;
    } catch (streamError: unknown) {
      // fetch() can reject before streamResponseEvents reaches its SSE reader error callback.
      if (!abortCtrl.signal.aborted && isCurrentView()) {
        live.patch({ isStreaming: false, isPending: false, activeResponseId: null, error: t(chatFailureKey(streamError, 'errorSending')) });
      }
      throw streamError;
    } finally {
      if (refs.attachedStream === streamAttachment) {
        refs.setAttachedStream(null);
        if (refs.abortController === abortCtrl) refs.setAbortController(null);
      }
    }
    if (!run.terminal && (run.unavailable || !abortCtrl.signal.aborted)) await recoverLostStream(ctx);
  }, [queryClient, t, refs, live, setThemeRequest]);
  React.useEffect(() => { refs.setStreamRun(streamResponseRun); }, [refs, streamResponseRun]);
  return streamResponseRun;
}
