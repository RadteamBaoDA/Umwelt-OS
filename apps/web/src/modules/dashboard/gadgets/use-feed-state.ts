'use client';

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type RefObject } from 'react';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { documentKeys, listGadgetDocumentProjections, setGadgetDocumentInteraction } from '@/modules/knowledge';
import { dashboardKeys, listGadgetSources } from '../api';
import { WINDOW_HOURS, windowStart, type SelectedTelegramIdentity } from './feed-model';

/**
 * User-controlled feed filters. The time window captures its lower bound when chosen, so every page and refetch
 * shares it (`windowSince`).
 */
export function useFeedFilters() {
  const [onlyUnread, setOnlyUnread] = useState<boolean>(false);
  // Row checkboxes appear only in selection mode so the resting list stays quiet.
  const [selectMode, setSelectMode] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [language, setLanguage] = useState('any');
  const [timeWindow, setTimeWindowRaw] = useState('any');
  // Lower bound captured when the window is chosen, so every page and refetch shares it.
  const [windowSince, setWindowSince] = useState<string | undefined>();
  /** Updates the feed time window filter. */
  const setTimeWindow = (value: string) => {
    const hours = WINDOW_HOURS[value];
    setWindowSince(hours ? windowStart(hours) : undefined);
    setTimeWindowRaw(value);
  };
  return {
    onlyUnread, setOnlyUnread, selectMode, setSelectMode, searchText, setSearchText, sourceFilter, setSourceFilter,
    language, setLanguage, timeWindow, setTimeWindow, windowSince,
  };
}

/** Server query inputs of a feed: the gadget's sources/channels plus the server-side filters. */
export interface FeedQueryInputs {
  sourceIds: string[];
  channelIds: string[];
  language: string;
  windowSince: string | undefined;
}

/**
 * Loads the gadget's documents page by page and, when more than one source can be filtered, their names.
 *
 * @returns The document infinite query and the source-name query (enabled only for multi-source feeds).
 */
export function useFeedQueries({ sourceIds, channelIds, language, windowSince }: FeedQueryInputs) {
  const docsQuery = useInfiniteQuery({
    queryKey: [...documentKeys.all, 'gadget', sourceIds, channelIds, language, windowSince],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      if (sourceIds.length === 0) return { items: [], next_cursor: null };
      return listGadgetDocumentProjections(sourceIds, channelIds, pageParam, {
        language: language === 'any' ? undefined : language,
        since: windowSince,
      });
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: 30_000,
  });
  // Source names for the filter; only fetched when more than one source can be filtered.
  const sourcesQuery = useQuery({
    queryKey: [...dashboardKeys.sources, 'feed-filter'],
    queryFn: ({ signal }) => listGadgetSources(100, undefined, signal),
    enabled: sourceIds.length > 1,
  });
  return { docsQuery, sourcesQuery };
}

/** One interaction change sent to the server for a document version. */
type InteractionChange = {
  id: string; versionNumber: number; item?: SelectedTelegramIdentity; read?: boolean; bookmarked?: boolean; dismissed?: boolean;
};

/**
 * Optimistic "Not relevant" moderation plus read/bookmark interactions.
 *
 * Version ids are hidden locally until the server filter catches up (a new version of the same document is not
 * hidden); `lastHidden` powers Undo. Interaction mutations are serialized per gadget so an Undo can never overtake
 * the Hide it reverses; a failed hide un-hides locally and a failed undo restores the hidden state with a retry.
 * Scope: dashboard feeds and highlights only.
 *
 * Side effect: after a hide / undo it moves focus to the Undo button / the feed root once that element exists.
 *
 * @param instanceId Gadget instance id (mutation scope).
 * @param refs The Undo button and feed root elements owned by the component (focus targets).
 */
export function useFeedModeration(instanceId: string, refs: { undoRef: RefObject<HTMLButtonElement | null>; rootRef: RefObject<HTMLDivElement | null> }) {
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [hiddenIds, setHiddenIds] = useState<ReadonlySet<string>>(new Set());
  const [lastHidden, setLastHidden] = useState<SelectedTelegramIdentity[]>([]);
  const [undoFailed, setUndoFailed] = useState(false);
  const [hideFailed, setHideFailed] = useState(false);
  const focusTarget = useRef<'undo' | 'root' | null>(null);
  const { undoRef, rootRef } = refs;

  const interactionMutation = useMutation({
    // Serialize hide/undo/read/save so an Undo can never overtake the Hide it reverses.
    scope: { id: `feed-interaction:${instanceId}` },
    mutationFn: (change: InteractionChange) =>
      setGadgetDocumentInteraction(change.id, change.versionNumber, {
        read: change.read, bookmarked: change.bookmarked, dismissed: change.dismissed,
      }, session.csrfToken),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: documentKeys.all }),
    onError: (_error, change) => {
      if (change.dismissed === undefined || !change.item) return;
      const { item } = change;
      if (change.dismissed) {
        setHideFailed(true);
        setHiddenIds((current) => { const next = new Set(current); next.delete(item.documentVersionId); return next; });
      } else {
        // Undo failed: the server still has it hidden, so restore the hidden state and offer a retry.
        setHiddenIds((current) => new Set(current).add(item.documentVersionId));
        setLastHidden((current) => (current.some((c) => c.documentVersionId === item.documentVersionId) ? current : [...current, item]));
        setUndoFailed(true);
        focusTarget.current = 'undo';
      }
    },
  });
  /** Hides or restores the given Telegram identities from the feed. */
  const setHidden = (items: SelectedTelegramIdentity[], hidden: boolean) => {
    setHiddenIds((current) => {
      const next = new Set(current);
      items.forEach((item) => (hidden ? next.add(item.documentVersionId) : next.delete(item.documentVersionId)));
      return next;
    });
    setLastHidden(hidden ? items : []);
    setUndoFailed(false);
    setHideFailed(false);
    focusTarget.current = hidden ? 'undo' : 'root';
    items.forEach((item) => interactionMutation.mutate({ id: item.documentId, versionNumber: item.versionNumber, item, dismissed: hidden }));
  };
  useEffect(() => {
    const target = focusTarget.current;
    if (!target || (target === 'undo' && !undoRef.current)) return;
    (target === 'undo' ? undoRef.current : rootRef.current)?.focus();
    focusTarget.current = null;
  }, [lastHidden, undoFailed, undoRef, rootRef]);

  return { hiddenIds, lastHidden, undoFailed, hideFailed, interactionMutation, setHidden };
}

/**
 * Pulls further server pages while nothing is visible: pages can be short or fully hidden, so while nothing is
 * visible and a cursor exists the next page is fetched (at most 3 times per server scope) instead of claiming the
 * feed is empty. Does nothing while client filters are active, for preloaded items, or while fetching / errored.
 */
export function useAutoFetchWhenEmpty(args: {
  docsQuery: ReturnType<typeof useFeedQueries>['docsQuery'];
  displayedCount: number;
  filtering: boolean;
  hasInitialItems: boolean;
  serverScope: string;
}) {
  const { docsQuery, displayedCount, filtering, hasInitialItems, serverScope } = args;
  const autoFetches = useRef({ scope: '', count: 0 });
  useEffect(() => {
    const budget = autoFetches.current;
    if (budget.scope !== serverScope || displayedCount > 0) { budget.scope = serverScope; budget.count = 0; }
    if (filtering || displayedCount > 0 || hasInitialItems || !docsQuery.hasNextPage || docsQuery.isFetching || docsQuery.isError || budget.count >= 3) return;
    budget.count += 1;
    void docsQuery.fetchNextPage();
  }, [displayedCount, hasInitialItems, docsQuery, serverScope, filtering]);
}
