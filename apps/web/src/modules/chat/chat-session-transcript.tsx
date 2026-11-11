'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { ChatTranscript, type EmptyCapabilities } from './chat-transcript';
import { useWebSearchToggle } from './chat-session-composer';
import type { ChatSessionData, useChatSessionCommands, useWidgetPromptHandlers } from './use-chat-session';

/** Props of {@link SessionTranscript}. */
export interface SessionTranscriptProps {
  data: ChatSessionData;
  cmd: ReturnType<typeof useChatSessionCommands>;
  widget: ReturnType<typeof useWidgetPromptHandlers>;
  conversationId?: string | null;
  mode: 'drawer' | 'full';
}

/**
 * Builds the empty-state capability chips from the same state as the composer's + menu (full mode only). A chip is
 * omitted exactly when its composer control is, and carries the same disabled reason.
 *
 * @param data - Session data (toggles, attachment state, hidden file input).
 * @param full - Whether the full Chat page is hosting the transcript.
 * @returns The chip models, or undefined outside full mode.
 */
function useEmptyCapabilities(data: ChatSessionData, full: boolean): EmptyCapabilities | undefined {
  const t = useTranslations('chat');
  const web = useWebSearchToggle(data, full);
  if (!full) return undefined;
  const attachBlocked = data.busy || data.attachmentsUnavailable || data.attachments.full;
  const webReason = web && !web.available ? t('webSearchNotSetUp') : web?.profileBlocked ? t('webSearchProfileOnly') : undefined;
  return {
    providerMissing: !data.providerReady,
    webSearch: web && {
      pressed: web.available && !web.profileBlocked && web.enabled,
      disabledReason: webReason,
      // Not set up: the chip becomes a link to the page that fixes it (profile restrictions have no such fix).
      href: web.available || web.profileBlocked ? undefined : '/settings/web-search',
      disabled: data.busy,
      onActivate: () => web.onChange(!web.enabled),
    },
    attach: {
      disabledReason: data.attachmentsUnavailable ? t('attachmentUnavailableBlock') : undefined,
      disabled: attachBlocked,
      onActivate: () => data.composer.fileInputRef.current?.click(),
    },
  };
}

/**
 * Transcript area wired to the session: committed messages plus the live streaming state. Edit and regenerate are
 * offered in full mode only (the quick drawer does not expose those advanced message actions).
 *
 * @param props - See {@link SessionTranscriptProps}.
 */
export function SessionTranscript({ data, cmd, widget, conversationId, mode }: SessionTranscriptProps) {
  const { live } = data;
  const full = mode === 'full';
  const capabilities = useEmptyCapabilities(data, full);
  return (
    <ChatTranscript
      conversationId={conversationId}
      csrfToken={data.session.csrfToken}
      mode={mode}
      messages={data.messages}
      loading={data.detail.isLoading}
      editingMessageId={full && !data.pendingMessageMutation ? data.effectiveEditingMessageId : null}
      streamingText={live.streamingText}
      streamingReasoning={live.streamingReasoning}
      cancelled={live.cancelled}
      stoppedResponseId={live.stoppedResponseId}
      onRetryMessage={full ? cmd.handleRetryMessage : undefined}
      streamingCitations={live.streamingCitations}
      streamingWebSearch={live.streamingWebSearch}
      activeTool={live.activeTool}
      streamingUi={live.streamingUi}
      steps={live.steps}
      isStreaming={live.isStreaming}
      isPending={live.isPending}
      error={live.error}
      onEditMessage={full ? cmd.handleEditMessage : undefined}
      onRegenerateMessage={full ? cmd.handleRegenerateMessage : undefined}
      capabilities={capabilities}
      onSendPrompt={widget.onTranscriptSendPrompt}
      onWidgetSendPrompt={widget.onSendPrompt}
      onRetry={cmd.retryLast}
    />
  );
}
