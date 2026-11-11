'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { AssistantThemeBanner } from './assistant-theme-banner';
import { TodosPanel } from './todos/todos-panel';
import { ChatContextBar } from './chat-context-bar';
import { PromptBridgeNotice, PromptBridgeProvider } from './generative-ui/bridge';
import { UserInputRequest } from './generative-ui/components/user-input-request';
import { ProviderGateAlert } from './provider-gate';
import { ChatSessionHeader } from './chat-session-header';
import { SessionComposer } from './chat-session-composer';
import { SessionTranscript } from './chat-session-transcript';
import { AgentPanels, RevisionBanners } from './chat-session-panels';
import { useChatAgentRuns } from './use-chat-agent-runs';
import { useStopOnEscape } from './use-chat-shortcuts';
import { ChatDropZone } from './chat-drop-zone';
import { useConversationExport } from './conversation-export';
import { useChatSessionCommands, useChatSessionData, useWidgetPromptHandlers } from './use-chat-session';

export { MAX_RESUME_ATTEMPTS, RESUME_BACKOFF_MS } from './use-chat-stream';

export interface ChatSessionProps {
  /** Target conversation ID to render, or null/undefined for a fresh unsaved thread. */
  conversationId?: string | null;
  /** Callback invoked when a new conversation is created on the server. */
  onConversationCreated?: (id: string) => void;
  /** Mode indicating whether ChatSession is hosted inside the quick drawer or full /chat route. */
  mode?: 'drawer' | 'full';
  /** Opens the rename dialog for the open conversation (full mode header); omitted when there is no saved conversation. */
  onRenameConversation?: () => void;
  /** Opens the delete confirmation for the open conversation (full mode header). */
  onDeleteConversation?: () => void;
  /** Pins/unpins the open conversation (full mode header menu); omitted when there is no saved conversation. */
  onSetPinned?: (pinned: boolean) => void;
  /** Archives/restores the open conversation (full mode header menu). */
  onSetArchived?: (archived: boolean) => void;
  /** Full-mode header slot before the title (e.g. the mobile History switch), so the page needs no second header row. */
  headerLeading?: React.ReactNode;
  /** Full-mode header slot after the controls (e.g. the Sources sheet trigger on narrow viewports). */
  headerTrailing?: React.ReactNode;
  /** Optional callback to close the drawer presentation when hosted inside ChatDrawer. */
  onCloseDrawer?: () => void;
}

/**
 * Stack between the transcript and the composer: the agent todo list, a pending generative-UI input request and the
 * provider-gate alert. Renders nothing for the parts whose state is absent; no side effects beyond forwarding callbacks.
 */
function SessionInputStack({
  data: d,
  cmd,
  conversationId,
}: {
  data: ReturnType<typeof useChatSessionData>;
  cmd: ReturnType<typeof useChatSessionCommands>;
  conversationId?: string | null;
}) {
  const { live, composer } = d;
  return (
    <>
      <PromptBridgeNotice />
      {d.agentMode && <TodosPanel conversationId={conversationId} isStreaming={live.isStreaming} />}
      {live.inputRequest && (
        <div className="shrink-0 px-4">
          <UserInputRequest
            request={live.inputRequest.request}
            state={live.inputRequest.state}
            answeredLabel={live.inputRequest.label}
            onSubmitInput={cmd.handleSubmitInput}
          />
        </div>
      )}
      {composer.providerGateOpen && !d.providerReady && <ProviderGateAlert onDismiss={() => composer.setProviderGateOpen(false)} />}
    </>
  );
}

/**
 * Shared core chat session component powering both the quick-chat Sheet drawer and the /chat route.
 * Coordinates conversation querying, message sending with client_request_id idempotency,
 * active SSE event stream consumption, citation extraction, cancellation controls, and full-page
 * profile run selection/status/history while keeping the quick drawer's legacy assistant flow.
 * Full Chat appends prompt edits and answer regenerations with stable shared request identities;
 * the quick drawer does not expose these advanced message actions.
 * Specialist retries retain the exact prompt, profile revision, conversation, and request key in the shared
 * Chat controller until acknowledged, including across sidebar changes, drawer remounts, and page navigation.
 * Late acknowledgements refresh their target conversation, while visible state updates require the
 * mounted send and conversation-selection generation that initiated the request.
 *
 * State lives in focused hooks: `useChatLiveState` (streaming/pending/error reducer), `useChatStream` (SSE
 * consumption with resume), `useChatSend` (send and revisions), `useConversationSelection` (selection reset and
 * reattach), `useChatComposerState` (toggles/pickers) and `useChatAgentRuns` (specialist runs).
 *
 * @param props - ChatSessionProps interface.
 * @returns Unified chat session surface.
 */
export function ChatSession({
  conversationId,
  onConversationCreated,
  mode = 'drawer',
  onRenameConversation,
  onDeleteConversation,
  onSetPinned,
  onSetArchived,
  headerLeading,
  headerTrailing,
}: ChatSessionProps) {
  const t = useTranslations('chat');
  const options = { conversationId, mode, onConversationCreated };
  const d = useChatSessionData(options);
  const { session, chatCtrl, live, actions, composer, pendingProfileRun, pendingMessageMutation } = d;
  const [themeRequest, setThemeRequest] = React.useState<'light' | 'dark' | 'system' | null>(null);
  const agent = useChatAgentRuns({ conversationId, mode, agentMode: d.agentMode, csrfToken: session.csrfToken });
  const cmd = useChatSessionCommands(d, agent, options, setThemeRequest);
  const setError = React.useCallback((message: string) => actions.patch({ error: message }), [actions]);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const widget = useWidgetPromptHandlers({ chatCtrl, busy: d.busy, handleSend: cmd.handleSend, setError, rootRef });
  const title = d.detail.data?.title ?? '';
  const handleExport = useConversationExport(title, d.messages, mode === 'full');
  // Esc stops an in-flight generation on the full page (same action as the Stop button).
  useStopOnEscape(rootRef, { enabled: mode === 'full', isStreaming: live.isStreaming, onStop: cmd.handleStop });

  return (
    <PromptBridgeProvider
      onSendPrompt={widget.onSendPrompt}
      onDraftPrompt={widget.onDraftPrompt}
      linkConfirmLabels={{
        title: t('genUiLinkConfirmTitle'),
        description: t('genUiLinkConfirmBody'),
        open: t('genUiLinkConfirmOpen'),
        cancel: t('genUiLinkConfirmCancel'),
      }}
    >
      <ChatDropZone
        rootRef={rootRef}
        enabled={mode === 'full'}
        accepting={d.canAttachFiles}
        onFiles={d.attachments.add}
        className="flex flex-col h-full bg-background relative overflow-hidden"
      >
        <ChatSessionHeader
          mode={mode}
          title={title}
          csrfToken={session.csrfToken}
          headerLeading={headerLeading}
          headerTrailing={headerTrailing}
          onRenameConversation={onRenameConversation}
          onDeleteConversation={onDeleteConversation}
          pinned={d.detail.data?.pinned}
          archived={d.detail.data?.archived}
          onSetPinned={onSetPinned}
          onSetArchived={onSetArchived}
          onExportConversation={handleExport}
          onSendPrompt={widget.onTranscriptSendPrompt}
          onNewChat={cmd.handleNewChat}
          profileValue={pendingProfileRun?.profileId ?? composer.selectedProfileId}
          profilePickerDisabled={Boolean(pendingProfileRun)}
          onProfileChange={composer.setSelectedProfileId}
          profiles={agent.profiles.data}
        />
        <AgentPanels
          conversationId={conversationId}
          csrfToken={session.csrfToken}
          agentMode={d.agentMode}
          pendingProfileRun={pendingProfileRun}
          activeRun={agent.activeAgentRun.data}
          runs={agent.agentRuns.data}
          onCancelRun={() => agent.cancelAgent.mutate()}
          cancelling={agent.cancelAgent.isPending}
          onOpenRun={agent.setActiveAgentRunId}
        />
        {mode === 'drawer' && <ChatContextBar variant="bar" context={chatCtrl.context} onRemove={() => chatCtrl.setContext(null)} />}
        <RevisionBanners
          mode={mode}
          pendingMessageMutation={pendingMessageMutation}
          busy={d.busy}
          onRetryRevision={() =>
            pendingMessageMutation &&
            void cmd.handleMessageMutation(
              pendingMessageMutation.action,
              pendingMessageMutation.targetMessageId,
              pendingMessageMutation.content,
            )
          }
        />
        {themeRequest && (
          <AssistantThemeBanner mode={themeRequest} csrfToken={session.csrfToken} onResolved={() => setThemeRequest(null)} />
        )}

        <SessionTranscript data={d} cmd={cmd} widget={widget} conversationId={conversationId} mode={mode} />

        <SessionInputStack data={d} cmd={cmd} conversationId={conversationId} />

        {/* Bottom composer */}
        <SessionComposer data={d} mode={mode} onSend={cmd.handleSend} onStop={cmd.handleStop} onCancelEdit={cmd.handleCancelEdit} />
      </ChatDropZone>
    </PromptBridgeProvider>
  );
}
