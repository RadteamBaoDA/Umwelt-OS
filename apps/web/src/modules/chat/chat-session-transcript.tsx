'use client';

import * as React from 'react';
import { ChatTranscript } from './chat-transcript';
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
 * Transcript area wired to the session: committed messages plus the live streaming state. Edit and regenerate are
 * offered in full mode only (the quick drawer does not expose those advanced message actions).
 *
 * @param props - See {@link SessionTranscriptProps}.
 */
export function SessionTranscript({ data, cmd, widget, conversationId, mode }: SessionTranscriptProps) {
  const { live } = data;
  const full = mode === 'full';
  return (
    <ChatTranscript
      conversationId={conversationId}
      csrfToken={data.session.csrfToken}
      mode={mode}
      messages={data.messages}
      streamingText={live.streamingText}
      streamingReasoning={live.streamingReasoning}
      cancelled={live.cancelled}
      streamingCitations={live.streamingCitations}
      streamingWebSearch={live.streamingWebSearch}
      activeTool={live.activeTool}
      streamingUi={live.streamingUi}
      isStreaming={live.isStreaming}
      isPending={live.isPending}
      error={live.error}
      onEditMessage={full ? cmd.handleEditMessage : undefined}
      onRegenerateMessage={full ? cmd.handleRegenerateMessage : undefined}
      onSendPrompt={widget.onTranscriptSendPrompt}
      onWidgetSendPrompt={widget.onSendPrompt}
      onRetry={cmd.retryLast}
    />
  );
}
