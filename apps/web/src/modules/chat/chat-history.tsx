'use client';

import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { MessageSquareIcon, PlusIcon, SearchIcon, Trash2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  chatKeys,
  deleteConversation,
  listConversations,
  type Conversation,
} from '@/modules/chat/api';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';

export interface ChatHistoryProps {
  /** Identifier of the currently active conversation. */
  activeConversationId: string | null;
  /** Callback invoked when the user selects a conversation from the list. */
  onSelectConversation: (id: string) => void;
  /** Callback invoked when the user triggers creation of a new conversation thread. */
  onNewConversation: () => void;
  /** Optional custom CSS classes. */
  className?: string;
}

/**
 * Conversation sidebar history component displaying past threads, search filtering,
 * thread switching, and deletion with accessible confirmation modal.
 *
 * @param props - ChatHistoryProps interface.
 * @returns Accessible conversation list sidebar.
 */
export function ChatHistory({
  activeConversationId,
  onSelectConversation,
  onNewConversation,
  className = '',
}: ChatHistoryProps) {
  const t = useTranslations('chat');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [searchQuery, setSearchQuery] = React.useState('');
  const [pendingDeleteId, setPendingDeleteId] = React.useState<string | null>(null);

  const { data: conversations = [], isLoading } = useQuery({
    queryKey: chatKeys.conversations(),
    queryFn: () => listConversations(),
  });

  const { unbindConversation } = useChatController();
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConversation(id, session.csrfToken),
    onSuccess: (_, deletedId) => {
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
      // A deleted day conversation must stop being that day's drawer target.
      unbindConversation(deletedId);
      if (activeConversationId === deletedId) {
        onNewConversation();
      }
      setPendingDeleteId(null);
    },
  });

  const filteredConversations = React.useMemo(() => {
    if (!searchQuery.trim()) return conversations;
    const query = searchQuery.toLowerCase();
    return conversations.filter(
      (c) =>
        c.title.toLowerCase().includes(query) ||
        (c.context_kind && c.context_kind.toLowerCase().includes(query)),
    );
  }, [conversations, searchQuery]);

  return (
    <aside
      className={`flex flex-col h-full bg-surface border-r border-border shrink-0 ${className}`}
      aria-label={t('history')}
    >
      <div className="p-3 border-b border-border flex flex-col gap-2">
        <Button
          type="button"
          onClick={onNewConversation}
          className="flex items-center justify-center gap-2 w-full h-10 rounded-xl bg-accent text-white dark:text-zinc-900 font-semibold text-xs shadow-xs"
        >
          <PlusIcon className="size-4" />
          <span>{t('newChat')}</span>
        </Button>

        <div className="relative">
          <SearchIcon className="absolute left-2.5 top-2.5 size-3.5 text-muted-foreground pointer-events-none" />
          <Input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t('searchConversations')}
            className="pl-8 h-8 text-xs bg-background"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-1">
        {isLoading && (
          <div className="p-4 text-xs text-center text-muted-foreground">
            {t('thinking')}
          </div>
        )}

        {!isLoading && filteredConversations.length === 0 && (
          <div className="p-4 text-xs text-center text-muted-foreground">
            {t('noHistory')}
          </div>
        )}

        {filteredConversations.map((conv: Conversation) => {
          const isActive = conv.id === activeConversationId;

          return (
            <div
              key={conv.id}
              className={`group flex items-center justify-between gap-2 p-2 rounded-lg text-xs transition-colors cursor-pointer ${
                isActive
                  ? 'bg-accent/15 text-accent font-semibold border border-accent/20'
                  : 'hover:bg-accent/5 text-foreground'
              }`}
              onClick={() => onSelectConversation(conv.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onSelectConversation(conv.id);
                }
              }}
            >
              <div className="flex items-center gap-2 min-w-0 flex-1">
                <MessageSquareIcon className="size-3.5 shrink-0 opacity-70" />
                <span className="truncate">{conv.title || t('untitledConversation')}</span>
              </div>

              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  setPendingDeleteId(conv.id);
                }}
                className="opacity-0 group-hover:opacity-100 p-1 rounded-md text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-opacity"
                title={t('delete')}
                aria-label={t('delete')}
              >
                <Trash2Icon className="size-3.5" />
              </button>
            </div>
          );
        })}
      </div>

      {/* Confirmation Dialog for Deleting Conversation */}
      <AlertDialog
        open={pendingDeleteId !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDeleteId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('delete')}</AlertDialogTitle>
            <AlertDialogDescription>{t('deleteConfirm')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingDeleteId) {
                  deleteMutation.mutate(pendingDeleteId);
                }
              }}
              className="bg-destructive text-white hover:opacity-90"
            >
              {t('delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </aside>
  );
}
