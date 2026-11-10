'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { WEB_SEARCH_SEND_ENABLED } from './api';
import { ATTACHMENT_STATUS_ID, ChatAttachmentBar } from './chat-attachments';
import { ChatComposer } from './chat-composer';
import { ChatContextBar } from './chat-context-bar';
import { WEB_SEARCH_PROVIDER_LABELS } from './use-chat-web-search';
import type { ChatSessionData } from './use-chat-session';

/** Props of {@link SessionComposer}. */
export interface SessionComposerProps {
  data: ChatSessionData;
  mode: 'drawer' | 'full';
  onSend: (content: string) => Promise<boolean>;
  onStop: () => Promise<void>;
}

/**
 * Builds the web-search toggle model for the composer (full mode with web search enabled in this build only).
 *
 * @returns The toggle props, or undefined when the control must not render.
 */
function useWebSearchToggle(data: ChatSessionData, full: boolean) {
  const t = useTranslations('chat');
  if (!full || !WEB_SEARCH_SEND_ENABLED) return undefined;
  return {
    available: data.webSearch.available,
    provider: WEB_SEARCH_PROVIDER_LABELS[data.webSearch.provider ?? ''] ?? t('webSearchProviderGeneric'),
    profileBlocked: data.profileBlocked,
    enabled: data.composer.webSearchOn,
    onChange: data.composer.setWebSearchOn,
  };
}

/**
 * Builds the composer's + menu model (full mode only).
 *
 * @returns Menu props, or undefined outside full mode. Adding context is offered only without a context or while a
 * selection can still grow.
 */
function buildComposerMenu(data: ChatSessionData, attachmentsBlocked: boolean) {
  const { chatCtrl, composer, attachments } = data;
  const canAddContext = !chatCtrl.context || chatCtrl.context.kind === 'selection';
  return {
    onAddContext: canAddContext ? () => composer.setContextPickerOpen(true) : undefined,
    onAttach: () => composer.fileInputRef.current?.click(),
    attachDisabled: attachmentsBlocked || attachments.full,
    share: { checked: attachments.shareWithModel, disabled: attachmentsBlocked, onChange: attachments.setShareWithModel },
  };
}

/** Context badges row of the full-mode composer. */
function SessionContextBadges({ data }: { data: ChatSessionData }) {
  const { chatCtrl, composer } = data;
  return (
    <ChatContextBar
      variant="badges"
      context={chatCtrl.context}
      onRemove={() => chatCtrl.setContext(null)}
      onChange={chatCtrl.setContext}
      pickerOpen={composer.contextPickerOpen}
      onPickerOpenChange={composer.setContextPickerOpen}
    />
  );
}

/** Controlled draft binding: a pending specialist retry pins its own prompt and locks editing. */
function draftBinding({ chatCtrl, pendingProfileRun }: ChatSessionData) {
  return {
    value: pendingProfileRun?.prompt ?? chatCtrl.draft,
    onChange: pendingProfileRun ? () => undefined : chatCtrl.setDraft,
  };
}

/** Model label, picker and the latest answer's source count shown by the composer. */
function modelBinding({ latestAssistant, modelPicker }: ChatSessionData) {
  return {
    modelLabel: latestAssistant?.model_identity ?? null,
    modelPicker,
    sourcesCount: latestAssistant?.citations?.length ?? 0,
  };
}

/** Database-query toggle; specialist profiles never carry `db_query`, so it is hidden for them. */
function dbQueryToggle(data: ChatSessionData) {
  return data.profileBlocked ? undefined : { enabled: data.dbQueryEffective, onChange: data.composer.setDbQueryOn };
}

/** Full-mode only composer slots: context badges, file attachments, the + menu and the send block reason. */
function buildFullModeExtras(data: ChatSessionData, attachmentsBlocked: boolean) {
  const { attachments, composer } = data;
  return {
    contextBar: <SessionContextBadges data={data} />,
    onAttachFiles: (files: File[]) => attachments.add(files),
    attachmentBar: <ChatAttachmentBar state={attachments} disabled={attachmentsBlocked} inputRef={composer.fileInputRef} />,
    menu: buildComposerMenu(data, attachmentsBlocked),
    sendBlockedBy: attachments.sendBlock ? ATTACHMENT_STATUS_ID : null,
  };
}

/**
 * Bottom composer of the chat surface wired to the session state: context badges, attachments, + menu and tool toggles.
 *
 * @param props - See {@link SessionComposerProps}.
 */
export function SessionComposer({ data, mode, onSend, onStop }: SessionComposerProps) {
  const { attachments, live } = data;
  const full = mode === 'full';
  const attachmentsBlocked = data.busy || data.attachmentsUnavailable;
  const webSearch = useWebSearchToggle(data, full);
  const fullExtras = full ? buildFullModeExtras(data, attachmentsBlocked) : undefined;
  return (
    <ChatComposer
      mode={mode}
      contextBar={fullExtras?.contextBar}
      {...draftBinding(data)}
      onSend={onSend}
      onStop={onStop}
      onAttachFiles={fullExtras?.onAttachFiles}
      attachmentBusy={attachmentsBlocked || attachments.full}
      isStreaming={live.isStreaming}
      disabled={live.isPending}
      {...modelBinding(data)}
      attachments={fullExtras?.attachmentBar}
      menu={fullExtras?.menu}
      sendBlockedBy={fullExtras?.sendBlockedBy ?? null}
      webSearch={webSearch}
      dbQuery={dbQueryToggle(data)}
      autoFocusKey={full ? (data.chatCtrl.activeConversationId ?? '') : undefined}
    />
  );
}
