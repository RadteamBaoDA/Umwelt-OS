import * as React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { cancelResponse, chatKeys, getConversation, submitResponseInput, type ChatMessage } from './api';
import { settleWidgetPrompt } from './generative-ui/bridge/widget-prompt';
import { useChatAttachments } from './chat-attachments';
import { mergeOptimistic, useOptimisticMessages } from './chat-optimistic';
import { useChatLiveState } from './chat-live-state';
import { useChatSessionRefs } from './chat-session-refs';
import { useChatModelPicker } from './model-picker';
import { useChatProviderReady } from './provider-gate';
import { useChatAgentRuns } from './use-chat-agent-runs';
import { useChatComposerState } from './use-chat-composer-state';
import { useSourceMention, type SourceMention } from './chat-source-picker';
import { useChatSend } from './use-chat-send';
import { useChatStream } from './use-chat-stream';
import { useChatWebSearch } from './use-chat-web-search';
import { useConversationSelection } from './use-conversation-selection';
import { useFollowupRefresh } from './use-followup-refresh';

/** Inputs of the session hooks (the data-bearing subset of ChatSessionProps). */
export interface ChatSessionOptions {
  conversationId?: string | null;
  mode: 'drawer' | 'full';
  onConversationCreated?: (id: string) => void;
}

/**
 * Whether files can ride along with a send. Specialist runs and prompt edits carry no chat context, and a
 * day/document/entity context cannot hold a selection, so attachments cannot accompany any of them.
 *
 * @param profileBlocked - A specialist profile is selected or pending.
 * @param editingMessageId - Message being edited, if any.
 * @param context - Active chat context.
 * @returns True when attachments must be disabled.
 */
function areAttachmentsUnavailable(profileBlocked: boolean, editingMessageId: string | null, context: { kind: string } | null): boolean {
  return profileBlocked || !!editingMessageId || (!!context && context.kind !== 'selection');
}

/**
 * Collects every piece of state and every query the chat surface renders from (no commands).
 *
 * @param options - Conversation id and host mode.
 * @returns Live-state reducer, composer state, controller, queries and the derived flags (pending runs, busy, agent mode,
 * attachment availability) shared by the command hook and the view.
 */
export function useChatSessionData({ conversationId, mode }: ChatSessionOptions) {
  const session = useWorkspaceSession();
  const chatCtrl = useChatController();
  const { live, actions } = useChatLiveState();
  const refs = useChatSessionRefs({ conversationId: chatCtrl.activeConversationId, generation: chatCtrl.activeConversationGeneration });
  const composer = useChatComposerState();
  const pendingProfileRun = conversationId ? (chatCtrl.pendingSpecialistRuns[conversationId] ?? null) : null;
  const pendingMessageMutation = conversationId ? (chatCtrl.pendingMessageMutations[conversationId] ?? null) : null;
  const effectiveEditingMessageId =
    composer.editingMessageId ?? (pendingMessageMutation?.action === 'edit' ? pendingMessageMutation.targetMessageId : null);
  // Fetch full conversation details if conversationId is defined
  const detail = useQuery({
    queryKey: chatKeys.conversation(conversationId ?? ''),
    queryFn: () => getConversation(conversationId!),
    enabled: Boolean(conversationId),
  });
  const serverMessages: ChatMessage[] = React.useMemo(() => detail.data?.messages ?? [], [detail.data?.messages]);
  // The user's own message shows at once; the server copy replaces it (matched by client_request_id) on refetch.
  const optimisticMessages = useOptimisticMessages(conversationId, serverMessages);
  const messages: ChatMessage[] = React.useMemo(
    () => mergeOptimistic(serverMessages, optimisticMessages),
    [serverMessages, optimisticMessages],
  );
  const latestAssistant = React.useMemo(() => [...messages].reverse().find((message) => message.role === 'assistant'), [messages]);
  // Specialist-agent sends (non-assistant profile or a pending profile retry) do not carry web_search or attachments.
  const profileBlocked = composer.selectedProfileId !== 'assistant' || Boolean(pendingProfileRun);
  const attachmentsUnavailable = areAttachmentsUnavailable(profileBlocked, effectiveEditingMessageId, chatCtrl.context);
  const attachments = useChatAttachments(session.csrfToken, attachmentsUnavailable);
  const { sourceMention, sourceTextareaRef } = useComposerSourceMention(
    chatCtrl,
    mode,
    profileBlocked,
    attachments.items.length,
    effectiveEditingMessageId !== null,
  );
  const busy = live.isPending || live.isStreaming;
  const queryClient = useQueryClient();
  const { refetch: refetchDetail } = detail;
  /** Picks up the auto title and suggestions written after the stream closed (see useFollowupRefresh). */
  const refreshFollowup = React.useCallback(() => {
    void refetchDetail();
    void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
  }, [refetchDetail, queryClient]);
  useFollowupRefresh({ conversationId, messages, busy, refresh: refreshFollowup });
  return {
    session,
    chatCtrl,
    live,
    actions,
    refs,
    composer,
    detail,
    messages,
    latestAssistant,
    pendingProfileRun,
    pendingMessageMutation,
    effectiveEditingMessageId,
    profileBlocked,
    attachmentsUnavailable,
    busy,
    /** True while files may be added (drop zone, paste and attach controls share the rule). */
    canAttachFiles: canAddFiles(busy, attachmentsUnavailable, attachments.full, sourceMention.chips.length > 0),
    // Plain Q&A (Assistant, no run pending) shows no agent or task chrome; only a specialist selection does.
    agentMode: mode === 'full' && profileBlocked,
    modelPicker: useChatModelPicker(),
    providerReady: useChatProviderReady(),
    webSearch: useChatWebSearch(mode, profileBlocked),
    attachments,
    sourceMention,
    sourceTextareaRef,
  };
}

/**
 * The `@` source picker of the full composer. It is enabled only on the full Chat page, for the Assistant profile,
 * outside selection and gadget contexts, with no attachments and outside edit mode. When it becomes disabled, the
 * picker drops its chips (see `useSourceMention`), so a send can never carry a scope it cannot honour.
 *
 * @param chatCtrl - Chat controller that owns the draft and the view context.
 * @param mode - Host surface; the picker is never enabled in the drawer.
 * @param profileBlocked - True while a specialist profile is selected or a specialist retry is pending.
 * @param attachmentCount - Attachments on the draft (any attachment disables the picker).
 * @param editing - True while a previous message is being edited; edit sends reuse the original run's context.
 * @returns The picker state and the textarea ref it needs to read the caret.
 */
function useComposerSourceMention(
  chatCtrl: ReturnType<typeof useChatController>,
  mode: 'drawer' | 'full',
  profileBlocked: boolean,
  attachmentCount: number,
  editing: boolean,
) {
  const sourceTextareaRef = React.useRef<HTMLTextAreaElement>(null);
  const kind = chatCtrl.context?.kind;
  const sourceMention = useSourceMention({
    value: chatCtrl.draft,
    setValue: chatCtrl.setDraft,
    textareaRef: sourceTextareaRef,
    enabled: mode === 'full' && !profileBlocked && kind !== 'selection' && kind !== 'gadget' && attachmentCount === 0 && !editing,
    scopeKey: chatCtrl.activeConversationId,
  });
  return { sourceMention, sourceTextareaRef };
}

/**
 * Send-side view of the picker: the chosen source ids and the clear command, or undefined when nothing is chosen.
 *
 * @param picker - The composer's source picker state.
 * @returns The scope for the send hook, or undefined when no source is chosen.
 */
function sourceScopeOf(picker: SourceMention): { ids: string[]; clear: () => void } | undefined {
  return picker.chips.length ? { ids: picker.chips.map((chip) => chip.id), clear: picker.clear } : undefined;
}

/**
 * Whether files may be added: not mid-turn, attachments available for this send, below the per-message limit, and no
 * `@` source chosen. A chosen source holds the turn to its scope, and attaching a file would disable the picker and
 * silently drop the chips, so drop, paste and the attach control all share this block.
 *
 * @param busy - A turn is in flight.
 * @param unavailable - Attachments cannot be used for this send.
 * @param full - The per-message attachment limit is reached.
 * @param hasSources - At least one `@` source chip is chosen for this turn.
 * @returns True when a file may be attached now.
 */
export function canAddFiles(busy: boolean, unavailable: boolean, full: boolean, hasSources: boolean): boolean {
  return !busy && !unavailable && !full && !hasSources;
}

/** Return type of {@link useChatSessionData}. */
export type ChatSessionData = ReturnType<typeof useChatSessionData>;

/**
 * Builds the append-only edit and regenerate commands of the full Chat transcript.
 *
 * @param d - State from {@link useChatSessionData} (controller draft and composer edit state).
 * @param mode - Hosting mode; edit and regenerate are no-ops outside `full`.
 * @param handleMessageMutation - Send-hook command that submits a revision or regeneration.
 * @returns `handleEditMessage` (stashes the typed draft once, then loads the message), `handleCancelEdit` (restores the
 * stash) and `handleRegenerateMessage` (assistant messages only) and `handleRetryMessage` (user prompts).
 */
function useMessageEditCommands(
  d: ChatSessionData,
  mode: ChatSessionOptions['mode'],
  handleMessageMutation: (action: 'regenerate' | 'edit', targetMessageId: string, content?: string) => Promise<unknown>,
) {
  const { chatCtrl, composer } = d;
  const { setEditingMessageId, draftStashRef, editingMessageId } = composer;
  const { setDraft } = chatCtrl;
  /** Opens the full Chat composer with a user message's content for an append-only edit. */
  const handleEditMessage = React.useCallback(
    (message: ChatMessage) => {
      if (mode !== 'full' || message.role !== 'user') return;
      // Stash the typed draft once (re-editing another message keeps the original stash) so Cancel can restore it.
      if (!editingMessageId) draftStashRef.current = chatCtrl.draft;
      setEditingMessageId(message.id);
      setDraft(message.content);
    },
    [mode, setEditingMessageId, setDraft, editingMessageId, draftStashRef, chatCtrl.draft],
  );
  /** Leaves edit mode and restores the draft that was in the composer before the edit began. */
  const handleCancelEdit = React.useCallback(() => {
    setEditingMessageId(null);
    setDraft(draftStashRef.current ?? '');
    draftStashRef.current = null;
  }, [setEditingMessageId, setDraft, draftStashRef]);
  /** Reuses the parent assistant message's captured context for a new answer branch. */
  const handleRegenerateMessage = React.useCallback(
    (message: ChatMessage) => {
      if (mode === 'full' && message.role === 'assistant') void handleMessageMutation('regenerate', message.id);
    },
    [handleMessageMutation, mode],
  );
  /**
   * Retries a turn whose answer was lost or stopped before it was stored: an edit mutation with the same text, which the
   * server records as a new version of the same turn. A 409 (a run still active) surfaces the localized busy message.
   */
  const handleRetryMessage = React.useCallback(
    (message: ChatMessage) => {
      if (mode === 'full' && message.role === 'user') void handleMessageMutation('edit', message.id, message.content);
    },
    [handleMessageMutation, mode],
  );
  return { handleEditMessage, handleCancelEdit, handleRegenerateMessage, handleRetryMessage };
}

/**
 * Builds the Stop command: aborts the local stream, cancels the server run, then shows the neutral stopped state.
 *
 * @param d - State from {@link useChatSessionData}.
 * @returns `handleStop`; it records `stoppedResponseId` so the transcript can tell the stopped run's stored answer from
 * an older last answer (the server never persists a partial on Stop).
 */
function useStopCommand(d: ChatSessionData) {
  const { session, live, actions, refs } = d;
  const { refetch: refetchDetail } = d.detail;
  const activeResponseId = live.activeResponseId;
  /** Explicitly cancels active response generation without losing transcript state. */
  return React.useCallback(async () => {
    if (refs.abortController) {
      refs.abortController.abort();
      refs.setAbortController(null);
    }
    if (activeResponseId) {
      try {
        await cancelResponse(activeResponseId, session.csrfToken);
      } catch (err) {
        console.warn('Failed to dispatch cancel endpoint', err);
      }
    }
    // User-initiated stop is a neutral status line, never an error banner (so no Retry is offered).
    actions.patch({
      isStreaming: false,
      isPending: false,
      activeResponseId: null,
      stoppedResponseId: activeResponseId ?? null,
      error: null,
      cancelled: true,
    });
    void refetchDetail();
  }, [activeResponseId, refetchDetail, session.csrfToken, refs, actions]);
}

/**
 * Builds the selection-change reset. Every entry point (header, history, shortcut) funnels through here: a fresh chat
 * also restores the toggle defaults (C6/C9), and the attachment-sharing answer is forgotten unless the change merely
 * adopts the conversation this send created (C5).
 *
 * @param d - Session state (composer, attachments).
 * @param agent - Specialist-run data; its active run id is cleared on every selection change.
 * @returns A stable callback for `useConversationSelection`.
 */
function useSelectionReset(d: ChatSessionData, agent: ReturnType<typeof useChatAgentRuns>) {
  const { setEditingMessageId, draftStashRef, resetToggles } = d.composer;
  const { setActiveAgentRunId } = agent;
  const { resetShareChoice } = d.attachments;
  return React.useCallback(
    ({ conversationId: selected, promoted }: { conversationId: string | null; promoted: boolean }) => {
      setActiveAgentRunId(null);
      setEditingMessageId(null);
      // A stash belongs to the conversation being edited; it must not leak into the next one.
      draftStashRef.current = null;
      // The attachment-sharing answer is remembered per conversation (C5), including the one a first send creates.
      if (!promoted) resetShareChoice();
      if (!selected) resetToggles();
    },
    [setActiveAgentRunId, setEditingMessageId, draftStashRef, resetShareChoice, resetToggles],
  );
}

/**
 * Wires the stream, selection and send hooks and builds the session's remaining commands.
 *
 * @param d - State from {@link useChatSessionData}.
 * @param agent - Specialist-run data from `useChatAgentRuns`.
 * @param options - Conversation id, mode and creation callback.
 * @param setThemeRequest - Receives theme requests streamed from the assistant.
 * @returns `handleSend`, `handleMessageMutation`, `handleStop`, `handleNewChat` and `handleSubmitInput`.
 */
export function useChatSessionCommands(
  d: ChatSessionData,
  agent: ReturnType<typeof useChatAgentRuns>,
  options: ChatSessionOptions,
  setThemeRequest: (mode: 'light' | 'dark' | 'system') => void,
) {
  const { conversationId, mode, onConversationCreated } = options;
  const { session, chatCtrl, live, actions, refs, composer } = d;
  const { setProviderGateOpen, setEditingMessageId, draftStashRef } = composer;
  const { setActiveAgentRunId } = agent;
  const { resetShareChoice } = d.attachments;
  const { resetToggles } = composer;
  const streamResponseRun = useChatStream(refs, actions, setThemeRequest);
  const onSelectionReset = useSelectionReset(d, agent);
  useConversationSelection({
    refs,
    live: actions,
    isPending: live.isPending,
    conversationId,
    activeConversationId: chatCtrl.activeConversationId,
    activeConversationGeneration: chatCtrl.activeConversationGeneration,
    activeResponseId: d.detail.data?.active_response_id,
    streamResponseRun,
    onSelectionReset,
  });
  const openProviderGate = React.useCallback(() => setProviderGateOpen(true), [setProviderGateOpen]);
  /** Leaves edit mode after a successful edit and returns the draft that was stashed when it began (null when none). */
  const clearEditing = React.useCallback(() => {
    const stashed = draftStashRef.current;
    setEditingMessageId(null);
    draftStashRef.current = null;
    return stashed;
  }, [setEditingMessageId, draftStashRef]);
  const send = useChatSend({
    refs,
    live: actions,
    chatCtrl,
    csrfToken: session.csrfToken,
    conversationId,
    mode,
    onConversationCreated,
    messages: d.messages,
    isBusy: d.busy,
    providerReady: d.providerReady,
    openProviderGate,
    modelChoice: d.modelPicker?.value ?? null,
    webSearchRequested: d.webSearch.usable && composer.webSearchOn,
    effectiveEditingMessageId: d.effectiveEditingMessageId,
    clearEditing,
    selectedProfileId: composer.selectedProfileId,
    profiles: agent.profiles.data,
    pendingProfileRun: d.pendingProfileRun,
    attachments: d.attachments,
    setActiveAgentRunId,
    streamResponseRun,
    sourceScope: sourceScopeOf(d.sourceMention),
  });
  const handleStop = useStopCommand(d);
  const activeResponseId = live.activeResponseId;

  const { resetConversation } = chatCtrl;
  const { clear: clearSources } = d.sourceMention;
  /** Starts a clean new conversation thread: orphans in-flight sends, clears live output and per-send toggles. */
  const handleNewChat = React.useCallback(() => {
    refs.bumpSendGeneration();
    resetConversation();
    // Chips belong to the thread they were chosen in; a new chat must start with none.
    clearSources();
    setEditingMessageId(null);
    actions.clearOutput();
    actions.patch({ error: null, cancelled: false, inputRequest: null, isStreaming: false, isPending: false });
    resetToggles();
    resetShareChoice();
  }, [resetConversation, refs, actions, setEditingMessageId, resetToggles, resetShareChoice, clearSources]);

  /** Answers the pending user-input request so the paused run resumes (no new message or run). */
  const handleSubmitInput = React.useCallback(
    async (requestId: string, answer: string): Promise<void> => {
      if (!activeResponseId) throw new Error('No active response');
      await submitResponseInput(activeResponseId, requestId, answer, session.csrfToken);
    },
    [activeResponseId, session.csrfToken],
  );

  const { handleMessageMutation, handleSend } = send;
  const edit = useMessageEditCommands(d, mode, handleMessageMutation);
  const draft = chatCtrl.draft;
  const pendingPrompt = d.pendingProfileRun?.prompt;
  const lastMessage = d.messages.at(-1);
  const { handleRetryMessage } = edit;
  const { refetch: refetchDetail } = d.detail;
  /**
   * Error-banner Retry (C3). A turn the server already stored (the transcript ends at a persisted user message that is
   * the text last sent, so the run failed afterwards) is retried as a new version of that turn, never as a duplicate
   * question. While that turn is still the optimistic bubble nothing is resent: the stored copy is refetched first.
   * Otherwise (a newer send the server never accepted, a specialist prompt, a draft) the last sent text is sent again.
   */
  const retryLast = React.useCallback(() => {
    if (!pendingPrompt && mode === 'full' && lastMessage?.role === 'user') {
      if (lastMessage.id.startsWith('optimistic-')) {
        void refetchDetail();
        return;
      }
      if (!refs.lastSent || refs.lastSent === lastMessage.content) {
        handleRetryMessage(lastMessage);
        return;
      }
    }
    const retryDraft = pendingPrompt ?? refs.lastSent ?? draft;
    if (retryDraft) void handleSend(retryDraft);
  }, [pendingPrompt, mode, lastMessage, handleRetryMessage, refetchDetail, refs, draft, handleSend]);

  return { ...send, ...edit, handleStop, handleNewChat, handleSubmitInput, retryLast };
}

/**
 * Builds the widget-facing prompt handlers of the session (sandbox widgets, gallery, QR pick).
 *
 * @param p.chatCtrl - Chat controller (draft access).
 * @param p.busy - True while a response is pending/streaming.
 * @param p.handleSend - Session send command.
 * @param p.setError - Receives errors of fire-and-forget transcript prompts.
 * @param p.rootRef - This session's root element; the composer is looked up inside it so a drawer and the full page
 * mounted together never steal each other's focus.
 * @returns `onDraftPrompt` (refuses to overwrite typed text), `onSendPrompt` (rejects while busy or on failure) and the
 * fire-and-forget `onTranscriptSendPrompt` that surfaces rejections as the session error banner.
 */
export function useWidgetPromptHandlers(p: {
  chatCtrl: ReturnType<typeof useChatController>;
  busy: boolean;
  handleSend: (content: string) => Promise<boolean>;
  setError: (message: string) => void;
  rootRef: React.RefObject<HTMLElement | null>;
}) {
  const t = useTranslations('chat');
  const { chatCtrl, busy, handleSend, setError, rootRef } = p;
  /**
   * Places a widget prompt in the composer and moves focus there so the owner can review and send it. Throws a localized
   * error (reported to the widget as retryable and shown by the bridge) when the owner already typed.
   */
  const onDraftPrompt = React.useCallback(
    (prompt: string) => {
      if (chatCtrl.draft.trim()) throw new Error(t('genUi.prompt.composerBusy'));
      chatCtrl.setDraft(prompt);
      // Wait one tick so the textarea already shows the drafted text when it takes focus.
      setTimeout(() => {
        const input = rootRef.current?.querySelector<HTMLTextAreaElement>('textarea[data-chat-composer-input]');
        input?.focus();
        input?.scrollIntoView({ block: 'nearest' });
      }, 0);
    },
    [chatCtrl, t, rootRef],
  );
  /** Submits a widget follow-up; rejects with a localized error while busy so the bridge reports a retryable failure. */
  const onSendPrompt = React.useCallback(
    (prompt: string): Promise<void> => settleWidgetPrompt(busy, () => handleSend(prompt), t('genUi.prompt.busy'), t('genUi.prompt.failed')),
    [handleSend, busy, t],
  );
  const onTranscriptSendPrompt = React.useCallback(
    (prompt: string) => {
      onSendPrompt(prompt).catch((err: unknown) => setError(err instanceof Error ? err.message : t('genUi.prompt.busy')));
    },
    [onSendPrompt, setError, t],
  );
  return { onDraftPrompt, onSendPrompt, onTranscriptSendPrompt };
}
