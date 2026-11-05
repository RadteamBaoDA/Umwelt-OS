'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { MoreHorizontalIcon, PlusIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { PickQrButton } from '@/modules/pick';
import type { AgentProfile } from '@/modules/agents';

export const profileTitleKeys: Record<string, 'agentProfileSupervisor' | 'agentProfileKnowledge' | 'agentProfileResearch' | 'agentProfilePersonal' | 'agentProfileProject' | 'agentProfileNews' | 'agentProfilePlanning' | 'agentProfileAutomation'> = {
  supervisor: 'agentProfileSupervisor', knowledge: 'agentProfileKnowledge', research: 'agentProfileResearch',
  personal: 'agentProfilePersonal', project: 'agentProfileProject', news: 'agentProfileNews',
  planning: 'agentProfilePlanning', automation: 'agentProfileAutomation',
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
  onSendPrompt: (prompt: string) => void;
  onNewChat: () => void;
  /** Profile id currently shown in the picker (a pending retry pins its own profile). */
  profileValue: string;
  profilePickerDisabled: boolean;
  onProfileChange: (id: string) => void;
  profiles: AgentProfile[] | undefined;
}

/**
 * Rename/delete controls: inline buttons from `sm` up, one "More" menu below so the header never overflows at 375px.
 *
 * @param props - Rename/delete callbacks (either may be omitted).
 */
function ConversationControls({ onRenameConversation, onDeleteConversation }: Pick<ChatSessionHeaderProps, 'onRenameConversation' | 'onDeleteConversation'>) {
  const t = useTranslations('chat');
  return (
    <>
      {/* sm and up: inline buttons. Below sm they collapse into one "More" menu so the header never overflows at 375px. */}
      <div className="hidden items-center gap-2 sm:flex">
        {onRenameConversation && <Button type="button" variant="outline" onClick={onRenameConversation}>{t('renameConversation')}</Button>}
        {onDeleteConversation && <Button type="button" variant="outline" className="text-destructive" onClick={onDeleteConversation}>{t('delete')}</Button>}
      </div>
      <div className="sm:hidden">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline" size="icon" aria-label={t('moreActions')}>
              <MoreHorizontalIcon className="size-4" aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {onRenameConversation && <DropdownMenuItem onSelect={onRenameConversation}>{t('renameConversation')}</DropdownMenuItem>}
            {onDeleteConversation && <DropdownMenuItem className="text-destructive" onSelect={onDeleteConversation}>{t('delete')}</DropdownMenuItem>}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </>
  );
}

/**
 * Agent profile picker (full mode): the Assistant plus each specialist, disabled when unavailable.
 *
 * @param props - Picker value, disabled flag, change handler and the loaded profiles.
 */
function ProfilePicker({ profileValue, profilePickerDisabled, onProfileChange, profiles }: Pick<ChatSessionHeaderProps, 'profileValue' | 'profilePickerDisabled' | 'onProfileChange' | 'profiles'>) {
  const tAgents = useTranslations('aiSettings');
  return (
    <Select value={profileValue} onValueChange={onProfileChange}>
      <SelectTrigger aria-label={tAgents('agentSelect')} className="w-32 sm:w-40" disabled={profilePickerDisabled}><SelectValue placeholder={tAgents('agentSelect')} /></SelectTrigger>
      <SelectContent>
        <SelectItem value="assistant">{tAgents('agentAssistant')}</SelectItem>
        {(profiles ?? []).map((profile) => (
          <SelectItem key={profile.id} value={profile.id} disabled={!profile.enabled || profile.capability === 'unavailable'}>
            {tAgents(profileTitleKeys[profile.id] ?? 'agentProfileKnowledge')}{profile.capability === 'partial' ? ` · ${tAgents('agentPartial')}` : profile.capability === 'unavailable' ? ` · ${tAgents('agentUnavailable')}` : ''}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/**
 * Session header shared by the quick drawer and the /chat route: title, QR pick, rename/delete, agent picker, new chat.
 *
 * @param props - See {@link ChatSessionHeaderProps}.
 * @returns The header row (drawer mode reserves room for the sheet close button).
 */
export function ChatSessionHeader(props: ChatSessionHeaderProps) {
  const { mode, title, csrfToken, headerLeading, headerTrailing, onRenameConversation, onDeleteConversation, onSendPrompt, onNewChat } = props;
  const t = useTranslations('chat');
  const full = mode === 'full';
  return (
    <div className={`flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface shrink-0 ${mode === 'drawer' ? 'px-4 py-3 pr-14' : 'px-5 py-3'}`}>
      {full && headerLeading}
      {full ? <h1 className="min-w-32 flex-1 truncate text-lg font-semibold text-foreground">{title || t('title')}</h1> : null}
      <div className={`flex shrink-0 items-center gap-2 ${mode === 'drawer' ? 'order-first' : ''}`}>
        {full && <PickQrButton csrfToken={csrfToken} onPrompt={onSendPrompt} />}
        {full && (onRenameConversation || onDeleteConversation) && (
          <ConversationControls onRenameConversation={onRenameConversation} onDeleteConversation={onDeleteConversation} />
        )}
        {full && <ProfilePicker {...props} />}
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
    </div>
  );
}
