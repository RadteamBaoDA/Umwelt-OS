'use client';

import * as React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Loader2Icon, PanelLeftClose, PencilIcon, PlusIcon, SearchIcon, Trash2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { ConversationActions } from './conversation-actions';
import {
  chatKeys,
  getConversation,
  listConversations,
  type Conversation,
} from './api';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime, type AppLocaleId } from '@/core/i18n';

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

type GroupKey = 'today' | 'earlier';

/**
 * Splits conversations into "today" and "earlier" by calendar day in the owner's preference timezone (not the
 * browser's); the server orders newest first and that order is kept inside each group.
 *
 * @param conversations - Already filtered conversations.
 * @param timezone - IANA timezone of the display preferences.
 * @returns Non-empty groups in display order.
 */
function groupConversationsByDay(conversations: Conversation[], timezone: string): { key: GroupKey; items: Conversation[] }[] {
  /** Formats a date as a YYYY-MM-DD key in the display timezone for day grouping. */
  const dayKey = (date: Date) => new Intl.DateTimeFormat('en-CA', { timeZone: timezone }).format(date);
  const today = dayKey(new Date());
  const groups = { today: [] as Conversation[], earlier: [] as Conversation[] };
  for (const conv of conversations) {
    (dayKey(new Date(conv.updated_at)) === today ? groups.today : groups.earlier).push(conv);
  }
  return (['today', 'earlier'] as const).filter((key) => groups[key].length).map((key) => ({ key, items: groups[key] }));
}

/**
 * Data for the history list: conversations, the passive active-transcript loading flag, the search filter and day groups.
 *
 * @param activeConversationId - Open conversation, if any.
 * @param searchQuery - Raw search text.
 * @param timezone - Display timezone used for grouping.
 */
function useHistoryData(activeConversationId: string | null | undefined, searchQuery: string, timezone: string) {
  const t = useTranslations('chat');
  const { data: conversations = [], isLoading, isError, refetch } = useQuery({
    queryKey: chatKeys.conversations(),
    queryFn: () => listConversations(),
  });

  // Passive observer of the active transcript query (ChatSession owns fetching): the first load after a conversation
  // switch shows an inline spinner on that row instead of a page-level loader; background refetches do not.
  const activeDetail = useQuery({
    queryKey: chatKeys.conversation(activeConversationId ?? ''),
    queryFn: () => getConversation(activeConversationId!),
    enabled: false,
  });
  const activeLoading = Boolean(activeConversationId) && activeDetail.isFetching && activeDetail.data === undefined;

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

  const groupedConversations = React.useMemo(
    () => groupConversationsByDay(filteredConversations, timezone),
    [filteredConversations, timezone],
  );
  return { conversations, isLoading, isError, refetch, activeLoading, filteredConversations, groupedConversations };
}

/** Search box; Escape clears the active filter without closing surrounding overlays. */
function HistorySearch({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const t = useTranslations('chat');
  return (
    <div className="relative">
      <SearchIcon className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground pointer-events-none" aria-hidden="true" />
      <Input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          // Allow Escape to clear the active filter without closing surrounding overlays.
          if (e.key === 'Escape' && value) {
            e.preventDefault();
            e.stopPropagation();
            onChange('');
          }
        }}
        placeholder={t('searchConversations')}
        aria-label={t('searchConversations')}
        className="pl-8 text-sm"
      />
    </div>
  );
}

/** Loading / error / empty feedback above the list. */
function HistoryStatus({ isLoading, isError, onRetry, isEmpty, searching, onClearSearch }: {
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  isEmpty: boolean;
  /** True when a non-blank filter is hiding existing conversations. */
  searching: boolean;
  onClearSearch: () => void;
}) {
  const t = useTranslations('chat');
  return (
    <>
      {isLoading && (
        <div role="status" className="p-4 text-xs text-center text-muted-foreground">
          {t('loading')}
        </div>
      )}

      {isError && (
        <div role="alert" className="flex flex-col items-center gap-2 p-4 text-center text-xs text-destructive">
          <span>{t('historyLoadFailed')}</span>
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>{t('retry')}</Button>
        </div>
      )}

      {!isLoading && !isError && isEmpty && (
        <div className="p-4 text-xs text-center text-muted-foreground flex flex-col items-center gap-2">
          <span>{searching ? t('paletteNoResults') : t('noHistory')}</span>
          {searching && (
            <Button type="button" variant="outline" size="sm" onClick={onClearSearch}>
              {t('cancel')}
            </Button>
          )}
        </div>
      )}
    </>
  );
}

/** One conversation row: open button (with inline loading spinner for the active row) plus rename and delete. */
function ConversationRow({ conv, isActive, activeLoading, locale, timezone, onSelect, actions }: {
  conv: Conversation;
  isActive: boolean;
  activeLoading: boolean;
  locale: AppLocaleId;
  timezone: string;
  onSelect: (id: string) => void;
  actions: ChatHistoryProps['actions'];
}) {
  const t = useTranslations('chat');
  return (
    <div
      className={`group flex items-center gap-1 rounded-md text-sm ${isActive ? 'bg-active font-semibold text-foreground' : 'text-foreground hover:bg-raised'}`}
    >
      <Button
        type="button"
        variant="ghost"
        onClick={() => onSelect(conv.id)}
        aria-current={isActive ? 'true' : undefined}
        aria-busy={isActive && activeLoading ? 'true' : undefined}
        className="h-auto min-w-0 flex-1 justify-start whitespace-normal rounded-lg p-2 text-left font-[inherit] hover:bg-transparent"
      >
        <span className="min-w-0 flex-1">
          <span className="block truncate">{conv.title || t('untitledConversation')}</span>
          <span className="block text-[11px] font-normal text-muted-foreground">{formatDateTime(conv.updated_at, locale, timezone)}</span>
        </span>
        {isActive && activeLoading && (
          <Loader2Icon data-testid="conversation-loading" className="size-3.5 shrink-0 animate-spin text-muted-foreground" aria-label={t('loading')} />
        )}
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
  const data = useHistoryData(activeConversationId, searchQuery, timezone);
  const searching = Boolean(searchQuery.trim()) && data.conversations.length > 0;

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
            className="flex min-w-0 flex-1 items-center justify-center gap-2 text-sm font-semibold"
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
        <HistorySearch value={searchQuery} onChange={setSearchQuery} />
      </div>

      <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-1">
        <HistoryStatus
          isLoading={data.isLoading}
          isError={data.isError}
          onRetry={() => void data.refetch()}
          isEmpty={data.filteredConversations.length === 0}
          searching={searching}
          onClearSearch={() => setSearchQuery('')}
        />

        {data.groupedConversations.map(({ key, items }) => (
          <section key={key} aria-label={key === 'today' ? t('historyToday') : t('historyEarlier')} className="flex flex-col gap-1">
            <h3 className="px-2 pb-1 pt-2 text-xs font-semibold text-muted-foreground">
              {key === 'today' ? t('historyToday') : t('historyEarlier')}
            </h3>
            {items.map((conv: Conversation) => (
              <ConversationRow
                key={conv.id}
                conv={conv}
                isActive={conv.id === activeConversationId}
                activeLoading={data.activeLoading}
                locale={locale}
                timezone={timezone}
                onSelect={onSelectConversation}
                actions={actions}
              />
            ))}
          </section>
        ))}
      </div>
    </aside>
  );
}
