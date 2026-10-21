'use client';

import * as React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { PanelLeftClose, PencilIcon, PlusIcon, SearchIcon, Trash2Icon } from 'lucide-react';
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
  /** Optional callback that collapses the containing conversation-history sidebar. */
  onCollapse?: () => void;
  /** Optional handle filled with rename/delete openers so the conversation header can reuse this component's dialogs. */
  actionsRef?: React.MutableRefObject<ChatHistoryActions | null>;
}

/** Imperative openers for the rename and delete dialogs owned by ChatHistory. */
export interface ChatHistoryActions {
  /** Opens the rename dialog for a conversation. */
  rename: (conversation: { id: string; title: string }) => void;
  /** Opens the delete confirmation for a conversation id. */
  remove: (id: string) => void;
}

/**
 * Conversation history sidebar showing searchable, grouped threads with switching, rename, deletion, and an optional collapse action.
 *
 * @param props - ChatHistoryProps interface.
 * @returns Accessible conversation list sidebar.
 */
export function ChatHistory({
  activeConversationId,
  onSelectConversation,
  onNewConversation,
  className = '',
  actionsRef,
  onCollapse,
}: ChatHistoryProps) {
  const t = useTranslations('chat');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';
  const [searchQuery, setSearchQuery] = React.useState('');
  const [pendingDeleteId, setPendingDeleteId] = React.useState<string | null>(null);
  const [pendingRename, setPendingRename] = React.useState<{ id: string; title: string } | null>(null);
  const [renameValue, setRenameValue] = React.useState('');

  const { data: conversations = [], isLoading } = useQuery({
    queryKey: chatKeys.conversations(),
    queryFn: () => listConversations(),
  });

  React.useEffect(() => {
    if (!actionsRef) return;
    actionsRef.current = {
      rename: (conversation) => { setRenameValue(conversation.title); setPendingRename(conversation); },
      remove: setPendingDeleteId,
    };
    return () => { actionsRef.current = null; };
  }, [actionsRef]);

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

  // Filter sessions case-insensitively by title (including untitled fallback) or context kind.
  const filteredConversations = React.useMemo(() => {
    const trimmed = searchQuery.trim();
    if (!trimmed) return conversations;
    const query = trimmed.toLowerCase();
    return conversations.filter(
      (c) =>
        (c.title || t('untitledConversation')).toLowerCase().includes(query) ||
        (c.context_kind && c.context_kind.toLowerCase().includes(query)),
    );
  }, [conversations, searchQuery, t]);

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
      className={`flex flex-col h-full bg-surface shrink-0 ${className}`}
      aria-label={t('history')}
    >
      <div className="p-3 border-b border-border flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            onClick={onNewConversation}
            className="flex min-h-11 min-w-0 flex-1 items-center justify-center gap-2 text-sm font-semibold"
          >
            <PlusIcon className="size-4" aria-hidden="true" />
            <span>{t('newChat')}</span>
          </Button>
          {onCollapse && (
            <Button type="button" variant="outline" size="icon" className="size-11 shrink-0" onClick={onCollapse} aria-label={t('collapseHistory')} aria-expanded="true" aria-controls="chat-history-sidebar">
              <PanelLeftClose aria-hidden="true" />
            </Button>
          )}
        </div>

        <div className="relative">
          <SearchIcon className="absolute left-2.5 top-3.5 size-4 text-muted-foreground pointer-events-none" aria-hidden="true" />
          <Input
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => {
              // Allow Escape to clear the active filter without closing surrounding overlays.
              if (e.key === 'Escape' && searchQuery) {
                e.preventDefault();
                e.stopPropagation();
                setSearchQuery('');
              }
            }}
            placeholder={t('searchConversations')}
            aria-label={t('searchConversations')}
            className="pl-8 min-h-11 text-sm bg-background"
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
          <div className="p-4 text-xs text-center text-muted-foreground flex flex-col items-center gap-2">
            <span>{searchQuery.trim() && conversations.length > 0 ? t('paletteNoResults') : t('noHistory')}</span>
            {searchQuery.trim() && conversations.length > 0 && (
              <button
                type="button"
                onClick={() => setSearchQuery('')}
                className="inline-flex min-h-8 items-center rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium text-foreground hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {t('cancel')}
              </button>
            )}
          </div>
        )}

        {groupedConversations.map(({ key, items }) => (
          <section key={key} aria-label={key === 'today' ? t('historyToday') : t('historyEarlier')} className="flex flex-col gap-1">
            <h3 className="px-2 pb-1 pt-2 text-xs font-semibold text-muted-foreground">
              {key === 'today' ? t('historyToday') : t('historyEarlier')}
            </h3>
            {items.map((conv: Conversation) => {
              const isActive = conv.id === activeConversationId;
              return (
                <div
                  key={conv.id}
                  className={`group flex items-center gap-1 rounded-lg border text-sm ${isActive ? 'border-border bg-background font-semibold text-foreground' : 'border-transparent text-foreground hover:bg-background'}`}
                >
                  <button
                    type="button"
                    onClick={() => onSelectConversation(conv.id)}
                    aria-current={isActive ? 'true' : undefined}
                    className="flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-lg p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{conv.title || t('untitledConversation')}</span>
                      <span className="block text-[11px] font-normal text-muted-foreground">{formatDateTime(conv.updated_at, locale, timezone)}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => { setRenameValue(conv.title); setPendingRename({ id: conv.id, title: conv.title }); }}
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
