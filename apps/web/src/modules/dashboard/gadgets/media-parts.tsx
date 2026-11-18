'use client';

import { useId, useRef, useState } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useLocale, useTranslations } from 'next-intl';
import Link from 'next/link';
import { ListFilter, RotateCw, Sparkles, X } from 'lucide-react';
import { FeedImage } from '@/components/feed-image';
import { FeedSourceTabs } from '@/components/feed-source-tabs';
import { FEED_VIEWS, FeedViewToggle, type FeedView } from '@/components/feed-view-toggle';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { BareButton } from '@/components/bare-button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { apiRequest } from '@/core/api';
import { useChatController } from '@/core/app-shell/chat-controller';
import { useDisplayPreferences } from '@/core/query-provider';
import { useWorkspace } from '@/core/workspace-context';
import { formatDate } from '@/core/format';
import type { AppLocaleId } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import { useLocalPref } from '@/core/use-local-pref';
import {
  documentKeys,
  listGadgetDocumentProjections,
  type GadgetDocumentProjection,
  type GadgetProjectionFilters,
} from '@/modules/knowledge/public-api';
import { dashboardKeys, listGadgetSources, type GadgetDefinition, type GadgetInstance } from '../api';
import {
  ANIMESEASON,
  CONTENTRATING,
  GADGETRELEASESTATUSFILTER,
  MEDIAFORMAT,
  MEDIAGENRE,
  MEDIALANGUAGE,
  MEDIACOUNTRY,
  MEDIATYPE,
  MEDIA_VALUE_KEY,
} from '@/core/media-options';
import { MEDIA_SELECTOR_LIMITS } from '../gadget-editor-model';
import { MediaMultiChipPicker } from '../gadget-editor-pickers';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { FeedUndoBar } from './feed-parts';
import type { SelectedTelegramIdentity } from './feed-model';
import { useFeedModeration } from './use-feed-state';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';

/** Literal labels for persisted media fields. */
const MEDIA_FIELD_KEY = {
  media_types: 'media.media_types',
  countries: 'media.countries',
  genres: 'media.genres',
  languages: 'media.languages',
  formats: 'media.formats',
  content_ratings: 'media.content_ratings',
  release_year: 'media.release_year',
  release_season: 'media.release_season',
  min_rating: 'media.min_rating',
  release_window_days: 'media.release_window_days',
  media_type: 'media.media_type',
  release_status: 'media.release_status',
  group_by: 'media.group_by',
} as const;

/** Selector lists use populated filter overrides; null scalars fall back to scope, including a valid zero rating. */
export function mediaDefaults(definition: GadgetDefinition): GadgetProjectionFilters {
  const { scope, filters } = definition;
  const result: GadgetProjectionFilters = {};
  for (const key of ['media_types', 'countries', 'genres', 'languages', 'formats', 'content_ratings'] as const) {
    result[key] = [...(filters[key]?.length ? filters[key]! : (scope[key] ?? []))];
  }
  result.release_year = filters.release_year ?? scope.release_year ?? undefined;
  result.release_season = filters.release_season ?? scope.release_season ?? undefined;
  result.min_rating = filters.min_rating ?? scope.min_rating ?? undefined;
  return result;
}

/** A compact labelled Select shared by quick filters and the advanced popover. */
function MediaSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger aria-label={label} className="h-8 w-auto min-w-24 text-xs pointer-coarse:min-h-11">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** Toolbar/advanced/chip filter state is renderer-owned so the frame can reparent it without resetting it. */
export interface MediaFilterState {
  filters: GadgetProjectionFilters;
  mediaType: string;
  releaseStatus: string;
  search: string;
  onlyUnread: boolean;
  source: string;
  window: number;
}

/** Labels canonical enum values using literal catalog keys; provider values remain visible without translation. */
function useMediaLabel() {
  const t = useTranslations('gadgetSettings');
  return (value: string) => (Object.hasOwn(MEDIA_VALUE_KEY, value) ? t(MEDIA_VALUE_KEY[value as keyof typeof MEDIA_VALUE_KEY]) : value);
}

/** Counts/removes effective server selectors as well as interactive quick filters without restoring hidden defaults. */
export function mediaFilterChips(state: MediaFilterState): { key: string; value: string; remove: MediaFilterState }[] {
  const chips: { key: string; value: string; remove: MediaFilterState }[] = [];
  for (const key of ['media_types', 'countries', 'genres', 'languages', 'formats', 'content_ratings'] as const) {
    for (const value of state.filters[key] ?? [])
      chips.push({
        key,
        value,
        remove: { ...state, filters: { ...state.filters, [key]: state.filters[key]!.filter((item) => item !== value) } },
      });
  }
  for (const key of ['release_year', 'release_season', 'min_rating'] as const) {
    if (state.filters[key] != null)
      chips.push({ key, value: String(state.filters[key]), remove: { ...state, filters: { ...state.filters, [key]: undefined } } });
  }
  if (state.mediaType !== 'all') chips.push({ key: 'media_type', value: state.mediaType, remove: { ...state, mediaType: 'all' } });
  if (state.releaseStatus !== 'all')
    chips.push({ key: 'release_status', value: state.releaseStatus, remove: { ...state, releaseStatus: 'all' } });
  if (state.search) chips.push({ key: 'search', value: state.search, remove: { ...state, search: '' } });
  return chips;
}

/** Effective filter state and its renderer-owned update callback. */
interface MediaFilterControls {
  state: MediaFilterState;
  setState: (state: MediaFilterState) => void;
}

/** Bounded searchable selectors reuse the editor's API limits and country normalization. */
function MediaSelectorFields({ state, setState }: MediaFilterControls) {
  const settings = useTranslations('gadgetSettings');
  const locale = useLocale();
  const id = useId();
  const label = useMediaLabel();
  const lists = [
    ['media_types', MEDIATYPE],
    ['countries', MEDIACOUNTRY],
    ['genres', MEDIAGENRE],
    ['languages', MEDIALANGUAGE],
    ['formats', MEDIAFORMAT],
    ['content_ratings', CONTENTRATING],
  ] as const;
  const countryNames = new Intl.DisplayNames([locale], { type: 'region' });
  return (
    <>
      {lists.map(([field, options]) => (
        <div key={field} className="space-y-1">
          <Label id={`${id}-${field}`}>{settings(MEDIA_FIELD_KEY[field])}</Label>
          <MediaMultiChipPicker
            value={state.filters[field] ?? []}
            options={options}
            maxItems={MEDIA_SELECTOR_LIMITS[field]}
            country={field === 'countries'}
            labelId={`${id}-${field}`}
            label={(value) => (field === 'countries' ? `${countryNames.of(value) ?? value} (${value})` : label(value))}
            onChange={(next) => setState({ ...state, filters: { ...state.filters, [field]: next } })}
          />
        </div>
      ))}
    </>
  );
}

/** Numeric drafts commit on blur after validation; reset and chip removal restore the external value. */
function MediaNumericFields({ state, setState }: MediaFilterControls) {
  const settings = useTranslations('gadgetSettings');
  const id = useId();
  return (
    <>
      {/* Commit numeric drafts on blur so intermediate digits never reset focus or reach the server. */}
      {(['release_year', 'min_rating'] as const).map((field) => (
        <div key={field} className="space-y-1">
          <Label htmlFor={`${id}-${field}`}>{settings(MEDIA_FIELD_KEY[field])}</Label>
          <Input
            id={`${id}-${field}`}
            key={`${field}:${state.filters[field] ?? 'unset'}`}
            type="number"
            min={field === 'release_year' ? 1800 : 0}
            max={field === 'release_year' ? 2100 : 100}
            step={field === 'release_year' ? 1 : 'any'}
            defaultValue={state.filters[field] ?? ''}
            onBlur={(event) => {
              const raw = event.target.value;
              const value = Number(raw);
              if (
                !raw ||
                (Number.isFinite(value) &&
                  value >= (field === 'release_year' ? 1800 : 0) &&
                  value <= (field === 'release_year' ? 2100 : 100) &&
                  (field !== 'release_year' || Number.isInteger(value)))
              )
                setState({ ...state, filters: { ...state.filters, [field]: raw ? value : undefined } });
              else event.target.value = String(state.filters[field] ?? '');
            }}
          />
        </div>
      ))}
    </>
  );
}

/** Advanced controls edit the same renderer state as the toolbar and removable chips. */
function MediaFilterPopover({
  state,
  setState,
  defaults,
  sources,
  clear,
}: MediaFilterControls & {
  defaults: MediaFilterState;
  sources: { id: string; name: string }[];
  clear: () => void;
}) {
  const t = useTranslations('dashboard');
  const settings = useTranslations('gadgetSettings');
  const id = useId();
  const label = useMediaLabel();
  const chips = mediaFilterChips(state);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button type="button" size="sm" variant="ghost" className="h-8 px-2 text-xs pointer-coarse:min-h-11">
          <ListFilter className="size-3.5" aria-hidden="true" />
          {chips.length ? t('feed.filterActive', { count: chips.length }) : t('feed.filter')}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="max-h-[70dvh] w-80 space-y-3 overflow-y-auto p-3">
        <div className="space-y-1">
          <Label htmlFor={`${id}-search`}>{t('feed.search')}</Label>
          <Input
            id={`${id}-search`}
            type="search"
            value={state.search}
            onChange={(event) => setState({ ...state, search: event.target.value })}
          />
        </div>
        {sources.length > 1 && (
          <MediaSelect
            label={t('feed.sourceLabel')}
            value={state.source}
            options={[{ value: 'all', label: t('feed.sourceAll') }, ...sources.map((source) => ({ value: source.id, label: source.name }))]}
            onChange={(source) => setState({ ...state, source })}
          />
        )}
        <MediaSelectorFields state={state} setState={setState} />
        <MediaNumericFields state={state} setState={setState} />
        <MediaSelect
          label={settings('media.release_season')}
          value={state.filters.release_season ?? 'all'}
          options={['all', ...ANIMESEASON].map((value) => ({ value, label: label(value) }))}
          onChange={(release_season) =>
            setState({ ...state, filters: { ...state.filters, release_season: release_season === 'all' ? undefined : release_season } })
          }
        />
        <div className="flex flex-wrap gap-1">
          <Button type="button" size="sm" variant="ghost" onClick={() => setState(defaults)}>
            {t('media.reset')}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={clear}>
            {t('media.clear')}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Removable effective selectors never silently reapply inherited scope values. */
function MediaActiveChips({ state, setState, clear }: MediaFilterControls & { clear: () => void }) {
  const t = useTranslations('dashboard');
  const settings = useTranslations('gadgetSettings');
  const label = useMediaLabel();
  const chips = mediaFilterChips(state);
  return (
    <>
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {chips.map((chip) => (
            <Badge key={`${chip.key}:${chip.value}`} variant="outline" className="gap-1">
              {chip.key === 'search' ? t('feed.search') : settings(MEDIA_FIELD_KEY[chip.key as keyof typeof MEDIA_FIELD_KEY])}:{' '}
              {label(chip.value)}
              <BareButton
                type="button"
                aria-label={t('media.removeFilter', { value: label(chip.value) })}
                onClick={() => setState(chip.remove)}
                className="inline-flex min-h-6 min-w-6 items-center justify-center pointer-coarse:min-h-11 pointer-coarse:min-w-11"
              >
                <X className="size-3" aria-hidden="true" />
              </BareButton>
            </Badge>
          ))}
          <Button type="button" size="sm" variant="ghost" onClick={clear}>
            {t('media.clear')}
          </Button>
        </div>
      )}
    </>
  );
}

/** Shared multi-dimensional filters use the editor's bounded, searchable chip picker and preserve separate singular media type. */
export function MediaFilterBar({
  state,
  setState,
  defaults,
  calendar,
  refreshing,
  onRefresh,
  view,
  setView,
  sources,
}: {
  state: MediaFilterState;
  setState: (state: MediaFilterState) => void;
  defaults: MediaFilterState;
  calendar: boolean;
  refreshing: boolean;
  onRefresh: () => void;
  view: FeedView;
  setView: (view: FeedView) => void;
  sources: { id: string; name: string }[];
}) {
  const t = useTranslations('dashboard');
  const settings = useTranslations('gadgetSettings');
  const label = useMediaLabel();
  /** Clearing filters removes inherited selectors until Reset explicitly reinstates the saved defaults. */
  const clear = () =>
    setState({ ...state, filters: {}, mediaType: 'all', releaseStatus: 'all', search: '', onlyUnread: false, source: 'all' });
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1">
        <MediaSelect
          label={settings('media.media_type')}
          value={state.mediaType}
          options={['all', ...MEDIATYPE].map((value) => ({ value, label: label(value) }))}
          onChange={(mediaType) => setState({ ...state, mediaType })}
        />
        <MediaSelect
          label={settings('media.release_status')}
          value={state.releaseStatus}
          options={GADGETRELEASESTATUSFILTER.map((value) => ({ value, label: label(value) }))}
          onChange={(releaseStatus) => setState({ ...state, releaseStatus })}
        />
        {calendar && (
          <MediaSelect
            label={settings('media.release_window_days')}
            value={String(state.window)}
            options={[...new Set([7, 14, 30, 90, state.window])]
              .sort((a, b) => a - b)
              .map((value) => ({ value: String(value), label: t('media.windowDays', { count: value }) }))}
            onChange={(window) => setState({ ...state, window: Number(window) })}
          />
        )}
        <MediaFilterPopover state={state} setState={setState} defaults={defaults} sources={sources} clear={clear} />
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-8 text-xs pointer-coarse:min-h-11"
          aria-pressed={state.onlyUnread}
          onClick={() => setState({ ...state, onlyUnread: !state.onlyUnread })}
        >
          {t('unread.only')}
        </Button>
        {!calendar && <FeedViewToggle view={view} setView={setView} />}
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-8 pointer-coarse:min-h-11 pointer-coarse:min-w-11"
          aria-label={t('feed.refresh')}
          disabled={refreshing}
          onClick={onRefresh}
        >
          <RotateCw className="size-3.5" aria-hidden="true" />
        </Button>
      </div>
      <MediaActiveChips state={state} setState={setState} clear={clear} />
    </div>
  );
}

/** Immutable selection identity; no excerpts or provider URLs enter chat grounding. Server fences enforce ownership and local-only egress. */
export function mediaIdentity(item: GadgetDocumentProjection): SelectedTelegramIdentity {
  return {
    sourceId: item.source_id,
    documentId: item.document_id,
    documentVersionId: item.document_version_id,
    versionNumber: item.version_number,
  };
}

/** Metadata badges consume only the typed projection, never parse provider excerpts. */
export function MediaMetadata({ item }: { item: GadgetDocumentProjection }) {
  const t = useTranslations('dashboard');
  const label = useMediaLabel();
  const m = item.media;
  if (!m) return null;
  const badges = [
    label(m.media_type),
    m.country,
    m.language && label(m.language),
    ...m.genres.map(label),
    m.format && label(m.format),
    m.content_rating && label(m.content_rating),
    m.release_status && label(m.release_status),
    m.season && t('media.season', { value: m.season }),
    m.episode && t('media.episode', { value: m.episode }),
    m.chapter && t('media.chapter', { value: m.chapter }),
    m.volume && t('media.volume', { value: m.volume }),
    m.page_count != null && t('media.pages', { count: m.page_count }),
    m.duration_seconds != null && t('media.minutes', { count: Math.ceil(m.duration_seconds / 60) }),
    m.rating != null && t('media.score', { value: m.rating }),
  ].filter((value) => typeof value === 'string');
  return (
    <div className="flex flex-wrap gap-1">
      {badges.map((value, index) => (
        <Badge key={`${index}:${value}`} variant="outline" className="text-xs">
          {value}
        </Badge>
      ))}
    </div>
  );
}

/** Card inputs preserve immutable-version writes, ownership and bounded selection callbacks. */
interface MediaCardProps {
  item: GadgetDocumentProjection;
  canInteract: boolean;
  view: FeedView;
  selected: boolean;
  onSelect: (checked: boolean) => void;
  pending: boolean;
  onInteract: (change: { id: string; versionNumber: number; read?: boolean; bookmarked?: boolean }) => void;
  onHide: () => void;
  onAsk: () => void;
}

/** Owner-only actions reuse serialized moderation; external trailers remain safe HTTP links. */
function MediaActions({
  item,
  canInteract,
  pending,
  onInteract,
  onHide,
  onAsk,
}: Pick<MediaCardProps, 'item' | 'canInteract' | 'pending' | 'onInteract' | 'onHide' | 'onAsk'>) {
  const t = useTranslations('dashboard');
  const trailer = safeHttpUrl(item.media?.trailer_url);
  return (
    <div className="flex flex-wrap gap-1">
      {canInteract && (
        <>
          <Button type="button" variant="ghost" size="sm" onClick={onAsk}>
            <Sparkles className="size-3.5" aria-hidden="true" />
            {t('media.ask')}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={pending}
            onClick={() => onInteract({ id: item.document_id, versionNumber: item.version_number, read: !item.read_at })}
          >
            {item.read_at ? t('markUnread') : t('markRead')}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={pending}
            aria-pressed={!!item.bookmarked_at}
            onClick={() => onInteract({ id: item.document_id, versionNumber: item.version_number, bookmarked: !item.bookmarked_at })}
          >
            {item.bookmarked_at ? t('removeBookmark') : t('bookmark')}
          </Button>
          <Button type="button" variant="ghost" size="sm" disabled={pending} onClick={onHide}>
            {t('feed.notRelevant')}
          </Button>
        </>
      )}
      {trailer && (
        <Button asChild variant="ghost" size="sm">
          <a href={trailer} target="_blank" rel="noopener noreferrer">
            {t('media.trailer')}
          </a>
        </Button>
      )}
    </div>
  );
}

/** Adaptive poster/cover/video card with bounded selection, immutable-version actions and safe outbound links. */
export function MediaCard({ item, view, selected, onSelect, pending, onInteract, onHide, onAsk, canInteract }: MediaCardProps) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const url = safeHttpUrl(item.canonical_url);
  return (
    <article
      className={`min-w-0 space-y-2 rounded-md border border-line bg-surface p-2 ${view === 'list' ? 'flex items-start gap-3' : ''}`}
    >
      <div className={view === 'list' ? 'w-16 shrink-0' : ''}>
        <FeedImage
          key={item.image_url}
          src={item.image_url}
          aspect={item.media?.artwork_aspect ?? 'video'}
          className="border border-line bg-surface"
        />
      </div>
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex items-start gap-2">
          {canInteract && (
            <Checkbox
              aria-label={t('selectRecord', { title: item.title })}
              checked={selected}
              onCheckedChange={(checked) => onSelect(checked === true)}
            />
          )}
          <h3 className={`text-sm ${item.read_at ? 'text-muted-foreground' : 'font-semibold'}`}>
            {url ? (
              <a href={url} target="_blank" rel="noopener noreferrer" className="hover:underline">
                {item.title}
              </a>
            ) : (
              item.title
            )}
          </h3>
        </div>
        {item.media?.creator && <p className="text-xs text-muted-foreground">{item.media.creator}</p>}
        {item.media?.release_date && (
          <time className="text-xs text-muted-foreground" dateTime={item.media.release_date}>
            {formatDate(item.media.release_date, display.locale, releaseTimeZone(item.media.release_date, display.timezone))}
          </time>
        )}
        <MediaMetadata item={item} />
        {item.media?.attribution && <p className="text-xs text-muted-foreground">{item.media.attribution}</p>}
        <MediaActions item={item} canInteract={canInteract} pending={pending} onInteract={onInteract} onHide={onHide} onAsk={onAsk} />
      </div>
    </article>
  );
}

/** Group labels and records are rendered in owner-selected order. */
interface MediaGroup {
  key: string;
  label: string;
  items: GadgetDocumentProjection[];
}
/** Shelf/calendar rendering share one source and interaction owner. */
interface MediaCollectionProps {
  instance: GadgetInstance;
  calendar?: boolean;
  groupItems?: (items: GadgetDocumentProjection[], timezone: string) => MediaGroup[];
}

/** Revision-scoped collection inputs keep the display preference in the stable outer owner. */
interface MediaCollectionBodyProps extends MediaCollectionProps {
  view: FeedView;
  setView: (view: FeedView) => void;
}

/** Owns source/privacy reads and opaque server cursors; conflicting type selectors cannot widen scope. */
function useMediaQuery(instance: GadgetInstance, state: MediaFilterState, calendar: boolean, isOwner: boolean) {
  const t = useTranslations('dashboard');
  const sourceIds = [...new Set(instance.definition.source_ids)].slice(0, 32);
  const sourceQuery = useQuery({
    queryKey: [...dashboardKeys.sources, 'media'],
    queryFn: ({ signal }) => listGadgetSources(100, undefined, signal),
    enabled: sourceIds.length > 0,
  });
  // Reuse the settings query key so a confirmed privacy save immediately updates this notice.
  const privacy = useQuery({
    queryKey: ['ai-settings'],
    queryFn: () => apiRequest<{ privacy: { allow_widget_remote_media: boolean } }>('/api/v1/settings/ai'),
    enabled: isOwner,
  });
  const filters: GadgetProjectionFilters = {
    ...state.filters,
    release_statuses: state.releaseStatus === 'all' ? undefined : [state.releaseStatus],
    ...(calendar ? { sort: 'release_date_asc' as const, release_window_days: state.window } : { sort: 'created_at_desc' as const }),
  };
  // The singular quick filter narrows the selector list; a conflicting saved pair remains empty, not widened.
  const typeConflict = state.mediaType !== 'all' && !!filters.media_types?.length && !filters.media_types.includes(state.mediaType);
  if (state.mediaType !== 'all') filters.media_types = [state.mediaType];
  const activeSources = state.source === 'all' ? sourceIds : sourceIds.filter((id) => id === state.source);
  const query = useInfiniteQuery({
    queryKey: [...documentKeys.all, 'media', instance.id, activeSources, filters],
    initialPageParam: undefined as string | undefined,
    enabled: activeSources.length > 0 && !typeConflict,
    queryFn: ({ pageParam }) => listGadgetDocumentProjections(activeSources, [], pageParam, filters),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: 30_000,
  });
  const documents =
    typeConflict || !activeSources.length
      ? []
      : [
          ...new Map(
            (query.data?.pages.flatMap((page) => page.items) ?? [])
              .filter((item) => !!item.media)
              .map((item) => [item.document_version_id, item]),
          ).values(),
        ];
  const sources = sourceIds.map((id) => ({
    id,
    name: sourceQuery.data?.items.find((source) => source.id === id)?.name ?? t('feed.sourceUnknown'),
  }));
  return { sourceIds, activeSources, typeConflict, query, documents, sources, privacy };
}

/** Applies only local reading filters to loaded typed projections; pagination remains server-owned. */
function filterMediaItems(
  documents: GadgetDocumentProjection[],
  state: MediaFilterState,
  definition: GadgetDefinition,
  hiddenIds: ReadonlySet<string>,
) {
  const needle = state.search.trim().toLowerCase();
  return documents
    .filter((item) => {
      const text = `${item.title} ${item.excerpt} ${item.media?.creator ?? ''}`.toLowerCase();
      return (
        !item.dismissed_at &&
        !hiddenIds.has(item.document_version_id) &&
        (!state.onlyUnread || !item.read_at) &&
        (!needle || text.includes(needle)) &&
        (!definition.filters.keywords?.length || definition.filters.keywords.some((word) => text.includes(word.toLowerCase()))) &&
        !definition.filters.exclude_keywords?.some((word) => text.includes(word.toLowerCase()))
      );
    })
    .slice(0, definition.filters.limit ?? 200);
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Timezone in which a provider release date must be read: date-only values (YYYY-MM-DD) are calendar dates without a
 * zone, so they are read as UTC to avoid shifting a day in negative-offset display timezones; instants use the display zone.
 */
export function releaseTimeZone(releaseDate: string, displayTimezone: string): string {
  return DATE_ONLY.test(releaseDate) ? 'UTC' : displayTimezone;
}

/** Builds a stable display-local date key from calendar fields instead of the provider's offset or UTC prefix. */
function mediaDateKey(item: GadgetDocumentProjection, timezone: string): string {
  if (!item.media?.release_date || !Number.isFinite(Date.parse(item.media.release_date))) return '';
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: releaseTimeZone(item.media.release_date, timezone),
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(item.media.release_date));
  return ['year', 'month', 'day'].map((type) => parts.find((part) => part.type === type)?.value).join('-');
}

/** Groups the loaded shelf using display-local dates and localized headings, or delegates to calendar buckets. */
function groupMediaItems(
  items: GadgetDocumentProjection[],
  groupBy: GadgetDefinition['filters']['group_by'],
  sources: { id: string; name: string }[],
  label: ReturnType<typeof useMediaLabel>,
  timezone: string,
  locale: AppLocaleId,
  groupItems: MediaCollectionProps['groupItems'],
): MediaGroup[] {
  return groupItems
    ? groupItems(items, timezone)
    : groupBy && groupBy !== 'none'
      ? Object.entries(
          Object.groupBy(items, (item) =>
            groupBy === 'source' ? item.source_id : groupBy === 'media_type' ? item.media!.media_type : mediaDateKey(item, timezone),
          ),
        ).map(([key, rows]) => ({
          key,
          label:
            groupBy === 'source'
              ? (sources.find((source) => source.id === key)?.name ?? key)
              : groupBy === 'date' && key && rows?.[0]?.media?.release_date
                ? formatDate(rows[0].media.release_date, locale, releaseTimeZone(rows[0].media.release_date, timezone))
                : label(key),
          items: rows ?? [],
        }))
      : [{ key: 'all', label: '', items }];
}

/** Maximum immutable versions one selection may carry to chat. */
const MEDIA_SELECTION_LIMIT = 32;

/** Renders shared cards while keeping selection capped at 32 immutable versions and writes serialized. */
function MediaGroups({
  groups,
  calendar,
  view,
  isOwner,
  selected,
  setSelected,
  moderation,
  ask,
}: {
  groups: MediaGroup[];
  calendar: boolean;
  view: FeedView;
  isOwner: boolean;
  selected: Record<string, SelectedTelegramIdentity>;
  setSelected: React.Dispatch<React.SetStateAction<Record<string, SelectedTelegramIdentity>>>;
  moderation: ReturnType<typeof useFeedModeration>;
  ask: (identities: SelectedTelegramIdentity[]) => void;
}) {
  // Only currently visible records count toward the cap; selections hidden by later filters are pruned on the next tick.
  const visibleIds = new Set(groups.flatMap((group) => group.items.map((item) => item.document_version_id)));
  return (
    <>
      {groups.map((group) => (
        <section key={group.key} className="space-y-2">
          {group.label && <h3 className="text-sm font-semibold">{group.label}</h3>}
          <div className={!calendar && view === 'cards' ? 'grid grid-cols-2 gap-3 @[40rem]/gadget:grid-cols-3' : 'space-y-2'}>
            {group.items.map((item) => (
              <MediaCard
                key={item.document_version_id}
                canInteract={isOwner}
                item={item}
                view={calendar ? 'list' : view}
                selected={!!selected[item.document_version_id]}
                onSelect={(checked) =>
                  setSelected((current) => {
                    const next = Object.fromEntries(Object.entries(current).filter(([id]) => visibleIds.has(id)));
                    if (!checked) delete next[item.document_version_id];
                    else if (Object.keys(next).length < MEDIA_SELECTION_LIMIT) next[item.document_version_id] = mediaIdentity(item);
                    return next;
                  })
                }
                pending={moderation.interactionMutation.isPending}
                onInteract={(change) => moderation.interactionMutation.mutate(change)}
                onHide={() => moderation.setHidden([mediaIdentity(item)], true)}
                onAsk={() => ask([mediaIdentity(item)])}
              />
            ))}
          </div>
        </section>
      ))}
    </>
  );
}

/** Reports the latest loaded collection instant and distinct provider attribution to the frame footer. */
function useMediaFreshness(documents: GadgetDocumentProjection[]) {
  const t = useTranslations('dashboard');
  const at =
    documents
      .map((item) => item.observed_at)
      .sort()
      .at(-1) ?? null;
  const attribution = [...new Set(documents.map((item) => item.media?.attribution).filter(Boolean))].join(' · ');
  useReportGadgetFreshness(at, attribution || (documents.length ? t('gadget.itemCount', { count: documents.length }) : null));
}

/** Projection feedback and cursor controls surround loaded records. Disabled query scopes expose no pagination. */
function MediaResults({
  sourceCount,
  query,
  itemCount,
  shownCount,
  limit,
  children,
  paginationAllowed,
  remoteMediaDisabled,
}: {
  sourceCount: number;
  query: ReturnType<typeof useMediaQuery>['query'];
  itemCount: number;
  /** Records actually rendered after grouping (calendar grouping omits records without a valid release date). */
  shownCount: number;
  limit: number;
  children: React.ReactNode;
  paginationAllowed: boolean;
  remoteMediaDisabled: boolean;
}) {
  const t = useTranslations('dashboard');
  return (
    <>
      {remoteMediaDisabled && (
        <Link
          href="/settings/models#ai-widget-media"
          className="rounded-md border border-line p-2 text-xs text-muted-foreground hover:underline"
        >
          {t('media.remoteNotice')}
        </Link>
      )}
      {!sourceCount && <GadgetNotice kind="setup" message={t('media.sourcesRequired')} />}
      {!!sourceCount && query.isLoading && <GadgetSkeleton variant="list" label={t('gadget.dataLoading')} />}
      {query.isError && <GadgetNotice kind="error" message={t('gadget.dataLoadFailed')} />}
      {!!sourceCount && !query.isLoading && !query.isError && !shownCount && <GadgetNotice kind="empty" message={t('media.empty')} />}
      {children}
      {paginationAllowed && (
        <>
          {query.isFetchNextPageError && (
            <p role="alert" className="text-xs text-destructive">
              {t('feed.loadMoreError')}
            </p>
          )}
          {query.hasNextPage && itemCount < limit && (
            <Button type="button" variant="outline" size="sm" disabled={query.isFetchingNextPage} onClick={() => query.fetchNextPage()}>
              {query.isFetchingNextPage ? t('feed.loadingMore') : t('feed.loadMore')}
            </Button>
          )}
        </>
      )}
    </>
  );
}

/** Seeds reading controls from the persisted definition; Reset uses the same effective defaults. */
function mediaStateDefaults(definition: GadgetDefinition): MediaFilterState {
  return {
    filters: mediaDefaults(definition),
    mediaType: definition.filters.media_type ?? 'all',
    releaseStatus: definition.filters.release_status ?? 'all',
    search: '',
    onlyUnread: false,
    source: 'all',
    window: definition.scope.release_window_days ?? 30,
  };
}

/** Rebases definition-owned reading state and selections on identity/revision changes while retaining the view preference. */
export function MediaCollection(props: MediaCollectionProps) {
  const [view, setView] = useLocalPref<FeedView>(`umwelt.dashboard.mediaView.${props.instance.id}`, 'cards', FEED_VIEWS);
  const definition = props.instance.definition;
  return <MediaCollectionBody key={`${definition.id}:${definition.revision}`} {...props} view={view} setView={setView} />;
}

/** Bulk-action bar for the visible selection; shows the cap hint once the 32-item limit is reached. Renders nothing when empty. */
function MediaSelectionBar({ count, onAsk, onClear }: { count: number; onAsk: () => void; onClear: () => void }) {
  const t = useTranslations('dashboard');
  if (!count) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {count >= MEDIA_SELECTION_LIMIT && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('media.selectionLimit', { count: MEDIA_SELECTION_LIMIT })}
        </p>
      )}
      <Button type="button" variant="outline" size="sm" onClick={onAsk}>
        {t('askAbout.selected', { count })}
      </Button>
      <Button type="button" variant="ghost" size="sm" onClick={onClear}>
        {t('feed.clear')}
      </Button>
    </div>
  );
}

/** Owns one definition revision's filters, source choice, selection and moderation inside the frame's stable host. */
function MediaCollectionBody({ instance, calendar = false, groupItems, view, setView }: MediaCollectionBodyProps) {
  const t = useTranslations('dashboard');
  const label = useMediaLabel();
  const display = useDisplayPreferences();
  const { openDrawer } = useChatController();
  const { isOwner } = useWorkspace();
  const definition = instance.definition;
  const defaults = mediaStateDefaults(definition);
  const [state, setState] = useState(defaults);
  const [selected, setSelected] = useState<Record<string, SelectedTelegramIdentity>>({});
  const rootRef = useRef<HTMLDivElement>(null);
  const undoRef = useRef<HTMLButtonElement>(null);
  const moderation = useFeedModeration(instance.id, { rootRef, undoRef });
  const { sourceIds, activeSources, typeConflict, query, documents, sources, privacy } = useMediaQuery(instance, state, calendar, isOwner);
  const items = filterMediaItems(documents, state, definition, moderation.hiddenIds);
  useMediaFreshness(documents);
  /** Chat receives only currently visible immutable identities; server resolves ownership, provenance and egress. */
  const ask = (identities: SelectedTelegramIdentity[]) =>
    isOwner &&
    openDrawer({
      context: {
        kind: 'selection',
        items: identities.map(({ sourceId, documentId, documentVersionId }) => ({ sourceId, documentId, documentVersionId })),
      },
    });
  const activeSelected = items.filter((item) => selected[item.document_version_id]).map(mediaIdentity);
  const groups = groupMediaItems(items, definition.filters.group_by, sources, label, display.timezone, display.locale, groupItems);
  return (
    <div
      ref={rootRef}
      tabIndex={-1}
      className="flex h-full flex-col gap-3 overflow-y-auto bg-card px-[var(--gadget-pad)] py-2 text-card-foreground"
    >
      <MediaFilterBar
        state={state}
        setState={setState}
        defaults={defaults}
        calendar={calendar}
        refreshing={query.isFetching}
        onRefresh={() => {
          if (activeSources.length && !typeConflict) void query.refetch();
        }}
        view={view}
        setView={setView}
        sources={sources}
      />
      <FeedSourceTabs
        sourceIds={sourceIds}
        sourceNames={sources}
        value={state.source}
        onChange={(source) => setState({ ...state, source })}
      />
      <MediaSelectionBar count={activeSelected.length} onAsk={() => ask(activeSelected)} onClear={() => setSelected({})} />
      <FeedUndoBar
        lastHidden={moderation.lastHidden}
        undoFailed={moderation.undoFailed}
        hideFailed={moderation.hideFailed}
        interactionPending={moderation.interactionMutation.isPending}
        undoRef={undoRef}
        onUndo={() => moderation.setHidden(moderation.lastHidden, false)}
      />
      {/* Hide/undo failures are announced by FeedUndoBar; only read/bookmark failures use this alert. */}
      {moderation.interactionMutation.isError && moderation.interactionMutation.variables?.dismissed === undefined && (
        <p role="alert" className="text-xs text-destructive">
          {t('media.interactionFailed')}
        </p>
      )}
      <MediaResults
        sourceCount={sourceIds.length}
        query={query}
        itemCount={items.length}
        shownCount={groups.reduce((sum, group) => sum + group.items.length, 0)}
        limit={definition.filters.limit ?? 200}
        paginationAllowed={activeSources.length > 0 && !typeConflict}
        remoteMediaDisabled={privacy.data?.privacy.allow_widget_remote_media === false}
      >
        <MediaGroups
          groups={groups}
          calendar={calendar}
          view={view}
          isOwner={isOwner}
          selected={selected}
          setSelected={setSelected}
          moderation={moderation}
          ask={ask}
        />
      </MediaResults>
    </div>
  );
}
