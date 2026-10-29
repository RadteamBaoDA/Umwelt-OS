'use client';

import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { PanelLeftClose, PencilIcon, PlusIcon, SearchIcon, Trash2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { ConversationActions } from '@/modules/chat/conversation-actions';
import {
  chatKeys,
  listConversations,
  type Conversation,
} from '@/modules/chat/api';
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
  /** Rename/delete openers from useConversationActions; the dialogs are mounted by the owner, not by this list. */
  actions: Pick<ConversationActions, 'rename' | 'remove'>;
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
  actions,
  onCollapse,
}: ChatHistoryProps) {
  const t = useTranslations('chat');
  const display = useDisplayPreferences();
  const timezone = display.confirmedPreferences?.timezone || 'UTC';
  const locale = display.confirmedPreferences?.locale || 'en-us';
  const [searchQuery, setSearchQuery] = React.useState('');
  const { data: conversations = [], isLoading, isError, refetch } = useQuery({
    queryKey: chatKeys.conversations(),
    queryFn: () => listConversations(),
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

  // Grouping uses the calendar day in the owner's preference timezone (not the browser's); the server orders newest first.
  const groupedConversations = React.useMemo(() => {
    const dayKey = (date: Date) => new Intl.DateTimeFormat('en-CA', { timeZone: timezone }).format(date);
    const today = dayKey(new Date());
    const groups = { today: [] as Conversation[], earlier: [] as Conversation[] };
    for (const conv of filteredConversations) {
      (dayKey(new Date(conv.updated_at)) === today ? groups.today : groups.earlier).push(conv);
    }
    return (['today', 'earlier'] as const).filter((key) => groups[key].length).map((key) => ({ key, items: groups[key] }));
  }, [filteredConversations, timezone]);

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
            {t('loading')}
          </div>
        )}

        {isError && (
          <div role="alert" className="flex flex-col items-center gap-2 p-4 text-center text-xs text-destructive">
            <span>{t('historyLoadFailed')}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => void refetch()}>{t('retry')}</Button>
          </div>
        )}

        {!isLoading && !isError && filteredConversations.length === 0 && (
          <div className="p-4 text-xs text-center text-muted-foreground flex flex-col items-center gap-2">
            <span>{searchQuery.trim() && conversations.length > 0 ? t('paletteNoResults') : t('noHistory')}</span>
            {searchQuery.trim() && conversations.length > 0 && (
              <Button type="button" variant="outline" size="sm" onClick={() => setSearchQuery('')}>
                {t('cancel')}
              </Button>
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
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => onSelectConversation(conv.id)}
                    aria-current={isActive ? 'true' : undefined}
                    className="h-auto min-h-11 min-w-0 flex-1 justify-start whitespace-normal rounded-lg p-2 text-left font-[inherit] hover:bg-transparent"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{conv.title || t('untitledConversation')}</span>
                      <span className="block text-[11px] font-normal text-muted-foreground">{formatDateTime(conv.updated_at, locale, timezone)}</span>
                    </span>
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => actions.rename({ id: conv.id, title: conv.title })}
                    className="size-9 shrink-0 text-muted-foreground focus-visible:opacity-100 md:opacity-0 md:group-hover:opacity-100"
                    title={t('renameConversation')}
                    aria-label={t('renameConversation')}
                  >
                    <PencilIcon className="size-3.5" aria-hidden="true" />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => actions.remove(conv.id)}
                    className="mr-1 size-9 shrink-0 text-muted-foreground hover:text-destructive focus-visible:opacity-100 md:opacity-0 md:group-hover:opacity-100"
                    title={t('delete')}
                    aria-label={t('delete')}
                  >
                    <Trash2Icon className="size-3.5" aria-hidden="true" />
                  </Button>
                </div>
              );
            })}
          </section>
        ))}
      </div>
    </aside>
  );
}
