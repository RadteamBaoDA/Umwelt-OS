'use client';

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bookmark, Search, Sparkles } from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FeedSourceTabs } from '@/components/feed-source-tabs';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useWorkspaceSession } from '@/core/app-shell/workspace-session';
import { useWorkspace } from '@/core/workspace-context';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { safeHttpUrl } from '@/core/safe-url';
import { documentKeys, listGadgetDocumentProjections, setGadgetDocumentInteraction } from '@/modules/knowledge';
import { dashboardKeys, listGadgetSources, type GadgetInstance } from '../api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { ShowMoreButton, useShowMore } from './show-more';
import { useSkin } from '@/core/use-skin';

/** Props for the VideoPanel gadget component. */
export interface VideoPanelProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/** Metadata needed to render a media record without exposing provider-specific URL rules. */
type VideoRecord = Awaited<ReturnType<typeof listGadgetDocumentProjections>>['items'][number];

/** Video item as the panel maps it from a YouTube document projection. */
interface VideoItem {
  id: string;
  title: string;
  channel: string;
  videoUrl: string;
  imageUrl: string | null;
  durationSeconds: number | null;
}

/**
 * `mm:ss` (or `h:mm:ss`) clock text of a duration.
 *
 * @param seconds Non-negative duration in seconds; null, negative or non-finite yields an empty string.
 */
export function formatVideoDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  // Minutes are two digits like the demo caption (`02:14`).
  const s = String(total % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${String(m).padStart(2, '0')}:${s}`;
}

/**
 * Terminal skin: the first video as a 16:9 frame with a play square and a `title  duration` caption (demo `.dash-video`).
 * The frame is the same outbound link as the list rows (new tab, `noopener`); the thumbnail shows when the record has one.
 *
 * @param props.video First video of the list.
 */
function TerminalVideoFrame({ video }: { video: VideoItem }) {
  const dashboard = useTranslations('dashboard');
  const [failed, setFailed] = useState(false);
  return (
    <a
      className="video-frame"
      href={video.videoUrl}
      target="_blank"
      rel="noopener noreferrer"
      title={dashboard('video.open')}
      aria-label={`${dashboard('video.open')}: ${video.title}`}
    >
      {video.imageUrl && !failed && (
        // eslint-disable-next-line @next/next/no-img-element -- same-origin signed proxy path
        <img src={video.imageUrl} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} className="video-frame-img" />
      )}
      <span className="video-frame-play" aria-hidden="true">
        ▶
      </span>
      <span className="video-frame-cap" aria-hidden="true">
        <span>{video.title}</span>
        <span>{formatVideoDuration(video.durationSeconds)}</span>
      </span>
    </a>
  );
}

/**
 * Loads up to 32 bound sources through the owner-authorized projection API and renders their media/video records.
 * Source tabs and the opaque cursor remain server query inputs; search filters only loaded projections. Read and
 * bookmark writes are owner-only and identify the exact current version. Ask AI sends only source, document and
 * version IDs so Chat rechecks ownership, provenance and local-only policy before model egress.
 *
 * @param instance Persisted gadget definition containing the selected source IDs.
 */
export function VideoPanel({ instance }: VideoPanelProps) {
  const daily = useTranslations('daily');
  const display = useDisplayPreferences();
  const { openDrawer } = useChatController();
  const { isOwner } = useWorkspace();
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const [activeSource, setActiveSource] = useState('all');
  const [search, setSearch] = useState('');
  const sourceIds = [...new Set(instance.definition.source_ids)].slice(0, 32);
  const selectedSource = sourceIds.includes(activeSource) ? activeSource : 'all';
  const sources = useQuery({
    queryKey: [...dashboardKeys.sources, 'video-panel', sourceIds],
    enabled: sourceIds.length > 1,
    queryFn: async ({ signal }) => (await listGadgetSources(100, undefined, signal)).items,
  });
  const selectedSources = selectedSource === 'all' ? sourceIds : [selectedSource];
  const query = useInfiniteQuery({
    queryKey: [...documentKeys.all, 'video-panel', selectedSources],
    enabled: selectedSources.length > 0,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listGadgetDocumentProjections(selectedSources, [], pageParam),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    staleTime: 30_000,
  });
  const mutation = useMutation({
    mutationFn: (change: { item: VideoRecord; read?: boolean; bookmarked?: boolean }) =>
      setGadgetDocumentInteraction(
        change.item.document_id,
        change.item.version_number,
        { read: change.read, bookmarked: change.bookmarked },
        session.csrfToken,
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: documentKeys.all }),
  });
  const items = (query.data?.pages.flatMap((page) => page.items) ?? []).filter((item) =>
    `${item.title} ${item.excerpt} ${item.media?.creator ?? ''}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  );
  const more = useShowMore(items, 5, `${selectedSources.join(',')}:${search}`);
  const terminal = useSkin() === 'terminal';
  const first = terminal ? terminalFirstVideo(items) : undefined;
  useReportGadgetFreshness(null, items.length ? daily('streamCount', { count: items.length }) : null);

  return (
    <VideoPanelView
      sourceIds={sourceIds}
      activeSource={selectedSource}
      setActiveSource={setActiveSource}
      sources={sources.data?.map(({ id, name }) => ({ id, name }))}
      search={search}
      setSearch={setSearch}
      query={query}
      items={items}
      visibleItems={more.visible}
      more={more}
      first={first}
      isOwner={isOwner}
      display={display}
      mutate={mutation.mutate}
      pending={mutation.isPending}
      ask={(item) =>
        isOwner &&
        openDrawer({
          context: {
            kind: 'selection',
            items: [{ sourceId: item.source_id, documentId: item.document_id, documentVersionId: item.document_version_id }],
          },
        })
      }
    />
  );
}

/** Displays the filtered projection page; load-more forwards the server cursor unchanged. */
function VideoPanelView({
  sourceIds,
  activeSource,
  setActiveSource,
  sources,
  search,
  setSearch,
  query,
  items,
  visibleItems,
  more,
  first,
  isOwner,
  display,
  mutate,
  pending,
  ask,
}: {
  sourceIds: string[];
  activeSource: string;
  setActiveSource: (value: string) => void;
  sources?: { id: string; name: string }[];
  search: string;
  setSearch: (value: string) => void;
  query: ReturnType<typeof useInfiniteQuery>;
  items: VideoRecord[];
  visibleItems: VideoRecord[];
  more: { hiddenCount: number; expanded: boolean; toggle: () => void };
  first?: VideoItem;
  isOwner: boolean;
  display: ReturnType<typeof useDisplayPreferences>;
  mutate: (change: { item: VideoRecord; read?: boolean; bookmarked?: boolean }) => void;
  pending: boolean;
  ask: (item: VideoRecord) => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto bg-card p-3 text-card-foreground">
      {sourceIds.length === 0 && <GadgetNotice kind="setup" className="p-0" message={t('video.sourcesRequired')} />}
      {sourceIds.length > 1 && (
        <FeedSourceTabs sourceIds={sourceIds} sourceNames={sources} value={activeSource} onChange={setActiveSource} />
      )}
      {sourceIds.length > 0 && (
        <label className="relative block">
          <Search aria-hidden="true" className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t('video.search')}
            aria-label={t('video.search')}
            className="pl-9"
          />
        </label>
      )}
      {sourceIds.length > 0 && query.isPending && <GadgetSkeleton variant="list" className="p-0" label={t('gadget.dataLoading')} />}
      {query.isError && <GadgetNotice kind="error" className="p-0" message={t('gadget.dataLoadFailed')} />}
      {sourceIds.length > 0 && !query.isPending && !query.isError && items.length === 0 && (
        <GadgetNotice
          kind="empty"
          className="p-0"
          message={t('video.recordsEmpty')}
          action={{ href: '/settings/sources', label: t('gadget.configureAction') }}
        />
      )}
      {first && (
        <>
          <TerminalVideoFrame video={first} />
          <p className="video-term-note m-0" aria-hidden="true">
            {t('video.termNote', { count: items.length, channel: first.channel })}
          </p>
        </>
      )}
      <ul className="divide-y divide-border">
        {visibleItems
          .filter((item) => item.document_version_id !== first?.id)
          .map((item) => (
            <VideoRow
              key={item.document_version_id}
              item={item}
              isOwner={isOwner}
              bookmarked={!!item.bookmarked_at}
              read={!!item.read_at}
              pending={pending}
              timestamp={item.published_at ?? item.observed_at}
              dateLabel={formatDateTime(item.published_at ?? item.observed_at, display.locale, display.timezone)}
              ask={() => ask(item)}
              toggleRead={() => mutate({ item, read: !item.read_at })}
              toggleBookmark={() => mutate({ item, bookmarked: !item.bookmarked_at })}
            />
          ))}
      </ul>
      <ShowMoreButton state={more} />
      {query.hasNextPage && (
        <Button type="button" variant="outline" size="sm" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
          {query.isFetchingNextPage ? t('gadget.dataLoading') : t('video.loadMore')}
        </Button>
      )}
    </div>
  );
}

/**
 * Picks the terminal-skin hero video: the first loaded item of any video-shaped source that has a safe
 * http(s) canonical URL. Items without one are skipped; no provider or host is special-cased.
 *
 * @param items Loaded projection items in display order.
 * @returns The hero video fields, or undefined when no item has a safe URL.
 */
function terminalFirstVideo(items: VideoRecord[]): VideoItem | undefined {
  for (const item of items) {
    const videoUrl = safeHttpUrl(item.canonical_url);
    if (!videoUrl) continue;
    return {
      id: item.document_version_id,
      title: item.title,
      channel: creatorOf(item) ?? '',
      videoUrl,
      imageUrl: item.image_url ?? null,
      durationSeconds: item.media?.duration_seconds ?? null,
    };
  }
  return undefined;
}

/** Uses typed media creator metadata, falling back to legacy provider author only when available. */
function creatorOf(item: VideoRecord) {
  const author = item.provider_metadata?.source_fields.author;
  return item.media?.creator ?? (typeof author === 'string' ? author : null);
}

/** Hides missing and failed signed artwork instead of leaving an empty thumbnail box. */
function VideoThumb({ src }: { src: string | null | undefined }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) return null;
  return (
    // eslint-disable-next-line @next/next/no-img-element -- authenticated same-origin media proxy URL.
    <img
      src={src}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className="aspect-video w-28 shrink-0 rounded-md object-cover"
    />
  );
}

/** Renders projection metadata and safe external links; owner actions retain immutable version identity. */
function VideoRow({
  item,
  isOwner,
  bookmarked,
  read,
  pending,
  timestamp,
  dateLabel,
  ask,
  toggleRead,
  toggleBookmark,
}: {
  item: VideoRecord;
  isOwner: boolean;
  bookmarked: boolean;
  read: boolean;
  pending: boolean;
  timestamp: string;
  dateLabel: string;
  ask: () => void;
  toggleRead: () => void;
  toggleBookmark: () => void;
}) {
  const t = useTranslations('dashboard');
  const creator = creatorOf(item);
  const duration = item.media?.duration_seconds;
  const externalUrl = safeHttpUrl(item.canonical_url);
  return (
    <li className="flex items-start gap-3 py-2">
      <VideoThumb src={item.image_url} />
      <div className="min-w-0 flex-1">
        <a
          href={externalUrl ?? `/knowledge/documents/${item.document_id}`}
          target={externalUrl ? '_blank' : undefined}
          rel={externalUrl ? 'noopener noreferrer' : undefined}
          className="line-clamp-2 text-sm font-semibold text-foreground hover:underline underline-offset-4"
        >
          {item.title}
        </a>
        <p className="mt-0.5 text-xs text-muted-foreground">
          <time dateTime={timestamp} title={dateLabel}>
            {dateLabel}
          </time>
          {creator ? ` · ${creator}` : ''}
          {duration != null ? ` · ${t('video.duration', { duration: Math.round(duration / 60) })}` : ''}
        </p>
        {isOwner && (
          <div className="mt-1 flex flex-wrap gap-1">
            <Button type="button" variant="ghost" size="sm" className="h-7 px-2" onClick={ask}>
              <Sparkles className="mr-1 size-3.5" />
              {t('askAbout.message')}
            </Button>
            <Button type="button" variant="ghost" size="sm" className="h-7 px-2" disabled={pending} onClick={toggleRead}>
              {read ? t('markUnread') : t('markRead')}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              disabled={pending}
              onClick={toggleBookmark}
              aria-pressed={bookmarked}
              aria-label={bookmarked ? t('removeBookmark') : t('bookmark')}
            >
              <Bookmark className="size-3.5" fill={bookmarked ? 'currentColor' : 'none'} />
            </Button>
          </div>
        )}
      </div>
    </li>
  );
}
