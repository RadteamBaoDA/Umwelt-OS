import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { cancelResponse, chatKeys, getConversation, submitResponseInput, type ChatMessage } from './api';
import { settleWidgetPrompt } from './generative-ui/bridge/widget-prompt';
import { useChatAttachments } from './chat-attachments';
import { useChatLiveState } from './chat-live-state';
import { useChatSessionRefs } from './chat-session-refs';
import { useChatModelPicker } from './model-picker';
import { useChatProviderReady } from './provider-gate';
import { useChatAgentRuns } from './use-chat-agent-runs';
import { useChatComposerState } from './use-chat-composer-state';
import { useChatSend } from './use-chat-send';
import { useChatStream } from './use-chat-stream';
import { useChatWebSearch } from './use-chat-web-search';
import { useConversationSelection } from './use-conversation-selection';

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
  const pendingProfileRun = conversationId ? chatCtrl.pendingSpecialistRuns[conversationId] ?? null : null;
  const pendingMessageMutation = conversationId ? chatCtrl.pendingMessageMutations[conversationId] ?? null : null;
  const effectiveEditingMessageId = composer.editingMessageId
    ?? (pendingMessageMutation?.action === 'edit' ? pendingMessageMutation.targetMessageId : null);
  // Fetch full conversation details if conversationId is defined
  const detail = useQuery({
    queryKey: chatKeys.conversation(conversationId ?? ''),
    queryFn: () => getConversation(conversationId!),
    enabled: Boolean(conversationId),
  });
  const messages: ChatMessage[] = React.useMemo(() => detail.data?.messages ?? [], [detail.data?.messages]);
  const latestAssistant = React.useMemo(() => [...messages].reverse().find((message) => message.role === 'assistant'), [messages]);
  // Specialist-agent sends (non-assistant profile or a pending profile retry) do not carry web_search or attachments.
  const profileBlocked = composer.selectedProfileId !== 'assistant' || Boolean(pendingProfileRun);
  const attachmentsUnavailable = areAttachmentsUnavailable(profileBlocked, effectiveEditingMessageId, chatCtrl.context);
  return {
    session, chatCtrl, live, actions, refs, composer, detail, messages, latestAssistant,
    pendingProfileRun, pendingMessageMutation, effectiveEditingMessageId, profileBlocked, attachmentsUnavailable,
    busy: live.isPending || live.isStreaming,
    // Plain Q&A (Assistant, no run pending) shows no agent or task chrome; only a specialist selection does.
    agentMode: mode === 'full' && profileBlocked,
    dbQueryEffective: composer.dbQueryOn || chatCtrl.context?.kind === 'gadget',
    modelPicker: useChatModelPicker(),
    providerReady: useChatProviderReady(),
    webSearch: useChatWebSearch(mode, profileBlocked),
    attachments: useChatAttachments(session.csrfToken, attachmentsUnavailable),
  };
}

/** Return type of {@link useChatSessionData}. */
export type ChatSessionData = ReturnType<typeof useChatSessionData>;

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
  const { setProviderGateOpen, setEditingMessageId } = composer;
  const { setActiveAgentRunId } = agent;
  const streamResponseRun = useChatStream(refs, actions, setThemeRequest);
  const onSelectionReset = React.useCallback(() => { setActiveAgentRunId(null); setEditingMessageId(null); }, [setActiveAgentRunId, setEditingMessageId]);
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
  const clearEditing = React.useCallback(() => setEditingMessageId(null), [setEditingMessageId]);
  const send = useChatSend({
    refs, live: actions, chatCtrl, csrfToken: session.csrfToken, conversationId, mode, onConversationCreated, messages: d.messages,
    isBusy: d.busy, providerReady: d.providerReady, openProviderGate,
    modelChoice: d.modelPicker?.value ?? null, webSearchRequested: d.webSearch.usable && composer.webSearchOn,
    dbQueryEffective: d.dbQueryEffective, resetToggles: composer.resetToggles, effectiveEditingMessageId: d.effectiveEditingMessageId,
    clearEditing, selectedProfileId: composer.selectedProfileId, profiles: agent.profiles.data,
    pendingProfileRun: d.pendingProfileRun, attachments: d.attachments, setActiveAgentRunId, streamResponseRun,
  });
  const { refetch: refetchDetail } = d.detail;
  const activeResponseId = live.activeResponseId;

  /** Explicitly cancels active response generation without losing transcript state. */
  const handleStop = React.useCallback(async () => {
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
    actions.patch({ isStreaming: false, isPending: false, activeResponseId: null, error: null, cancelled: true });
    void refetchDetail();
  }, [activeResponseId, refetchDetail, session.csrfToken, refs, actions]);

  const { resetConversation } = chatCtrl;
  const { resetToggles } = composer;
  /** Starts a clean new conversation thread: orphans in-flight sends, clears live output and per-send toggles. */
  const handleNewChat = React.useCallback(() => {
    refs.bumpSendGeneration();
    resetConversation();
    setEditingMessageId(null);
    actions.clearOutput();
    actions.patch({ error: null, cancelled: false, inputRequest: null, isStreaming: false, isPending: false });
    resetToggles();
  }, [resetConversation, refs, actions, setEditingMessageId, resetToggles]);

  /** Answers the pending user-input request so the paused run resumes (no new message or run). */
  const handleSubmitInput = React.useCallback(async (requestId: string, answer: string): Promise<void> => {
    if (!activeResponseId) throw new Error('No active response');
    await submitResponseInput(activeResponseId, requestId, answer, session.csrfToken);
  }, [activeResponseId, session.csrfToken]);

  const { handleMessageMutation, handleSend } = send;
  const { setDraft } = chatCtrl;
  /** Opens the full Chat composer with a user message's content for an append-only edit. */
  const handleEditMessage = React.useCallback((message: ChatMessage) => {
    if (mode !== 'full' || message.role !== 'user') return;
    setEditingMessageId(message.id);
    setDraft(message.content);
  }, [mode, setEditingMessageId, setDraft]);
  /** Reuses the parent assistant message's captured context for a new answer branch. */
  const handleRegenerateMessage = React.useCallback((message: ChatMessage) => {
    if (mode === 'full' && message.role === 'assistant') void handleMessageMutation('regenerate', message.id);
  }, [handleMessageMutation, mode]);
  const draft = chatCtrl.draft;
  const pendingPrompt = d.pendingProfileRun?.prompt;
  /** Resends the pending specialist prompt, the last sent content, or the draft. */
  const retryLast = React.useCallback(() => {
    const retryDraft = pendingPrompt ?? refs.lastSent ?? draft;
    if (retryDraft) void handleSend(retryDraft);
  }, [pendingPrompt, refs, draft, handleSend]);

  return { ...send, handleStop, handleNewChat, handleSubmitInput, handleEditMessage, handleRegenerateMessage, retryLast };
}

/**
 * Builds the widget-facing prompt handlers of the session (sandbox widgets, gallery, QR pick).
 *
 * @param p.chatCtrl - Chat controller (draft access).
 * @param p.busy - True while a response is pending/streaming.
 * @param p.handleSend - Session send command.
 * @param p.setError - Receives errors of fire-and-forget transcript prompts.
 * @returns `onDraftPrompt` (refuses to overwrite typed text), `onSendPrompt` (rejects while busy or on failure) and the
 * fire-and-forget `onTranscriptSendPrompt` that surfaces rejections as the session error banner.
 */
export function useWidgetPromptHandlers(p: {
  chatCtrl: ReturnType<typeof useChatController>;
  busy: boolean;
  handleSend: (content: string) => Promise<boolean>;
  setError: (message: string) => void;
}) {
  const t = useTranslations('chat');
  const { chatCtrl, busy, handleSend, setError } = p;
  /** Places a widget prompt in the composer; throws (reported to the widget as retryable) when the owner already typed. */
  const onDraftPrompt = React.useCallback((prompt: string) => {
    if (chatCtrl.draft.trim()) throw new Error('Composer already has text');
    chatCtrl.setDraft(prompt);
  }, [chatCtrl]);
  /** Submits a widget follow-up; rejects with a localized error while busy so the bridge reports a retryable failure. */
  const onSendPrompt = React.useCallback(
    (prompt: string): Promise<void> => settleWidgetPrompt(busy, () => handleSend(prompt), t('genUiPromptBusy'), t('genUiPromptFailed')),
    [handleSend, busy, t],
  );
  const onTranscriptSendPrompt = React.useCallback((prompt: string) => {
    onSendPrompt(prompt).catch((err: unknown) => setError(err instanceof Error ? err.message : t('genUiPromptBusy')));
  }, [onSendPrompt, setError, t]);
  return { onDraftPrompt, onSendPrompt, onTranscriptSendPrompt };
}
