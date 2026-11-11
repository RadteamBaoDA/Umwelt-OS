'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { MoreHorizontalIcon, PlusIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { PickQrButton } from '@/modules/pick';

export const profileTitleKeys: Record<
  string,
  | 'agentProfileSupervisor'
  | 'agentProfileKnowledge'
  | 'agentProfileResearch'
  | 'agentProfilePersonal'
  | 'agentProfileProject'
  | 'agentProfileNews'
  | 'agentProfilePlanning'
  | 'agentProfileAutomation'
> = {
  supervisor: 'agentProfileSupervisor',
  knowledge: 'agentProfileKnowledge',
  research: 'agentProfileResearch',
  personal: 'agentProfilePersonal',
  project: 'agentProfileProject',
  news: 'agentProfileNews',
  planning: 'agentProfilePlanning',
  automation: 'agentProfileAutomation',
};

/** Props of {@link ChatSessionHeader}. */
export interface ChatSessionHeaderProps {
  mode: 'drawer' | 'full';
  title: string;
  csrfToken: string;
  headerLeading?: React.ReactNode;
  headerTrailing?: React.ReactNode;
  onRenameConversation?: () => void;
  onDeleteConversation?: () => void;
  /** Downloads the loaded transcript as Markdown (full mode); omitted when there is nothing to export. */
  onExportConversation?: () => void;
  /** Current pin/archive state of the open conversation; with the setters below they add Pin and Archive to the More menu. */
  pinned?: boolean;
  archived?: boolean;
  onSetPinned?: (pinned: boolean) => void;
  onSetArchived?: (archived: boolean) => void;
  /** Localized failure of the last pin/archive request for this conversation; shown here because the history rail may be collapsed. */
  actionError?: string | null;
  onSendPrompt: (prompt: string) => void;
  onNewChat: () => void;
}

type ControlProps = Pick<
  ChatSessionHeaderProps,
  'onRenameConversation' | 'onExportConversation' | 'onDeleteConversation' | 'pinned' | 'archived' | 'onSetPinned' | 'onSetArchived'
>;

/**
 * "More" menu holding every conversation action at every width: pin, archive, rename, export and delete.
 *
 * @param props - Callbacks and the pin/archive state; an absent callback hides its item.
 * @returns The menu, or null when no callback was given (new, unsaved chat).
 */
function MoreMenu({
  onRenameConversation,
  onExportConversation,
  onDeleteConversation,
  pinned,
  archived,
  onSetPinned,
  onSetArchived,
}: ControlProps) {
  const t = useTranslations('chat');
  if (![onRenameConversation, onExportConversation, onDeleteConversation, onSetPinned, onSetArchived].some(Boolean)) return null;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="icon" aria-label={t('moreActions')} title={t('moreActions')}>
          <MoreHorizontalIcon className="size-4" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {onRenameConversation && <DropdownMenuItem onSelect={onRenameConversation}>{t('renameConversation')}</DropdownMenuItem>}
        {onSetPinned && (
          <DropdownMenuItem onSelect={() => onSetPinned(!pinned)}>{t(pinned ? 'unpinConversation' : 'pinConversation')}</DropdownMenuItem>
        )}
        {onSetArchived && (
          <DropdownMenuItem onSelect={() => onSetArchived(!archived)}>
            {t(archived ? 'unarchiveConversation' : 'archiveConversation')}
          </DropdownMenuItem>
        )}
        {onExportConversation && <DropdownMenuItem onSelect={onExportConversation}>{t('exportMarkdown')}</DropdownMenuItem>}
        {onDeleteConversation && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={onDeleteConversation}>
              {t('delete')}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Session header shared by the quick drawer and the /chat route: title, QR pick, one "More" menu (rename, export, pin, archive, delete), new chat (the agent picker lives in the composer).
 *
 * @param props - See {@link ChatSessionHeaderProps}.
 * @returns The header row (drawer mode reserves room for the sheet close button).
 */
export function ChatSessionHeader(props: ChatSessionHeaderProps) {
  const {
    mode,
    title,
    csrfToken,
    headerLeading,
    headerTrailing,
    onRenameConversation,
    onExportConversation,
    onDeleteConversation,
    pinned,
    archived,
    onSetPinned,
    onSetArchived,
    actionError,
    onSendPrompt,
    onNewChat,
  } = props;
  const t = useTranslations('chat');
  const full = mode === 'full';
  return (
    <div
      className={`flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface shrink-0 ${mode === 'drawer' ? 'px-4 py-3 pr-14' : 'px-3 py-2 sm:px-5 sm:py-3'}`}
    >
      {full && headerLeading}
      {full ? <h1 className="min-w-24 flex-1 truncate text-lg font-semibold text-foreground">{title || t('title')}</h1> : null}
      <div className={`flex shrink-0 items-center gap-2 ${mode === 'drawer' ? 'order-first' : ''}`}>
        {full && <PickQrButton csrfToken={csrfToken} onPrompt={onSendPrompt} />}
        {full && (
          <MoreMenu
            onRenameConversation={onRenameConversation}
            onExportConversation={onExportConversation}
            onDeleteConversation={onDeleteConversation}
            pinned={pinned}
            archived={archived}
            onSetPinned={onSetPinned}
            onSetArchived={onSetArchived}
          />
        )}
        <Button
          type="button"
          onClick={onNewChat}
          className={`secondary inline-flex min-h-9 pointer-coarse:min-h-11 items-center gap-2 px-4 text-sm font-semibold transition-colors ${full ? 'hidden' : ''}`}
          title={t('newChat')}
        >
          <PlusIcon className="size-4" />
          <span>{t('newChat')}</span>
        </Button>
        {full && headerTrailing}
      </div>
      {full && actionError && (
        <p role="alert" className="w-full text-xs text-destructive">
          {actionError}
        </p>
      )}
    </div>
  );
}
