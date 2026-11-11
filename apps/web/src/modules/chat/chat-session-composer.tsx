'use client';

import * as React from 'react';
import dynamic from 'next/dynamic';
import { useTranslations } from 'next-intl';
import { useDisplayPreferences } from '@/core/query-provider';
import { WEB_SEARCH_SEND_ENABLED } from './api';
import { ATTACHMENT_STATUS_ID, ChatAttachmentBar } from './chat-attachments';
import { AgentPicker } from './chat-agent-picker';
import { ChatComposer } from './chat-composer';
import { ChatContextBar } from './chat-context-bar';
import { WEB_SEARCH_PROVIDER_LABELS } from './use-chat-web-search';
import type { AgentProfile } from '@/modules/agents';
import type { ChatSessionData } from './use-chat-session';

// Template library (dialog, seed templates, store) loads only after the owner first opens it from the + menu.
const TemplateLibrary = dynamic(() => import('./generative-ui/templates/template-library').then((m) => m.TemplateLibrary), { ssr: false });

/** Props of {@link SessionComposer}. */
export interface SessionComposerProps {
  data: ChatSessionData;
  /** Agent profiles, loaded lazily (undefined until the picker opens or a specialist is selected). */
  profiles?: AgentProfile[];
  mode: 'drawer' | 'full';
  onSend: (content: string) => Promise<boolean>;
  onStop: () => Promise<void>;
  /** Leaves edit mode and restores the stashed draft. */
  onCancelEdit: () => void;
}

/**
 * Builds the web-search toggle model for the composer (full mode with web search enabled in this build only).
 *
 * @returns The toggle props, or undefined when the control must not render.
 */
export function useWebSearchToggle(data: ChatSessionData, full: boolean) {
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
    onTemplates: () => composer.setTemplatesOpen(true),
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

/** Full-mode only composer slots: context badges, file attachments, the + menu and the send block reason. */
function buildFullModeExtras(data: ChatSessionData, attachmentsBlocked: boolean, profiles: AgentProfile[] | undefined) {
  const { attachments, composer } = data;
  return {
    contextBar: <SessionContextBadges data={data} />,
    onAttachFiles: (files: File[]) => attachments.add(files),
    attachments: <ChatAttachmentBar state={attachments} disabled={attachmentsBlocked} inputRef={composer.fileInputRef} />,
    menu: buildComposerMenu(data, attachmentsBlocked),
    sendBlockedBy: attachments.sendBlock ? ATTACHMENT_STATUS_ID : null,
    agentPicker: buildAgentPicker(data, profiles),
    modelLocked: data.agentMode,
  };
}

/**
 * Agent picker slot of the full composer (C9): shows a pending retry's profile and locks while that retry is pending.
 *
 * @returns The picker element.
 */
function buildAgentPicker(data: ChatSessionData, profiles: AgentProfile[] | undefined) {
  const { composer, pendingProfileRun } = data;
  return (
    <AgentPicker
      value={pendingProfileRun?.profileId ?? composer.selectedProfileId}
      disabled={Boolean(pendingProfileRun) || data.busy}
      onChange={composer.setSelectedProfileId}
      profiles={profiles}
      onOpen={composer.openAgentPicker}
    />
  );
}

/**
 * Template library dialog of the full composer: mounted at the first open, then kept so closing animates and state persists.
 *
 * @param props.data - Session data (open state, CSRF token, conversation).
 * @param props.onSend - Session send command; "Use template" sends its prompt through it.
 */
function SessionTemplates({ data, onSend }: { data: ChatSessionData; onSend: (content: string) => Promise<boolean> }) {
  const { composer, chatCtrl, session } = data;
  const display = useDisplayPreferences();
  const [everOpened, setEverOpened] = React.useState(composer.templatesOpen);
  React.useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- latches the first open so the dialog chunk loads once
    if (composer.templatesOpen) setEverOpened(true);
  }, [composer.templatesOpen]);
  if (!everOpened) return null;
  return (
    <TemplateLibrary
      open={composer.templatesOpen}
      onOpenChange={composer.setTemplatesOpen}
      onSendPrompt={(prompt) => void onSend(prompt)}
      locale={display.confirmedPreferences?.locale}
      csrfToken={session.csrfToken}
      conversationId={chatCtrl.activeConversationId}
    />
  );
}

/**
 * Bottom composer of the chat surface wired to the session state: context badges, attachments, + menu and tool toggles.
 *
 * @param props - See {@link SessionComposerProps}.
 */
export function SessionComposer({ data, profiles, mode, onSend, onStop, onCancelEdit }: SessionComposerProps) {
  const { attachments, live } = data;
  const full = mode === 'full';
  const attachmentsBlocked = data.busy || data.attachmentsUnavailable;
  const webSearch = useWebSearchToggle(data, full);
  const fullExtras = full ? buildFullModeExtras(data, attachmentsBlocked, profiles) : undefined;
  return (
    <>
      <ChatComposer
        mode={mode}
        {...draftBinding(data)}
        onSend={onSend}
        onStop={onStop}
        editing={full && data.effectiveEditingMessageId && !data.pendingMessageMutation ? { onCancel: onCancelEdit } : undefined}
        attachmentBusy={attachmentsBlocked || attachments.full}
        isStreaming={live.isStreaming}
        disabled={live.isPending}
        {...modelBinding(data)}
        {...fullExtras}
        webSearch={webSearch}
        autoFocusKey={full ? (data.chatCtrl.activeConversationId ?? '') : undefined}
      />
      {full && <SessionTemplates data={data} onSend={onSend} />}
    </>
  );
}
