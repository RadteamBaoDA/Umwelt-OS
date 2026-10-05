'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { WorkspaceShell } from '@/core/app-shell/workspace-shell';
import { ChatSession } from '@/modules/chat/chat-session';
import { ChatHistory } from '@/modules/chat/chat-history';
import { useChatController } from '@/core/app-shell/chat-controller';

/**
 * Inner Chat page content consuming ChatController from within the WorkspaceShell tree.
 *
 * @returns Responsive two-column chat workspace.
 */
function ChatPageContent() {
  const t = useTranslations('chat');
  const chatCtrl = useChatController();
  const [mobileView, setMobileView] = React.useState<'history' | 'chat'>('chat');

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

  return (
    <div className="flex h-[calc(100vh-140px)] min-h-[500px] w-full rounded-2xl border border-border bg-surface shadow-xs overflow-hidden">
      {/* Sidebar history on desktop; toggleable on mobile */}
      <div
        className={`${
          mobileView === 'history' ? 'flex w-full' : 'hidden md:flex md:w-64 lg:w-72'
        } shrink-0 border-r border-border h-full`}
      >
        <ChatHistory
          activeConversationId={chatCtrl.activeConversationId}
          onSelectConversation={handleSelectConversation}
          onNewConversation={handleNewConversation}
          className="w-full"
        />
      </div>

      {/* Main chat session area */}
      <div
        className={`${
          mobileView === 'chat' ? 'flex' : 'hidden md:flex'
        } flex-1 min-w-0 flex-col h-full`}
      >
        {/* Mobile view switch header */}
        <div className="md:hidden flex items-center justify-between p-2 border-b border-border bg-background">
          <button
            type="button"
            onClick={() => setMobileView('history')}
            className="px-3 py-1.5 text-xs font-semibold rounded-lg border border-border bg-surface text-foreground"
          >
            ← {t('history')}
          </button>
          <span className="text-xs font-semibold text-muted-foreground truncate max-w-[200px]">
            {chatCtrl.activeConversationId ? t('activeConversation') : t('newChat')}
          </span>
        </div>

        <div className="flex-1 min-h-0 flex flex-col">
          <ChatSession
            conversationId={chatCtrl.activeConversationId}
            onConversationCreated={(id) => chatCtrl.setActiveConversationId(id)}
            mode="full"
          />
        </div>
      </div>
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
