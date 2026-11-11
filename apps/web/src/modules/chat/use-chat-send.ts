import * as React from 'react';
import { ApiError } from '@/core/api';
import type { QueryClient } from '@tanstack/react-query';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { PendingMessageMutationConflictError, type PendingSpecialistRun, type useChatController } from '@/core/app-shell/chat-controller';
import { uuid7 } from '@/core/uuid7';
import { startAgentRun, type AgentProfile } from '@/modules/agents/public-api';
import {
  chatKeys,
  createConversation,
  hashMessageContent,
  mutateMessage,
  sendMessage,
  type ChatMessage,
} from './api';
import { chatFailureKey, LocalizedError, SELECTION_ERROR_KEYS } from './chat-errors';
import { MAX_ITEMS, stripItemTitles } from './chat-context-bar';
import { withAttachments, type useChatAttachments } from './chat-attachments';
import type { ChatLiveActions } from './chat-live-state';
import type { ChatSessionRefs } from './chat-session-refs';
import type { ChatModelPicker } from './model-picker';
import type { IsCurrentView } from './use-chat-stream';

type ChatController = ReturnType<typeof useChatController>;
type Translator = (key: string, values?: Record<string, string | number>) => string;

/** Everything the send/mutation callbacks read from the surrounding session (all values are render-time snapshots). */
export interface ChatSendDeps {
  refs: ChatSessionRefs;
  live: ChatLiveActions;
  chatCtrl: ChatController;
  csrfToken: string;
  conversationId?: string | null;
  mode: 'drawer' | 'full';
  onConversationCreated?: (id: string) => void;
  messages: ChatMessage[];
  isBusy: boolean;
  providerReady: boolean;
  openProviderGate: () => void;
  modelChoice: ChatModelPicker['value'] | null;
  webSearchRequested: boolean;
  dbQueryEffective: boolean;
  /** Restores the per-send tool toggles (web search on, db query off) once the server accepted a send. */
  resetToggles: () => void;
  effectiveEditingMessageId: string | null;
  clearEditing: () => void;
  selectedProfileId: string;
  profiles: AgentProfile[] | undefined;
  pendingProfileRun: PendingSpecialistRun | null;
  attachments: ReturnType<typeof useChatAttachments>;
  setActiveAgentRunId: (id: string) => void;
  streamResponseRun: (responseId: string, conversationId: string, isCurrentView: IsCurrentView) => Promise<void>;
  queryClient: QueryClient;
  t: Translator;
  tAgents: Translator;
}

/** Ownership of the mounted selection generation by one send; guards every local UI update after an await. */
interface ViewScope {
  sendGeneration: number;
  viewGeneration: number;
  viewConversationId: string | null;
  /** True only while this send still owns the mounted selection generation. */
  isCurrent: IsCurrentView;
}

/**
 * Claims a new send generation (orphaning earlier sends) and captures the current selection.
 *
 * @param refs - Shared session refs.
 * @returns The scope; `viewGeneration`/`viewConversationId` may be advanced when the send promotes a new conversation.
 */
function createViewScope(refs: ChatSessionRefs): ViewScope {
  const sendGeneration = refs.bumpSendGeneration();
  const scope: ViewScope = {
    sendGeneration,
    viewGeneration: refs.selection.generation,
    viewConversationId: refs.selection.conversationId,
    isCurrent: () => refs.mounted
      && refs.sendGeneration === sendGeneration
      && refs.selection.generation === scope.viewGeneration
      && refs.selection.conversationId === scope.viewConversationId,
  };
  return scope;
}

/** Generates the idempotency key of a send; falls back when the Web Crypto API is unavailable. */
function newClientRequestId(): string {
  return typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function'
    ? uuid7()
    : `req-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

/** Puts the live state into "streaming response `responseId`" with all previous output cleared. */
function beginStreaming(live: ChatLiveActions, responseId: string, clearInput: boolean): void {
  live.clearOutput();
  live.patch({ activeResponseId: responseId, isPending: false, isStreaming: true, steps: [], ...(clearInput ? { inputRequest: null } : {}) });
}

/**
 * Submits an edit or regeneration using a stable shared request envelope until acknowledged.
 *
 * @param d - Session snapshot.
 * @param action - Whether to append a changed prompt or reuse the original prompt.
 * @param targetMessageId - User prompt to edit or assistant response to regenerate.
 * @param content - Replacement prompt text for an edit; omitted for regeneration.
 * @remarks Ignored while busy or when the target message is unknown. Failures surface through `live.error`.
 */
async function runMessageMutation(
  d: ChatSendDeps,
  action: 'edit' | 'regenerate',
  targetMessageId: string,
  content?: string,
): Promise<void> {
  const { refs, live, chatCtrl, queryClient } = d;
  const targetConversationId = d.conversationId ?? refs.selection.conversationId;
  const target = d.messages.find((message) => message.id === targetMessageId);
  if (!targetConversationId || !target || d.isBusy) return;
  const scope = createViewScope(refs);
  live.patch({ error: null, cancelled: false, isPending: true });
  try {
    const baseContentHash = await hashMessageContent(target.content);
    const envelope = chatCtrl.getOrCreatePendingMessageMutation(targetConversationId, { action, targetMessageId, baseContentHash, content });
    const run = await mutateMessage(
      targetConversationId,
      targetMessageId,
      {
        action: envelope.action,
        base_content_hash: envelope.baseContentHash,
        client_request_id: envelope.clientRequestId,
        ...(envelope.content !== undefined ? { content: envelope.content } : {}),
        ...(d.mode === 'full' && d.webSearchRequested ? { web_search: true } : {}),
        ...(d.dbQueryEffective ? { db_query: true } : {}),
        ...(d.modelChoice ? { provider_id: d.modelChoice.providerId, model: d.modelChoice.model } : {}),
      },
      d.csrfToken,
    );
    d.resetToggles();
    chatCtrl.clearPendingMessageMutation(targetConversationId, envelope.clientRequestId);
    if (scope.isCurrent()) {
      d.clearEditing();
      chatCtrl.setDraft('');
    }
    void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
    if (!scope.isCurrent()) {
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(targetConversationId) });
      return;
    }
    beginStreaming(live, run.response_id, false);
    await d.streamResponseRun(run.response_id, targetConversationId, scope.isCurrent);
  } catch (mutationError: unknown) {
    if (scope.isCurrent()) {
      live.patch({
        isPending: false,
        error: mutationError instanceof PendingMessageMutationConflictError
          ? d.t('retryOriginalRevisionFirst')
          : d.t(chatFailureKey(mutationError, 'revisionFailed')),
      });
    }
  }
}

/** Mutable facts about one send attempt that the error mapper needs. */
interface SendAttempt {
  /** Conversation the message is sent to (created on demand). */
  targetId: string | null | undefined;
  /** Set once the server has accepted the message/run. */
  accepted: boolean;
  /** True only while the selection precheck / sendMessage can fail. */
  selectionStage: boolean;
  attemptedProfileRun: PendingSpecialistRun | null;
}

/** Resolves the day (date + timezone) of a day-context conversation before any await, so it stays bound to it. */
function captureDay(chatCtrl: ChatController): { date: string; timezone: string } | null {
  const ctx = chatCtrl.context;
  return ctx?.kind === 'day' && ctx.date && ctx.timezone ? { date: ctx.date, timezone: ctx.timezone } : null;
}

/**
 * Validates the specialist selection for this send.
 *
 * @returns The selected enabled profile (undefined for the assistant or a retry envelope).
 * @throws LocalizedError when a specialist is selected but unavailable.
 */
function resolveSelectedProfile(d: ChatSendDeps, retryEnvelope: PendingSpecialistRun | null): AgentProfile | undefined {
  const selected = d.mode === 'full' && d.selectedProfileId !== 'assistant'
    ? d.profiles?.find((profile) => profile.id === d.selectedProfileId)
    : undefined;
  if (d.mode === 'full' && d.selectedProfileId !== 'assistant' && !retryEnvelope
      && (!selected || !selected.enabled || selected.capability === 'unavailable')) {
    throw new LocalizedError(d.tAgents('agentUnavailable'));
  }
  return selected;
}

/**
 * Creates the conversation on first send and, when this send still owns the view, promotes it as the selection.
 *
 * @param day - Captured day context, stored immutably on the conversation.
 */
async function ensureConversation(
  d: ChatSendDeps,
  scope: ViewScope,
  attempt: SendAttempt,
  content: string,
  day: { date: string; timezone: string } | null,
): Promise<void> {
  const { chatCtrl, refs } = d;
  const titleExcerpt = content.slice(0, 48).trim() || d.t('newConversation');
  const created = await createConversation(
    {
      title: titleExcerpt,
      context_kind: chatCtrl.context?.kind,
      context_resource_id: chatCtrl.context?.resource_id,
      // A day conversation stores its immutable day so it can be recognized later.
      metadata: day ? { date: day.date, timezone: day.timezone } : undefined,
    },
    d.csrfToken,
  );
  const targetId = created.id;
  attempt.targetId = targetId;
  if (day) chatCtrl.bindDayConversation(day, targetId);
  if (scope.isCurrent()) {
    const nextGeneration = scope.viewGeneration + 1;
    refs.setPromotedSelection({ conversationId: targetId, generation: nextGeneration, sendGeneration: scope.sendGeneration });
    if (d.onConversationCreated) d.onConversationCreated(targetId);
    else chatCtrl.setActiveConversationId(targetId);
    scope.viewGeneration = nextGeneration;
    scope.viewConversationId = targetId;
    // Update the ref now: the controller change only re-renders later, and the draft clear below must see this view as current.
    refs.setSelection({ conversationId: targetId, generation: nextGeneration });
  }
  void d.queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
}

/**
 * Starts (or retries) a specialist agent run, keeping its immutable envelope in shared memory before awaiting a
 * response that may be lost.
 */
async function startSpecialistRun(
  d: ChatSendDeps,
  scope: ViewScope,
  attempt: SendAttempt,
  content: string,
  retryEnvelope: PendingSpecialistRun | null,
  profile: AgentProfile | undefined,
): Promise<void> {
  const { chatCtrl } = d;
  if (!retryEnvelope && !profile) throw new LocalizedError(d.tAgents('agentUnavailable'));
  const envelope: PendingSpecialistRun = retryEnvelope ?? {
    prompt: content,
    profileId: profile!.id,
    profileRevision: profile!.revision,
    conversationId: attempt.targetId!,
    clientRequestId: newClientRequestId(),
  };
  attempt.attemptedProfileRun = envelope;
  chatCtrl.setPendingSpecialistRun(envelope);
  const run = await startAgentRun(envelope.profileId, {
    prompt: envelope.prompt,
    expected_profile_revision: envelope.profileRevision,
    conversation_id: envelope.conversationId,
    client_request_id: envelope.clientRequestId,
  }, d.csrfToken);
  chatCtrl.clearPendingSpecialistRun(envelope.conversationId);
  void d.queryClient.invalidateQueries({ queryKey: ['agent-runs', 'conversation', attempt.targetId] });
  if (scope.isCurrent()) {
    chatCtrl.setDraft('');
    d.setActiveAgentRunId(run.id);
    d.live.patch({ isPending: false });
  }
}

/**
 * Sends a plain assistant message and attaches to its durable response stream.
 *
 * @param sendContext - Context (with ready attachments merged) sent with the message.
 * @remarks The draft is cleared optimistically before the request; the caller restores it when the server rejects.
 */
async function sendAssistantMessage(
  d: ChatSendDeps,
  scope: ViewScope,
  attempt: SendAttempt,
  content: string,
  sendContext: ReturnType<typeof stripItemTitles>,
): Promise<void> {
  const { chatCtrl, live, queryClient } = d;
  // Optimistically clear draft
  if (scope.isCurrent()) chatCtrl.setDraft('');
  // Deterministic client_request_id for idempotency
  const clientRequestId = newClientRequestId();
  const responseConversationId = attempt.targetId!;
  attempt.selectionStage = true;
  const responseRun = await sendMessage(
    responseConversationId,
    {
      content,
      client_request_id: clientRequestId,
      context: sendContext ?? undefined,
      ...(d.mode === 'full' && d.webSearchRequested ? { web_search: true } : {}),
      ...(d.dbQueryEffective ? { db_query: true } : {}),
      ...(d.modelChoice ? { provider_id: d.modelChoice.providerId, model: d.modelChoice.model } : {}),
    },
    d.csrfToken,
  );
  attempt.accepted = true;
  d.resetToggles();
  if (d.attachments.items.length && scope.isCurrent()) d.attachments.clear();
  attempt.selectionStage = false;
  if (!scope.isCurrent()) {
    void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(responseConversationId) });
    void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
    return;
  }
  // The user message is persisted server-side; refetch so its bubble shows even if the run later fails.
  void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(responseConversationId) });
  beginStreaming(live, responseRun.response_id, true);
  await d.streamResponseRun(responseRun.response_id, responseConversationId, scope.isCurrent);
}

/**
 * Localizes a failed send. Selection failures are identified by the server's machine code (never by message text).
 *
 * @returns The user-visible error message.
 */
function describeSendError(
  d: ChatSendDeps,
  attempt: SendAttempt,
  err: unknown,
  content: string,
  hasSelectionItems: boolean,
): string {
  const selectionFailure = attempt.selectionStage && hasSelectionItems && err instanceof ApiError && !!err.code
    && Object.hasOwn(SELECTION_ERROR_KEYS, err.code);
  if (err instanceof LocalizedError) return err.message;
  if (selectionFailure) return d.t(chatFailureKey(err, 'errorSending'));
  if (attempt.attemptedProfileRun?.prompt === content) return d.tAgents('agentStartFailed');
  return d.t(chatFailureKey(err, 'errorSending'));
}

/**
 * Dispatches a message to an existing or newly initialized conversation, retaining an unresolved specialist's
 * immutable retry envelope in shared memory before awaiting the durable start response.
 *
 * @param d - Session snapshot.
 * @param content - Message text to send.
 * @returns True only when the server accepted the message (a later stream failure still counts as accepted); false on
 * early returns and caught errors, which surface through the session error banner.
 */
async function runSend(d: ChatSendDeps, content: string): Promise<boolean> {
  const { refs, live, chatCtrl } = d;
  // Key gate: with no usable chat model keep the text as the composer draft and explain, instead of sending.
  if (!d.providerReady) {
    chatCtrl.setDraft(content);
    d.openProviderGate();
    return false;
  }
  refs.setLastSent(content);
  if (d.mode === 'full' && d.effectiveEditingMessageId) {
    await runMessageMutation(d, 'edit', d.effectiveEditingMessageId, content);
    // Edit is an append-only mutation, not a widget send; its outcome is not reported here.
    return false;
  }
  const scope = createViewScope(refs);
  live.patch({ error: null, cancelled: false, isPending: true });
  if (rejectUnresolvedSpecialist(d, content)) return false;
  return executeSend(d, scope, content);
}

/**
 * Refuses a send while another specialist prompt is still unacknowledged: its prompt is restored into the composer and
 * the retry-pending error shown. Only an exact retry of this conversation's own pending run (full mode) may proceed.
 *
 * @returns True when the send was rejected.
 */
function rejectUnresolvedSpecialist(d: ChatSendDeps, content: string): boolean {
  const { pendingProfileRun, chatCtrl, mode } = d;
  const otherPending = Object.values(chatCtrl.pendingSpecialistRuns).find((run) => run.prompt === content) ?? null;
  const unresolved = pendingProfileRun ?? otherPending;
  if (!unresolved || (unresolved === pendingProfileRun && unresolved.prompt === content && mode === 'full')) return false;
  chatCtrl.setDraft(unresolved.prompt);
  d.live.patch({ isPending: false, error: d.tAgents('agentRetryPending') });
  return true;
}

/**
 * Resets the live state after a failed send and shows the localized error, only while this send still owns the view.
 * A send the server did not accept (409/422/429/network) must not lose the text the composer already cleared.
 */
function reportSendFailure(d: ChatSendDeps, scope: ViewScope, attempt: SendAttempt, err: unknown, content: string, hasSelectionItems: boolean): void {
  if (!scope.isCurrent()) return;
  d.live.patch({ isPending: false, isStreaming: false, activeResponseId: null });
  if (!attempt.accepted) d.chatCtrl.setDraft(content);
  d.live.patch({ error: describeSendError(d, attempt, err, content, hasSelectionItems) });
}

/**
 * Pre-validates the server's 1..32 distinct-document selection limit before the draft is cleared.
 *
 * @throws ApiError 422 `selection_too_large` (flagged as a selection-stage failure) when the limit is exceeded.
 */
function assertSelectionWithinLimit(selItems: { documentId: string }[], attempt: SendAttempt): void {
  if (selItems.length > MAX_ITEMS || new Set(selItems.map((i) => i.documentId)).size !== selItems.length) {
    attempt.selectionStage = true;
    throw new ApiError(422, 'selection limit', { code: 'selection_too_large' });
  }
}

/**
 * Runs the accepted send: selection precheck, conversation creation, then either a specialist run or an assistant
 * message with its stream. Failures are localized into the session error banner.
 *
 * @returns True once the server accepted the message/run; false when it was not accepted.
 */
async function executeSend(d: ChatSendDeps, scope: ViewScope, content: string): Promise<boolean> {
  const { chatCtrl, mode, pendingProfileRun } = d;
  const retryEnvelope = pendingProfileRun?.prompt === content ? pendingProfileRun : null;
  const attempt: SendAttempt = { targetId: retryEnvelope?.conversationId ?? d.conversationId, accepted: false, selectionStage: false, attemptedProfileRun: null };
  // Day captured before any await: a day conversation is bound to it no matter where the user navigates.
  const day = captureDay(chatCtrl);
  // Ready Chat attachments join the U3 selection mechanism; the composer blocks Send while any are not ready.
  const sendContext = stripItemTitles(mode === 'full' ? withAttachments(chatCtrl.context, d.attachments.items) : chatCtrl.context);
  const selItems = sendContext?.kind === 'selection' ? sendContext.items ?? [] : [];
  try {
    assertSelectionWithinLimit(selItems, attempt);
    const selected = resolveSelectedProfile(d, retryEnvelope);
    // If no active conversation exists, create one first with initial title from message excerpt
    if (!attempt.targetId) await ensureConversation(d, scope, attempt, content, day);
    if (mode === 'full' && (d.selectedProfileId !== 'assistant' || retryEnvelope)) {
      await startSpecialistRun(d, scope, attempt, content, retryEnvelope, selected);
      return true;
    }
    await sendAssistantMessage(d, scope, attempt, content, sendContext);
  } catch (err: unknown) {
    reportSendFailure(d, scope, attempt, err, content, selItems.length > 0);
    return attempt.accepted;
  }
  return true;
}

/**
 * Hook providing the chat surface's send and message-revision commands.
 *
 * @param deps - Snapshot of the session state they depend on (see {@link ChatSendDeps}); `queryClient`, `t` and
 * `tAgents` are supplied by the hook itself.
 * @returns `handleMessageMutation(action, targetMessageId, content?)` and `handleSend(content)` (resolves true once the
 * server accepted the message).
 */
export function useChatSend(
  deps: Omit<ChatSendDeps, 'queryClient' | 't' | 'tAgents'>,
): {
  handleMessageMutation: (action: 'edit' | 'regenerate', targetMessageId: string, content?: string) => Promise<void>;
  handleSend: (content: string) => Promise<boolean>;
} {
  const queryClient = useQueryClient();
  const t = useTranslations('chat') as unknown as Translator;
  const tAgents = useTranslations('aiSettings') as unknown as Translator;
  // Rebuilt only when a real input changes (mirrors the dependency lists of the callbacks this replaced).
  const full = React.useMemo<ChatSendDeps>(
    () => ({ ...deps, queryClient, t, tAgents }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `deps` is a fresh object per render; its members are listed
    [deps.refs, deps.live, deps.chatCtrl, deps.csrfToken, deps.conversationId, deps.mode, deps.onConversationCreated, deps.messages, deps.isBusy, deps.providerReady, deps.openProviderGate, deps.modelChoice, deps.webSearchRequested, deps.dbQueryEffective, deps.resetToggles, deps.effectiveEditingMessageId, deps.clearEditing, deps.selectedProfileId, deps.profiles, deps.pendingProfileRun, deps.attachments, deps.setActiveAgentRunId, deps.streamResponseRun, queryClient, t, tAgents],
  );
  /** Edits a user prompt or regenerates an assistant response; failures surface through the live error banner. */
  const handleMessageMutation = React.useCallback(
    (action: 'edit' | 'regenerate', targetMessageId: string, content?: string) => runMessageMutation(full, action, targetMessageId, content),
    [full],
  );
  /** Sends a message; resolves true once the server accepted it. */
  const handleSend = React.useCallback((content: string) => runSend(full, content), [full]);
  return { handleMessageMutation, handleSend };
}
