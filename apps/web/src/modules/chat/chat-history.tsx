'use client';

import * as React from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  EllipsisIcon,
  Loader2Icon,
  PanelLeftClose,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  SearchIcon,
  Trash2Icon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { ConversationActions } from './conversation-actions';
import { chatKeys, getConversation, listConversations, type Conversation } from './api';
import { useDisplayPreferences } from '@/core/query-provider';
import { formatDateTime, normalizeFormattingLocale, type AppLocaleId } from '@/core/i18n';

/** Conversations fetched per "Load more" page; the server caps `limit` at 100. */
export const HISTORY_PAGE_SIZE = 30;
/** Delay before typing in the search box triggers a server query. */
const SEARCH_DEBOUNCE_MS = 250;

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
  /** Row-menu openers from useConversationActions; the dialogs are mounted by the owner, not by this list. */
  actions: Pick<ConversationActions, 'rename' | 'remove' | 'setPinned' | 'setArchived'> &
    Partial<Pick<ConversationActions, 'flagError' | 'clearFlagError'>>;
}

type GroupKey = 'pinned' | 'today' | 'yesterday' | 'last7' | 'last30' | 'older';
const GROUP_ORDER: GroupKey[] = ['pinned', 'today', 'yesterday', 'last7', 'last30', 'older'];
const GROUP_LABEL = {
  pinned: 'historyPinned',
  today: 'historyToday',
  yesterday: 'historyYesterday',
  last7: 'historyLast7Days',
  last30: 'historyLast30Days',
  older: 'historyOlder',
} as const;

/**
 * Splits conversations into Pinned plus calendar-day buckets (Today, Yesterday, Previous 7 days, Previous 30 days,
 * Older) measured in the owner's preference timezone (not the browser's). The server order (pinned first, then newest
 * first) is kept inside each group.
 *
 * @param conversations - Conversations in server order.
 * @param timezone - IANA timezone of the display preferences.
 * @param now - Reference instant; injectable for tests.
 * @returns Non-empty groups in display order.
 */
export function groupConversationsByDay(
  conversations: Conversation[],
  timezone: string,
  now: Date = new Date(),
): { key: GroupKey; items: Conversation[] }[] {
  /** Whole-day number of a date in the display timezone (en-CA formats as YYYY-MM-DD). */
  const dayNumber = (date: Date) => {
    const [y, m, d] = new Intl.DateTimeFormat('en-CA', { timeZone: timezone }).format(date).split('-').map(Number);
    return Date.UTC(y, m - 1, d) / 86_400_000;
  };
  const today = dayNumber(now);
  const groups: Record<GroupKey, Conversation[]> = { pinned: [], today: [], yesterday: [], last7: [], last30: [], older: [] };
  for (const conv of conversations) {
    const age = today - dayNumber(new Date(conv.updated_at));
    const key: GroupKey = conv.pinned
      ? 'pinned'
      : age <= 0
        ? 'today'
        : age === 1
          ? 'yesterday'
          : age <= 7
            ? 'last7'
            : age <= 30
              ? 'last30'
              : 'older';
    groups[key].push(conv);
  }
  return GROUP_ORDER.filter((key) => groups[key].length).map((key) => ({ key, items: groups[key] }));
}

/**
 * Returns `value` after it has stayed unchanged for `delayMs`, so typing does not fire a request per keystroke.
 *
 * @param value - Fast-changing input.
 * @param delayMs - Quiet period before the new value is published.
 */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = React.useState(value);
  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/**
 * Data for the history list: server-side searched and paged conversations, the passive active-transcript loading
 * flag and the day groups. Search (`q`, title or message text) and the archived filter run on the server; TanStack
 * Query aborts the superseded request through the signal.
 *
 * @param activeConversationId - Open conversation, if any.
 * @param searchQuery - Raw search text (debounced here).
 * @param archived - Whether to list archived instead of active conversations.
 * @param timezone - Display timezone used for grouping.
 */
function useHistoryData(activeConversationId: string | null | undefined, searchQuery: string, archived: boolean, timezone: string) {
  const q = useDebouncedValue(searchQuery.trim(), SEARCH_DEBOUNCE_MS);
  const list = useInfiniteQuery({
    // Nested under chatKeys.conversations() so every existing invalidation also refreshes this list.
    queryKey: [...chatKeys.conversations(), 'list', { archived, q }],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      listConversations({ limit: HISTORY_PAGE_SIZE, offset: pageParam, archived, q: q || undefined, signal }),
    getNextPageParam: (lastPage, pages) => (lastPage.length < HISTORY_PAGE_SIZE ? undefined : pages.length * HISTORY_PAGE_SIZE),
    // Keep the previous results on screen while a new search is in flight (no flash of the empty state).
    placeholderData: (previous) => previous,
  });

  // Passive observer of the active transcript query (ChatSession owns fetching): the first load after a conversation
  // switch shows an inline spinner on that row instead of a page-level loader; background refetches do not.
  const activeDetail = useQuery({
    queryKey: chatKeys.conversation(activeConversationId ?? ''),
    queryFn: () => getConversation(activeConversationId!),
    enabled: false,
  });
  const activeLoading = Boolean(activeConversationId) && activeDetail.isFetching && activeDetail.data === undefined;

  const conversations = React.useMemo(() => list.data?.pages.flat() ?? [], [list.data]);
  const groupedConversations = React.useMemo(() => groupConversationsByDay(conversations, timezone), [conversations, timezone]);
  return {
    conversations,
    isLoading: list.isLoading,
    isError: list.isError,
    refetch: list.refetch,
    activeLoading,
    groupedConversations,
    hasMore: Boolean(list.hasNextPage),
    loadingMore: list.isFetchingNextPage,
    loadMore: () => void list.fetchNextPage(),
  };
}

/** Search box; Escape clears the active filter without closing surrounding overlays. */
function HistorySearch({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const t = useTranslations('chat');
  return (
    <div className="relative">
      <SearchIcon
        className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground pointer-events-none"
        aria-hidden="true"
      />
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

/**
 * Loading / error / empty feedback above the list. An empty result is explained by what hides the conversations: a
 * search with no match, an archive with nothing in it, or a workspace with no conversations at all.
 *
 * @param props - Query state, whether a search text or the archived view is active, and the way back from each filter.
 */
function HistoryStatus({
  isLoading,
  isError,
  onRetry,
  isEmpty,
  searching,
  archived,
  onClearSearch,
  onBackToList,
}: {
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  isEmpty: boolean;
  /** True when a search text is hiding conversations. */
  searching: boolean;
  /** True while the archived view is shown. */
  archived: boolean;
  onClearSearch: () => void;
  onBackToList: () => void;
}) {
  const t = useTranslations('chat');
  const empty = searching
    ? { text: t('historySearchEmpty'), label: t('historyClearSearch'), onAction: onClearSearch }
    : archived
      ? { text: t('historyArchivedEmpty'), label: t('historyBackToList'), onAction: onBackToList }
      : null;
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
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            {t('retry')}
          </Button>
        </div>
      )}

      {!isLoading && !isError && isEmpty && (
        <div className="p-4 text-xs text-center text-muted-foreground flex flex-col items-center gap-2">
          <span>{empty ? empty.text : t('noHistory')}</span>
          {empty && (
            <Button type="button" variant="outline" size="sm" onClick={empty.onAction}>
              {empty.label}
            </Button>
          )}
        </div>
      )}
    </>
  );
}

/** Pulsing dot (C15): the list API reports a run still generating, so a conversation answering in the background is visible. */
function RunningDot({ active }: { active: boolean }) {
  const t = useTranslations('chat');
  if (!active) return null;
  return (
    <span
      role="img"
      data-testid="conversation-running"
      aria-label={t('conversationRunning')}
      title={t('conversationRunning')}
      className="size-2 shrink-0 animate-pulse rounded-full bg-accent motion-reduce:animate-none"
    />
  );
}

/** One conversation row: open button (with inline loading spinner for the active row, a pulsing dot while a run is active) plus a keyboard/touch-reachable actions menu. */
function ConversationRow({
  conv,
  isActive,
  activeLoading,
  locale,
  timezone,
  timeOnly,
  onSelect,
  actions,
}: {
  conv: Conversation;
  isActive: boolean;
  activeLoading: boolean;
  locale: AppLocaleId;
  timezone: string;
  /** Row sits under a Today/Yesterday heading, so the date would repeat the heading: show the time only. */
  timeOnly: boolean;
  onSelect: (id: string) => void;
  actions: ChatHistoryProps['actions'];
}) {
  const t = useTranslations('chat');
  const stamp = timeOnly
    ? new Intl.DateTimeFormat(normalizeFormattingLocale(locale), { timeStyle: 'short', timeZone: timezone }).format(
        new Date(conv.updated_at),
      )
    : formatDateTime(conv.updated_at, locale, timezone);
  return (
    <div>
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
            <span className="flex items-center gap-1">
              {conv.pinned && <PinIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />}
              <span className="block truncate">{conv.title || t('untitledConversation')}</span>
              <RunningDot active={conv.has_active_run} />
            </span>
            <span className="block text-xs font-normal text-muted-foreground">{stamp}</span>
          </span>
          {isActive && activeLoading && (
            <Loader2Icon
              data-testid="conversation-loading"
              className="size-3.5 shrink-0 animate-spin text-muted-foreground"
              aria-label={t('loading')}
            />
          )}
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="mr-1 size-9 shrink-0 text-muted-foreground focus-visible:opacity-100 data-[state=open]:opacity-100 md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100"
              title={t('conversationActionsMenu')}
              aria-label={t('conversationActionsMenu')}
            >
              <EllipsisIcon className="size-4" aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => actions.rename({ id: conv.id, title: conv.title })}>
              <PencilIcon aria-hidden="true" />
              {t('renameConversation')}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => actions.setPinned(conv.id, !conv.pinned)}>
              {conv.pinned ? <PinOffIcon aria-hidden="true" /> : <PinIcon aria-hidden="true" />}
              {conv.pinned ? t('unpinConversation') : t('pinConversation')}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => actions.setArchived(conv.id, !conv.archived)}>
              {conv.archived ? <ArchiveRestoreIcon aria-hidden="true" /> : <ArchiveIcon aria-hidden="true" />}
              {conv.archived ? t('unarchiveConversation') : t('archiveConversation')}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={() => actions.remove(conv.id)}>
              <Trash2Icon aria-hidden="true" />
              {t('delete')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {actions.flagError === conv.id && (
        <p role="alert" className="px-2 pb-1 text-xs text-destructive">
          {t('historyFlagFailed')}
        </p>
      )}
    </div>
  );
}

/**
 * Header of the history sidebar: New chat (with its shortcut hint), the optional collapse button, the search box and the
 * archived-view toggle.
 *
 * @param props - Handlers and the current search / archived state owned by {@link ChatHistory}.
 */
function HistoryToolbar({
  onNewConversation,
  onCollapse,
  searchQuery,
  onSearchChange,
  showArchived,
  onToggleArchived,
}: {
  onNewConversation: () => void;
  onCollapse?: () => void;
  searchQuery: string;
  onSearchChange: (value: string) => void;
  showArchived: boolean;
  onToggleArchived: () => void;
}) {
  const t = useTranslations('chat');
  return (
    <div className="p-3 border-b border-border flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Button
          type="button"
          onClick={onNewConversation}
          title={t('newChatShortcut')}
          className="flex min-w-0 flex-1 items-center justify-center gap-2 text-sm font-semibold"
        >
          <PlusIcon className="size-4" aria-hidden="true" />
          <span>{t('newChat')}</span>
        </Button>
        {onCollapse && (
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-11 shrink-0"
            onClick={onCollapse}
            aria-label={t('collapseHistory')}
            aria-expanded="true"
            aria-controls="chat-history-sidebar"
          >
            <PanelLeftClose aria-hidden="true" />
          </Button>
        )}
      </div>
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <HistorySearch value={searchQuery} onChange={onSearchChange} />
        </div>
        <Button
          type="button"
          variant={showArchived ? 'default' : 'outline'}
          size="icon"
          className="size-9 shrink-0"
          aria-pressed={showArchived}
          title={t('historyShowArchived')}
          aria-label={t('historyShowArchived')}
          onClick={onToggleArchived}
        >
          <ArchiveIcon className="size-4" aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}

/**
 * Conversation history sidebar: server-searched, paged, grouped (Pinned / date buckets) threads with an archived
 * filter, per-row menu (rename, pin, archive, delete) and an optional collapse action.
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
  const [showArchived, setShowArchived] = React.useState(false);
  const data = useHistoryData(activeConversationId, searchQuery, showArchived, timezone);

  return (
    <aside className={`flex flex-col h-full bg-surface shrink-0 ${className}`} aria-label={t('history')}>
      <HistoryToolbar
        onNewConversation={onNewConversation}
        onCollapse={onCollapse}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        showArchived={showArchived}
        onToggleArchived={() => {
          // The failed pin/archive row may not be listed in the other view; its error would resurface stale.
          actions.clearFlagError?.();
          setShowArchived((value) => !value);
        }}
      />

      <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-1">
        <HistoryStatus
          isLoading={data.isLoading}
          isError={data.isError}
          onRetry={() => void data.refetch()}
          isEmpty={data.conversations.length === 0}
          searching={Boolean(searchQuery.trim())}
          archived={showArchived}
          onClearSearch={() => setSearchQuery('')}
          onBackToList={() => setShowArchived(false)}
        />

        {showArchived && !data.isLoading && (
          <h3 data-testid="history-archived-label" className="px-2 pb-1 pt-2 text-xs font-semibold text-muted-foreground">
            {t('historyArchivedLabel')}
          </h3>
        )}

        {data.groupedConversations.map(({ key, items }) => (
          <section key={key} aria-label={t(GROUP_LABEL[key])} className="flex flex-col gap-1">
            <h3 className="px-2 pb-1 pt-2 text-xs font-semibold text-muted-foreground">{t(GROUP_LABEL[key])}</h3>
            {items.map((conv: Conversation) => (
              <ConversationRow
                key={conv.id}
                conv={conv}
                isActive={conv.id === activeConversationId}
                activeLoading={data.activeLoading}
                locale={locale}
                timezone={timezone}
                timeOnly={key === 'today' || key === 'yesterday'}
                onSelect={onSelectConversation}
                actions={actions}
              />
            ))}
          </section>
        ))}

        {data.hasMore && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-1 self-center"
            disabled={data.loadingMore}
            onClick={data.loadMore}
          >
            {data.loadingMore ? t('loading') : t('historyLoadMore')}
          </Button>
        )}
      </div>
    </aside>
  );
}
