'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { ChatSession } from '@/modules/chat/chat-session';
import { ChatHistory } from '@/modules/chat/chat-history';
import { useConversationActions } from '@/modules/chat/conversation-actions';
import { Button } from '@/components/ui/button';
import { PanelLeftOpen, Plus } from 'lucide-react';
import { useChatController } from '@/core/app-shell/chat-controller';
import { ChatSideRail, ChatSourcesSheet } from '@/modules/chat/chat-side-rail';
import { chatKeys, getConversation, type Citation } from '@/modules/chat/api';
import { useQuery } from '@tanstack/react-query';

/**
 * Inner Chat page content consuming ChatController and filling the WorkspaceShell's scrollable main region.
 *
 * @returns Responsive chat workspace with independently collapsible history and evidence sidebars.
 */
function ChatPageContent() {
  const t = useTranslations('chat');
  const chatCtrl = useChatController();
  const [mobileView, setMobileView] = React.useState<'history' | 'chat'>('chat');
  const [historyCollapsed, setHistoryCollapsed] = React.useState(true);

  const handleSelectConversation = React.useCallback(
    (id: string) => {
      chatCtrl.selectConversation(id);
      setMobileView('chat');
    },
    [chatCtrl],
  );

  const handleNewConversation = React.useCallback(() => {
    chatCtrl.resetConversation();
    setMobileView('chat');
  }, [chatCtrl]);

  // Rename/delete dialogs live here (not in the history list) so the header actions work with history collapsed.
  const conversationActions = useConversationActions(chatCtrl.activeConversationId, handleNewConversation);

  // Same query key as ChatSession, so this reads the cached transcript without another request.
  const { data: detail } = useQuery({
    queryKey: chatKeys.conversation(chatCtrl.activeConversationId ?? ''),
    queryFn: () => getConversation(chatCtrl.activeConversationId!),
    enabled: Boolean(chatCtrl.activeConversationId),
  });
  const citations = React.useMemo(() => {
    const seen = new Set<string>();
    const unique: Citation[] = [];
    for (const message of detail?.messages ?? []) for (const citation of message.citations ?? []) {
      const key = citation.sourceType === 'web' ? citation.url : `${citation.documentVersionId}:${citation.chunkId}`;
      if (!seen.has(key)) { seen.add(key); unique.push(citation); }
    }
    return unique;
  }, [detail?.messages]);

  return (
    <div className="flex min-h-0 flex-1 shrink! basis-0 w-full min-w-0 rounded-[18px] border border-border bg-surface overflow-hidden">
      {/* Sidebar history on desktop; toggleable on mobile */}
      <div
        id="chat-history-sidebar"
        className={`${
          mobileView === 'history' ? 'flex w-full' : historyCollapsed ? 'hidden md:flex md:w-12' : 'hidden md:flex md:w-[260px]'
        } shrink-0 border-r border-border h-full`}
      >
        {historyCollapsed && mobileView !== 'history' ? (
          <div className="mx-auto flex flex-col">
            <Button type="button" variant="ghost" size="icon" className="size-11 shrink-0" onClick={handleNewConversation} aria-label={t('newChat')}>
              <Plus aria-hidden="true" />
            </Button>
            <Button type="button" variant="ghost" size="icon" className="size-11 shrink-0" onClick={() => setHistoryCollapsed(false)} aria-label={t('expandHistory')} aria-expanded="false" aria-controls="chat-history-sidebar">
              <PanelLeftOpen aria-hidden="true" />
            </Button>
          </div>
        ) : (
          <ChatHistory
            activeConversationId={chatCtrl.activeConversationId}
            onSelectConversation={handleSelectConversation}
            onNewConversation={handleNewConversation}
            className="w-full"
            actions={conversationActions}
            onCollapse={mobileView === 'history' ? undefined : () => setHistoryCollapsed(true)}
          />
        )}
      </div>

      {/* Main chat session area */}
      <div
        className={`${
          mobileView === 'chat' ? 'flex' : 'hidden md:flex'
        } flex-1 min-w-0 flex-col h-full`}
      >
        {/* Mobile view switch header */}
        <div className="lg:hidden flex items-center justify-between gap-2 p-2 border-b border-border bg-background">
          <Button
            type="button"
            variant="outline"
            onClick={() => setMobileView('history')}
            className="md:hidden min-h-11 px-3 py-1.5 text-xs font-semibold"
          >
            ← {t('history')}
          </Button>
          <span className="min-w-0 flex-1 text-xs font-semibold text-muted-foreground truncate">
            {chatCtrl.activeConversationId ? t('activeConversation') : t('newChat')}
          </span>
          <ChatSourcesSheet citations={citations} context={chatCtrl.context} />
        </div>

        <div className="flex-1 min-h-0 flex flex-col">
          <ChatSession
            conversationId={chatCtrl.activeConversationId}
            onConversationCreated={(id) => chatCtrl.setActiveConversationId(id)}
            mode="full"
            onRenameConversation={chatCtrl.activeConversationId
              ? () => conversationActions.rename({ id: chatCtrl.activeConversationId!, title: detail?.title ?? '' })
              : undefined}
            onDeleteConversation={chatCtrl.activeConversationId
              ? () => conversationActions.remove(chatCtrl.activeConversationId!)
              : undefined}
          />
        </div>
      </div>

      <ChatSideRail citations={citations} context={chatCtrl.context} />
      {conversationActions.dialogs}
    </div>
  );
}

/**
 * Full Chat page route (/chat) providing comprehensive conversation history,
 * search, grounded evidence inspection, and shared ChatSession execution.
 *
 * @returns Full Chat page element wrapped within WorkspaceShell.
 */
export default function ChatPage() {
  return (
    <WorkspaceShell>
      <ChatPageContent />
    </WorkspaceShell>
  );
}
