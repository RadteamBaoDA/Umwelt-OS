'use client';

import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { MessageSquareIcon, PencilIcon, PlusIcon, SearchIcon, Trash2Icon } from 'lucide-react';
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  chatKeys,
  deleteConversation,
  listConversations,
  patchConversation,
  type Conversation,
} from '@/modules/chat/api';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime } from '@/core/i18n';

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
 * Conversation sidebar history component displaying past threads grouped by day, search filtering,
 * thread switching, rename, and deletion with accessible confirmation modal.
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
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';
  const [searchQuery, setSearchQuery] = React.useState('');
  const [pendingDeleteId, setPendingDeleteId] = React.useState<string | null>(null);
  const [pendingRename, setPendingRename] = React.useState<Conversation | null>(null);
  const [renameValue, setRenameValue] = React.useState('');

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
  const renameMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => patchConversation(id, { title }, session.csrfToken),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversations() });
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversation(updated.id) });
      setPendingRename(null);
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

  // Grouping uses the viewer's local calendar day; the server already orders newest first.
  const groupedConversations = React.useMemo(() => {
    const today = new Date().toDateString();
    const groups = { today: [] as Conversation[], earlier: [] as Conversation[] };
    for (const conv of filteredConversations) {
      (new Date(conv.updated_at).toDateString() === today ? groups.today : groups.earlier).push(conv);
    }
    return (['today', 'earlier'] as const).filter((key) => groups[key].length).map((key) => ({ key, items: groups[key] }));
  }, [filteredConversations]);

  return (
    <aside
      className={`flex flex-col h-full bg-surface border-r border-border shrink-0 ${className}`}
      aria-label={t('history')}
    >
      <div className="p-3 border-b border-border flex flex-col gap-2">
        <Button
          type="button"
          onClick={onNewConversation}
          className="flex h-10 w-full items-center justify-center gap-2 rounded-xl text-xs font-semibold shadow-xs"
        >
          <PlusIcon className="size-4" aria-hidden="true" />
          <span>{t('newChat')}</span>
        </Button>

        <div className="relative">
          <SearchIcon className="absolute left-2.5 top-2.5 size-3.5 text-muted-foreground pointer-events-none" aria-hidden="true" />
          <Input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t('searchConversations')}
            aria-label={t('searchConversations')}
            className="pl-8 h-8 text-xs bg-background"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-1">
        {isLoading && (
          <div role="status" className="p-4 text-xs text-center text-muted-foreground">
            {t('thinking')}
          </div>
        )}

        {!isLoading && filteredConversations.length === 0 && (
          <div className="p-4 text-xs text-center text-muted-foreground">
            {t('noHistory')}
          </div>
        )}

        {groupedConversations.map(({ key, items }) => (
          <section key={key} aria-label={key === 'today' ? t('historyToday') : t('historyEarlier')} className="flex flex-col gap-1">
            <h3 className="px-2 pt-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              {key === 'today' ? t('historyToday') : t('historyEarlier')}
            </h3>
            {items.map((conv: Conversation) => {
              const isActive = conv.id === activeConversationId;
              return (
                <div
                  key={conv.id}
                  className={`group flex items-center gap-1 rounded-lg border text-xs ${isActive ? 'border-border bg-secondary font-semibold text-foreground' : 'border-transparent text-foreground hover:bg-secondary'}`}
                >
                  <button
                    type="button"
                    onClick={() => onSelectConversation(conv.id)}
                    aria-current={isActive ? 'true' : undefined}
                    className="flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-lg p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <MessageSquareIcon className="size-3.5 shrink-0 opacity-70" aria-hidden="true" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{conv.title || t('untitledConversation')}</span>
                      <span className="block text-[11px] font-normal text-muted-foreground">{formatDateTime(conv.updated_at, locale, timezone)}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => { setRenameValue(conv.title); setPendingRename(conv); }}
                    className="inline-flex size-9 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-background hover:text-foreground focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:opacity-0 md:group-hover:opacity-100"
                    title={t('renameConversation')}
                    aria-label={t('renameConversation')}
                  >
                    <PencilIcon className="size-3.5" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    onClick={() => setPendingDeleteId(conv.id)}
                    className="mr-1 inline-flex size-9 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-destructive/10 hover:text-destructive focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:opacity-0 md:group-hover:opacity-100"
                    title={t('delete')}
                    aria-label={t('delete')}
                  >
                    <Trash2Icon className="size-3.5" aria-hidden="true" />
                  </button>
                </div>
              );
            })}
          </section>
        ))}
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
              className="bg-destructive text-destructive-foreground hover:opacity-90"
            >
              {t('delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog open={pendingRename !== null} onOpenChange={(open) => { if (!open) setPendingRename(null); }}>
        <DialogContent closeLabel={t('cancel')}>
          <DialogHeader>
            <DialogTitle>{t('renameConversation')}</DialogTitle>
            <DialogDescription className="sr-only">{t('renameLabel')}</DialogDescription>
          </DialogHeader>
          <form
            className="grid gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              const title = renameValue.trim();
              if (pendingRename && title && !renameMutation.isPending) renameMutation.mutate({ id: pendingRename.id, title });
            }}
          >
            <Input value={renameValue} onChange={(event) => setRenameValue(event.target.value)} aria-label={t('renameLabel')} maxLength={200} />
            {renameMutation.isError && <p role="alert" className="text-xs text-destructive">{t('renameFailed')}</p>}
            <DialogFooter>
              <Button type="button" className="secondary" onClick={() => setPendingRename(null)}>{t('cancel')}</Button>
              <Button type="submit" disabled={!renameValue.trim() || renameMutation.isPending}>{t('renameSave')}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </aside>
  );
}
