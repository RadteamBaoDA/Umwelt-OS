'use client';

import { Bookmark, ExternalLink, ListFilter, RotateCw, SquareCheck } from 'lucide-react';
import Link from 'next/link';
import { useLocale, useTranslations } from 'next-intl';
import React from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { FeedImage } from '@/components/feed-image';
import { TitleBarSlot } from '@/components/gadget-title-slot';
import { FeedSourceTabs } from '@/components/feed-source-tabs';
import { FEED_VIEWS, FeedViewToggle, type FeedView } from '@/components/feed-view-toggle';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { safeHttpUrl } from '@/core/safe-url';
import { FEED_LANGUAGES, languageLabel, type FeedStreamItem, type SelectedTelegramIdentity } from './feed-model';
import { formatRelativeTime } from './relative-time';
import { ItemTranslateButton, type ItemTranslationController } from '@/modules/translations';

export { FEED_VIEWS, type FeedView };
export { FeedSourceTabs };

/** Per-render facts every feed row needs. */
export interface FeedRowContext {
  /** Layout of the item stream; defaults to the list view. */
  view?: FeedView;
  isTelegram: boolean;
  /** Workspace owner status; gates the new non-Telegram Ask AI row affordance. */
  isOwner: boolean;
  selectMode: boolean;
  selected: Record<string, SelectedTelegramIdentity>;
  setSelected: React.Dispatch<React.SetStateAction<Record<string, SelectedTelegramIdentity>>>;
  interactionPending: boolean;
  onInteract: (change: { id: string; versionNumber: number; read?: boolean; bookmarked?: boolean }) => void;
  onAskAbout: (items: { sourceId: string; documentId: string; documentVersionId: string }[]) => void;
  /** Optional on-demand document translation controller for the current feed stream. */
  translation?: ItemTranslationController;
}

/** True once the item has any translation outcome (in flight, ready, unchanged or failed) worth showing without hover. */
function hasTranslationState(controller: ItemTranslationController, id: string): boolean {
  return controller.pendingIds.has(id) || controller.results.has(id) || controller.unchanged.has(id) || controller.issues.has(id);
}

/**
 * Always-visible translation state for one row: progress, the Original/Translation toggle, "unchanged" or an error
 * note. Renders nothing while the item is untranslated, so idle rows stay clean and the Translate button lives in the
 * hover-revealed actions. Placed next to the row timestamp because the actions overlay is hidden until hover.
 *
 * @param props.item Feed item; needs `documentVersionId` to be translatable.
 * @param props.ctx Row context carrying the optional translation controller.
 * @param props.className Placement classes for the host layout (grid rows need a full-width cell).
 */
function FeedTranslationState({ item, ctx, className = '' }: { item: FeedStreamItem; ctx: FeedRowContext; className?: string }) {
  if (!ctx.translation || !item.documentVersionId || !hasTranslationState(ctx.translation, item.id)) return null;
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap align-baseline ${className}`}>
      <ItemTranslateButton controller={ctx.translation} target={{ id: item.id, revision: item.documentVersionId }} title={item.title} />
    </span>
  );
}

/** Per-item controls preserve Telegram Ask AI and gate the new ID-only action on owner status for other feeds. */
function FeedItemActions({ item, ctx }: { item: FeedStreamItem; ctx: FeedRowContext }) {
  const t = useTranslations('dashboard');
  const isRead = !!item.read;
  // Translate lives in the hover actions only while idle; any outcome moves to FeedTranslationState beside the time.
  const showTranslate = ctx.translation && item.documentVersionId && !hasTranslationState(ctx.translation, item.id);
  return (
    <div className="absolute right-0 top-0 flex flex-wrap items-center gap-1 bg-card pl-2 text-xs pointer-coarse:static pointer-coarse:w-full pointer-coarse:bg-transparent pointer-coarse:pl-0 pointer-coarse:opacity-100 text-muted-foreground opacity-0 transition-opacity group-hover/item:opacity-100 group-focus-within/item:opacity-100">
      {showTranslate ? (
        <ItemTranslateButton controller={ctx.translation!} target={{ id: item.id, revision: item.documentVersionId! }} title={item.title} />
      ) : null}
      {item.documentVersionId && item.sourceId && (ctx.isTelegram || ctx.isOwner) && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs"
          onClick={() => ctx.onAskAbout([{ sourceId: item.sourceId!, documentId: item.id, documentVersionId: item.documentVersionId! }])}
        >
          {t('askAbout.message')}
        </Button>
      )}
      {item.versionNumber !== undefined && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs"
          disabled={ctx.interactionPending}
          onClick={() => ctx.onInteract({ id: item.id, versionNumber: item.versionNumber!, read: !item.read })}
        >
          {isRead ? t('markUnread') : t('markRead')}
        </Button>
      )}
      {item.versionNumber !== undefined && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-6"
          disabled={ctx.interactionPending}
          onClick={() => ctx.onInteract({ id: item.id, versionNumber: item.versionNumber!, bookmarked: !item.bookmarked })}
          aria-pressed={!!item.bookmarked}
          aria-label={item.bookmarked ? t('removeBookmark') : t('bookmark')}
        >
          <Bookmark className="h-3.5 w-3.5" fill={item.bookmarked ? 'currentColor' : 'none'} aria-hidden="true" />
        </Button>
      )}
      {safeHttpUrl(item.url) && (
        <a
          href={safeHttpUrl(item.url)}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-0.5 font-medium hover:text-foreground"
        >
          <span>{t('source.link')}</span>
          <ExternalLink className="w-2.5 h-2.5" />
        </a>
      )}
      <Link href={`/knowledge/documents/${item.id}`} className="font-medium hover:text-foreground">
        {t('detailLink')}
      </Link>
    </div>
  );
}

/** Selection checkbox; rendered only while selection mode is on, capped at 32 identities. */
function FeedCheckbox({ item, ctx }: { item: FeedStreamItem; ctx: FeedRowContext }) {
  const t = useTranslations('dashboard');
  if (!(ctx.selectMode && item.documentVersionId && item.sourceId)) return null;
  return (
    <Checkbox
      aria-label={t('selectRecord', { title: item.title })}
      checked={Boolean(ctx.selected[item.id])}
      onCheckedChange={(next) =>
        ctx.setSelected((current) => {
          if (next !== true) {
            const remaining = { ...current };
            delete remaining[item.id];
            return remaining;
          }
          if (Object.keys(current).length >= 32) return current;
          return {
            ...current,
            [item.id]: {
              sourceId: item.sourceId!,
              documentId: item.id,
              documentVersionId: item.documentVersionId!,
              versionNumber: item.versionNumber!,
            },
          };
        })
      }
    />
  );
}

/** Single muted meta line: source/channel, then (for Telegram) message id. Time is rendered separately. */
function useMetaParts(isTelegram: boolean) {
  const t = useTranslations('dashboard');
  return (item: FeedStreamItem) =>
    [isTelegram ? item.channelLabel : item.author, isTelegram && item.messageId ? t('telegram.messageId', { id: item.messageId }) : null]
      .filter(Boolean)
      .join(' · ');
}

/** Full timestamps for a tooltip; Telegram folds published/collected/edited lines into one title. */
function useTimeTitle(isTelegram: boolean) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  return (item: FeedStreamItem) =>
    isTelegram
      ? [
          item.publishedAt && t('telegram.publishedAt', { time: formatDateTime(item.publishedAt, display.locale, display.timezone) }),
          item.collectedAt && t('telegram.collectedAt', { time: formatDateTime(item.collectedAt, display.locale, display.timezone) }),
          item.editedAt && t('telegram.editedAt', { time: formatDateTime(item.editedAt, display.locale, display.timezone) }),
        ]
          .filter(Boolean)
          .join('\n')
      : formatDateTime(item.timestamp, display.locale, display.timezone);
}

/** Unread marker: a 6px ink dot with screen-reader text, replacing the old per-item pill. */
function UnreadDot({ item }: { item: FeedStreamItem }) {
  const t = useTranslations('dashboard');
  return item.read ? null : (
    <span className="inline-block size-1.5 shrink-0 self-center rounded-full bg-foreground">
      <span className="sr-only">{t('unread.badge', { count: 1 })}</span>
    </span>
  );
}

/** Literal catalog keys per Telegram media kind (no runtime key concatenation). */
const MEDIA_KIND_KEY = {
  photo: 'telegram.mediaKindPhoto',
  video: 'telegram.mediaKindVideo',
  audio: 'telegram.mediaKindAudio',
  voice: 'telegram.mediaKindVoice',
  document: 'telegram.mediaKindDocument',
  animation: 'telegram.mediaKindAnimation',
  sticker: 'telegram.mediaKindSticker',
  other: 'telegram.mediaKindOther',
} as const;

/** Telegram media placeholder list shared by lead and rows; kinds outside the table render as "other". */
function MediaList({ item, isTelegram }: { item: FeedStreamItem; isTelegram: boolean }) {
  const t = useTranslations('dashboard');
  return isTelegram && item.media && item.media.length > 0 ? (
    <ul aria-label={t('telegram.media')} className="space-y-0.5 text-xs text-muted-foreground">
      {item.media.map((media, index) => (
        <li key={`${media.kind}:${index}`}>
          {t('telegram.mediaPlaceholder', {
            kind: t(MEDIA_KIND_KEY[media.kind as keyof typeof MEDIA_KIND_KEY] ?? MEDIA_KIND_KEY.other),
            count: media.count,
          })}
          {media.caption ? ` · ${media.caption}` : ''}
        </li>
      ))}
    </ul>
  ) : null;
}

/** The first item: a compact row that becomes a large lead on wide gadgets (container query). */
function FeedLead({ item, ctx }: { item: FeedStreamItem; ctx: FeedRowContext }) {
  const display = useDisplayPreferences();
  const metaParts = useMetaParts(ctx.isTelegram);
  const timeTitle = useTimeTitle(ctx.isTelegram);
  const translated = ctx.translation?.shown(item.id);
  const title = translated?.title ?? item.title;
  const excerpt = translated?.excerpt ?? item.excerpt;
  return (
    <article
      data-testid="feed-lead"
      className="group/item relative flex min-w-0 items-baseline gap-2 py-[var(--gadget-row-y)] @[40rem]/gadget:block @[40rem]/gadget:py-0 @[40rem]/gadget:pr-2"
    >
      <FeedCheckbox item={item} ctx={ctx} />
      <div className="flex min-w-0 flex-1 items-baseline gap-2 @[40rem]/gadget:block">
        <FeedImage src={item.imageUrl} className="mb-2 hidden @[40rem]/gadget:block" />
        <h3
          className={`flex min-w-0 flex-1 items-baseline gap-1.5 text-sm leading-snug @[40rem]/gadget:text-xl ${item.read ? 'font-normal text-muted-foreground' : 'font-semibold text-foreground'}`}
        >
          <UnreadDot item={item} />
          <Link
            href={`/knowledge/documents/${item.id}`}
            className="min-w-0 truncate hover:underline @[40rem]/gadget:line-clamp-2 @[40rem]/gadget:whitespace-normal"
          >
            {title}
          </Link>
        </h3>
        {excerpt && (
          <p className="mt-2 hidden line-clamp-3 text-sm leading-relaxed text-muted-foreground @[40rem]/gadget:block">{excerpt}</p>
        )}
        <p className="shrink-0 text-xs text-muted-foreground @[40rem]/gadget:mt-2">
          {metaParts(item) ? `${metaParts(item)} · ` : ''}
          <time className="tabular-nums" dateTime={item.timestamp} title={timeTitle(item)}>
            {formatRelativeTime(item.timestamp, display.locale)}
          </time>{' '}
          <FeedTranslationState item={item} ctx={ctx} />
        </p>
        <div className="hidden @[40rem]/gadget:block">
          <MediaList item={item} isTelegram={ctx.isTelegram} />
        </div>
      </div>
      <FeedItemActions item={item} ctx={ctx} />
    </article>
  );
}

/**
 * Telegram message row (developer-theme `.dash-msg`): unread dot, channel name and clock on the first
 * line, the message text on the second. Keeps the read/bookmark actions and selection checkbox of the standard row.
 *
 * @param props.item Message to show; `props.ctx` supplies selection and interaction handlers.
 */
function TerminalMessageRow({ item, ctx }: { item: FeedStreamItem; ctx: FeedRowContext }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const timeTitle = useTimeTitle(ctx.isTelegram);
  const at = new Date(item.timestamp);
  const clock = Number.isNaN(at.getTime())
    ? ''
    : new Intl.DateTimeFormat(display.locale, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: display.timezone }).format(
        at,
      );
  return (
    <li className="msg-row group/item relative" data-read={item.read ? 'true' : 'false'}>
      <FeedCheckbox item={item} ctx={ctx} />
      <span className="msg-dot" aria-hidden="true">
        {item.read ? '○' : '●'}
      </span>
      {item.read ? null : <span className="sr-only">{t('unread.badge', { count: 1 })}</span>}
      <span className="msg-from">{item.channelLabel ?? item.author ?? ''}</span>
      <time className="msg-time" dateTime={item.timestamp} title={timeTitle(item)}>
        {clock}
      </time>
      <Link href={`/knowledge/documents/${item.id}`} className="msg-body">
        {ctx.translation?.shown(item.id)?.title ?? item.title}
      </Link>
      {/* Grid row: give the state its own full-width line under the message, like the photo below. */}
      <FeedTranslationState item={item} ctx={ctx} className="col-start-2 col-end-[-1] mt-0.5" />
      {item.imageUrl ? (
        // Post photo (public Telegram channels): a small fixed-width thumbnail under the message line.
        <div className="col-start-2 col-end-[-1] mt-1 w-40 max-w-full">
          <FeedImage src={item.imageUrl} />
        </div>
      ) : null}
      <FeedItemActions item={item} ctx={ctx} />
    </li>
  );
}

/** One non-lead list row. */
function FeedRow({ item, ctx }: { item: FeedStreamItem; ctx: FeedRowContext }) {
  const display = useDisplayPreferences();
  const metaParts = useMetaParts(ctx.isTelegram);
  const timeTitle = useTimeTitle(ctx.isTelegram);
  return (
    <li className="group/item relative flex flex-wrap items-baseline gap-2 py-[var(--gadget-row-y)]">
      <FeedCheckbox item={item} ctx={ctx} />
      <UnreadDot item={item} />
      <div className="min-w-0 flex-1">
        <Link
          href={`/knowledge/documents/${item.id}`}
          className={`block truncate text-[length:var(--gadget-font)] leading-snug hover:underline ${item.read ? 'font-normal text-muted-foreground' : 'font-semibold text-foreground'}`}
        >
          {ctx.translation?.shown(item.id)?.title ?? item.title}
        </Link>
        {ctx.isTelegram && metaParts(item) && <p className="truncate text-xs text-muted-foreground">{metaParts(item)}</p>}
        <MediaList item={item} isTelegram={ctx.isTelegram} />
      </div>
      <time className="shrink-0 text-xs tabular-nums text-muted-foreground" dateTime={item.timestamp} title={timeTitle(item)}>
        {formatRelativeTime(item.timestamp, display.locale)}
      </time>
      <FeedTranslationState item={item} ctx={ctx} />
      <FeedItemActions item={item} ctx={ctx} />
    </li>
  );
}

/** One card of the Cards view: optional image, title, excerpt and meta. */
function FeedCard({ item, ctx }: { item: FeedStreamItem; ctx: FeedRowContext }) {
  const display = useDisplayPreferences();
  const metaParts = useMetaParts(ctx.isTelegram);
  const timeTitle = useTimeTitle(ctx.isTelegram);
  const translated = ctx.translation?.shown(item.id);
  const title = translated?.title ?? item.title;
  const excerpt = translated?.excerpt ?? item.excerpt;
  return (
    <li className="group/item relative flex min-w-0 flex-col gap-2 rounded-lg border border-border p-[var(--gadget-pad)]">
      <FeedCheckbox item={item} ctx={ctx} />
      <FeedImage src={item.imageUrl} />
      <h3
        className={`flex min-w-0 items-baseline gap-1.5 text-[length:var(--gadget-font)] leading-snug ${item.read ? 'font-normal text-muted-foreground' : 'font-semibold text-foreground'}`}
      >
        <UnreadDot item={item} />
        <Link href={`/knowledge/documents/${item.id}`} className="min-w-0 line-clamp-2 hover:underline">
          {title}
        </Link>
      </h3>
      {excerpt && <p className="line-clamp-2 text-xs leading-relaxed text-muted-foreground">{excerpt}</p>}
      <p className="text-xs text-muted-foreground">
        {metaParts(item) ? `${metaParts(item)} · ` : ''}
        <time className="tabular-nums" dateTime={item.timestamp} title={timeTitle(item)}>
          {formatRelativeTime(item.timestamp, display.locale)}
        </time>{' '}
        <FeedTranslationState item={item} ctx={ctx} />
      </p>
      <MediaList item={item} isTelegram={ctx.isTelegram} />
      <FeedItemActions item={item} ctx={ctx} />
    </li>
  );
}

/** Lead + list (or a responsive card grid): one DOM per item; the container query turns the first item into a lead on wide gadgets. */
export function FeedItems({ items, ctx }: { items: FeedStreamItem[]; ctx: FeedRowContext }) {
  const [lead, ...rest] = items;
  if (!lead) return null;
  // Telegram feeds are plain two-line log rows: no wide-gadget lead headline.
  if (ctx.isTelegram && ctx.view !== 'cards') {
    return (
      <ul className="msg-log min-w-0">
        {items.map((item) => (
          <TerminalMessageRow key={item.id} item={item} ctx={ctx} />
        ))}
      </ul>
    );
  }
  if (ctx.view === 'cards') {
    return (
      <ul data-testid="feed-cards" className="grid min-h-0 grid-cols-1 gap-3 @[28rem]/gadget:grid-cols-2 @[56rem]/gadget:grid-cols-3">
        {items.map((item) => (
          <FeedCard key={item.id} item={item} ctx={ctx} />
        ))}
      </ul>
    );
  }
  return (
    <div className="grid min-h-0 gap-x-6 gap-y-1 @[40rem]/gadget:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <FeedLead item={lead} ctx={ctx} />
      <ul className="min-w-0 divide-y divide-border/50">
        {rest.map((item) => (
          <FeedRow key={item.id} item={item} ctx={ctx} />
        ))}
      </ul>
    </div>
  );
}

/** Props of the filter popover. */
export interface FeedFilterProps {
  searchText: string;
  setSearchText: (value: string) => void;
  sourceIds: string[];
  sourceFilter: string;
  setSourceFilter: (value: string) => void;
  sourceNames: { id: string; name: string }[] | undefined;
  language: string;
  setLanguage: (value: string) => void;
  timeWindow: string;
  setTimeWindow: (value: string) => void;
  activeFilterCount: number;
  filtering: boolean;
}

/** Filter popover: search text, source, language and time window. */
export function FeedFilterPopover(props: FeedFilterProps) {
  const t = useTranslations('dashboard');
  const locale = useLocale();
  const { sourceIds, activeFilterCount } = props;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button type="button" variant="ghost" size="sm" className="feed-tool-opt h-8 gap-1 px-2 text-xs">
          <ListFilter className="h-3.5 w-3.5" aria-hidden="true" />
          {/* The title-bar copy keeps the label for assistive tech only; the icon alone shows in the title bar. */}
          <span className="feed-tool-label">
            {activeFilterCount > 0 ? t('feed.filterActive', { count: activeFilterCount }) : t('feed.filter')}
          </span>
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72 space-y-2 p-3">
        <Input
          type="search"
          value={props.searchText}
          onChange={(event) => props.setSearchText(event.target.value)}
          aria-label={t('feed.search')}
          placeholder={t('feed.search')}
          className="h-9 text-xs"
        />
        {sourceIds.length > 1 && (
          <Select value={props.sourceFilter} onValueChange={props.setSourceFilter}>
            <SelectTrigger aria-label={t('feed.sourceLabel')} className="h-9 w-full text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" className="text-xs">
                {t('feed.sourceAll')}
              </SelectItem>
              {sourceIds.map((id) => (
                <SelectItem key={id} value={id} className="text-xs">
                  {props.sourceNames?.find((source) => source.id === id)?.name ?? t('feed.sourceUnknown')}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <Select value={props.language} onValueChange={props.setLanguage}>
          <SelectTrigger aria-label={t('feed.languageLabel')} className="h-9 w-full text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="any" className="text-xs">
              {t('feed.languageAny')}
            </SelectItem>
            {FEED_LANGUAGES.map((code) => (
              <SelectItem key={code} value={code} className="text-xs">
                {languageLabel(locale, code)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={props.timeWindow} onValueChange={props.setTimeWindow}>
          <SelectTrigger aria-label={t('feed.windowLabel')} className="h-9 w-full text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="any" className="text-xs">
              {t('feed.windowAny')}
            </SelectItem>
            <SelectItem value="24h" className="text-xs">
              {t('feed.window24h')}
            </SelectItem>
            <SelectItem value="7d" className="text-xs">
              {t('feed.window7d')}
            </SelectItem>
            <SelectItem value="30d" className="text-xs">
              {t('feed.window30d')}
            </SelectItem>
          </SelectContent>
        </Select>
        {props.filtering && <p className="text-xs text-muted-foreground">{t('feed.filtersNote')}</p>}
      </PopoverContent>
    </Popover>
  );
}

/** Props of {@link FeedToolbar}. */
export interface FeedToolbarProps {
  onlyUnread: boolean;
  toggleUnread: () => void;
  unreadCount: number;
  volume: React.ReactNode;
  filters: React.ReactNode;
  canSelect: boolean;
  selectMode: boolean;
  toggleSelectMode: () => void;
  refreshing: boolean;
  onRefresh: () => void;
  /** Current item layout and its setter (List | Cards toggle). */
  view: FeedView;
  setView: (view: FeedView) => void;
}

/** One quiet toolbar: unread toggle, volume spark, Filter popover, selection mode, refresh. It renders in the gadget title bar (`TitleBarSlot`); in narrow panes the List | Cards toggle moves from the title bar to a body row (CSS decides which copy shows), so no control is lost. */
export function FeedToolbar(props: FeedToolbarProps) {
  const t = useTranslations('dashboard');
  const { onlyUnread, selectMode, refreshing } = props;
  return (
    <>
      {/* Narrow panes: the title-bar copy of the view toggle is hidden by CSS (`.feed-view-slot`) so the meta and
        `[ collect now ]` keep their room; this body-row copy (shown only when narrow) keeps list/cards reachable. */}
      <div className="feed-view-narrow">
        <FeedViewToggle view={props.view} setView={props.setView} />
      </div>
      <TitleBarSlot>
        <div className="gadget-toolbar flex items-center justify-between gap-2 text-xs">
          <Button
            type="button"
            variant={onlyUnread ? 'default' : 'ghost'}
            size="sm"
            className="h-8 px-2 text-xs"
            aria-pressed={onlyUnread}
            onClick={props.toggleUnread}
          >
            {t('unread.only')} ({props.unreadCount})
          </Button>

          <div className="flex min-w-0 items-center gap-1">
            {props.volume}
            {props.filters}
            <div className="feed-view-slot">
              <FeedViewToggle view={props.view} setView={props.setView} />
            </div>
            {props.canSelect && (
              <Button
                type="button"
                variant={selectMode ? 'default' : 'ghost'}
                size="sm"
                className="feed-tool-opt h-8 px-2 text-xs"
                aria-pressed={selectMode}
                onClick={props.toggleSelectMode}
              >
                <SquareCheck className="feed-tool-icon h-3.5 w-3.5" aria-hidden="true" />
                <span className="feed-tool-label">{selectMode ? t('feed.selectDone') : t('feed.selectMode')}</span>
              </Button>
            )}
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="feed-tool-opt size-8"
              onClick={props.onRefresh}
              disabled={refreshing}
              aria-label={t('feed.refresh')}
            >
              <RotateCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} aria-hidden="true" />
            </Button>
          </div>
        </div>
      </TitleBarSlot>
    </>
  );
}

/** Props of {@link FeedBulkBar}. */
export interface FeedBulkBarProps {
  items: SelectedTelegramIdentity[];
  interactionPending: boolean;
  onAskAbout: FeedRowContext['onAskAbout'];
  onInteract: (change: { id: string; versionNumber: number; read?: boolean; bookmarked?: boolean }) => void;
  onHide: (items: SelectedTelegramIdentity[]) => void;
  onClear: () => void;
}

/** Bulk bar for the selected Telegram records: ask AI, mark read, save, hide, clear. */
export function FeedBulkBar({ items, interactionPending, onAskAbout, onInteract, onHide, onClear }: FeedBulkBarProps) {
  const t = useTranslations('dashboard');
  if (items.length === 0) return null;
  return (
    <div
      role="group"
      aria-label={t('feed.bulkBar')}
      className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background p-2 text-xs"
    >
      <span className="font-semibold" role="status">
        {t('feed.selectedCount', { count: items.length })}
      </span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() =>
          onAskAbout(items.map(({ sourceId, documentId, documentVersionId }) => ({ sourceId, documentId, documentVersionId })))
        }
      >
        {t('askAbout.selected', { count: items.length })}
      </Button>
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={interactionPending}
        onClick={() => items.forEach((item) => onInteract({ id: item.documentId, versionNumber: item.versionNumber, read: true }))}
      >
        {t('feed.markRead')}
      </Button>
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={interactionPending}
        onClick={() => items.forEach((item) => onInteract({ id: item.documentId, versionNumber: item.versionNumber, bookmarked: true }))}
      >
        {t('feed.save')}
      </Button>
      <Button type="button" size="sm" variant="outline" onClick={() => onHide(items)}>
        {t('feed.notRelevant')}
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={onClear}>
        {t('feed.clear')}
      </Button>
    </div>
  );
}

/** Props of {@link FeedUndoBar}. */
export interface FeedUndoBarProps {
  lastHidden: SelectedTelegramIdentity[];
  undoFailed: boolean;
  hideFailed: boolean;
  interactionPending: boolean;
  undoRef: React.RefObject<HTMLButtonElement | null>;
  onUndo: () => void;
}

/** Undo bar for "Not relevant" plus the hide / undo failure alerts. */
export function FeedUndoBar({ lastHidden, undoFailed, hideFailed, interactionPending, undoRef, onUndo }: FeedUndoBarProps) {
  const t = useTranslations('dashboard');
  return (
    <>
      {/* The live region stays mounted so screen readers announce the text when it changes. */}
      <div
        className={lastHidden.length > 0 ? 'flex items-center gap-2 rounded-md border border-border bg-background p-2 text-xs' : 'sr-only'}
      >
        <span role="status" aria-live="polite">
          {lastHidden.length > 0 ? t('feed.hiddenCount', { count: lastHidden.length }) : ''}
        </span>
        {lastHidden.length > 0 && (
          <Button
            ref={undoRef}
            type="button"
            size="sm"
            variant="ghost"
            aria-disabled={interactionPending || undefined}
            onClick={() => {
              if (!interactionPending) onUndo();
            }}
          >
            {undoFailed ? t('feed.undoRetry') : t('feed.undo')}
          </Button>
        )}
      </div>
      {hideFailed && (
        <p role="alert" className="text-xs text-destructive">
          {t('feed.hideError')}
        </p>
      )}
      {undoFailed && (
        <p role="alert" className="text-xs text-destructive">
          {t('feed.undoError')}
        </p>
      )}
    </>
  );
}
