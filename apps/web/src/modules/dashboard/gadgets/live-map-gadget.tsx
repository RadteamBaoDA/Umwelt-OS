'use client';

import { X } from 'lucide-react';
import { useTranslations } from 'next-intl';
import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useState,
  type ComponentProps,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ShowMoreButton, useShowMore } from '@/components/show-more';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useSkin } from '@/core/use-skin';
import type { GadgetFilters, GadgetInstance, GadgetScope } from '../api';
import { liveMapWeatherKey } from '../gadget-editor-model';
import { LiveEventLegend } from './live-map-legend';
import { LiveMapTerminalFilters, LiveMapTitleBar, TerminalEventRow } from './live-map-terminal';
import { safeLiveLink } from '../live-map-data';
import { LiveMapConfig } from '../live-map-config';
import { LIVE_TAG_KEY, LIVE_TAGS, providerMeta } from '../live-feed/sources';
import type { StoredEvent } from '../live-feed/store';
import { termsAccepted, useLiveFeed, type ProviderStatus } from '../live-feed/use-live-feed';

const WorldMap = lazy(() => import('./world-map').then((module) => ({ default: module.WorldMap })));

/**
 * Keeps the live map mounted (filters, selection and the map context survive scrolling and tab switches) and tells it
 * whether it is on a visible page so polling and fetching pause while hidden.
 */
export function LiveMapGadget({ instance, isEditMode }: { instance: GadgetInstance; isEditMode: boolean }) {
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!node) return;
    let intersecting = false;
    /** Start collection only while the gadget intersects a visible page. */
    const update = () => setVisible(intersecting && document.visibilityState === 'visible');
    const observer = new IntersectionObserver(([entry]) => {
      intersecting = entry.isIntersecting;
      update();
    });
    observer.observe(node);
    document.addEventListener('visibilitychange', update);
    return () => {
      observer.disconnect();
      document.removeEventListener('visibilitychange', update);
    };
  }, [node]);
  return (
    <div ref={setNode} className="h-full min-h-0">
      <LiveMapContent
        scope={instance.definition.scope ?? {}}
        engine={instance.definition.filters?.map_engine ?? 'globe'}
        weatherLayer={instance.definition.filters?.map_weather_layer ?? null}
        isEditMode={isEditMode}
        visible={visible}
      />
    </div>
  );
}

/**
 * Renders the dashboard's live overview without creating a gadget instance or changing its saved layout.
 * @param toolbarStart Optional control (the map collapse toggle) placed first in the map toolbar row so it never overlaps the filters.
 */
export function LiveMapOverview({ toolbarStart }: { toolbarStart?: ReactNode }) {
  const t = useTranslations('dashboard');
  // Overview defaults to the 3D globe; the choice is session-local and never written to the saved layout.
  const [engine, setEngine] = useState<'globe' | 'flat'>('globe');
  // Two-button segmented control (not a Select): the pressed segment is the active engine, as in the developer-theme demo.
  const engineSegment = (
    <div role="group" aria-label={t('map.engineLabel')} className="map-engine-seg inline-flex shrink-0 items-center gap-1">
      <Button
        type="button"
        size="sm"
        variant={engine === 'globe' ? 'default' : 'ghost'}
        aria-pressed={engine === 'globe'}
        onClick={() => setEngine('globe')}
      >
        {t('map.globeEngine')}
      </Button>
      <Button
        type="button"
        size="sm"
        variant={engine === 'flat' ? 'default' : 'ghost'}
        aria-pressed={engine === 'flat'}
        onClick={() => setEngine('flat')}
      >
        {t('map.flatEngine')}
      </Button>
    </div>
  );
  return (
    <LiveMapContent
      scope={{}}
      engine={engine}
      weatherLayer={null}
      isEditMode={false}
      overview
      toolbarStart={
        <>
          {toolbarStart}
          {engineSegment}
        </>
      }
    />
  );
}

/** The "open event" link and provider source links of one event (only safe http(s) URLs render). */
function EventLinks({ event, separator }: { event: { url?: string | null; sources?: { url: string }[] }; separator: boolean }) {
  const t = useTranslations('dashboard');
  const href = safeLiveLink(event.url ?? null);
  return (
    <>
      {href &&
        (separator ? (
          <>
            {' '}
            ·{' '}
            <a className="underline" href={href} target="_blank" rel="noreferrer">
              {t('liveMap.openEvent')}
            </a>
          </>
        ) : (
          <a className="underline" href={href} target="_blank" rel="noreferrer">
            {t('liveMap.openEvent')}
          </a>
        ))}
      {event.sources?.map(
        ({ url }) =>
          safeLiveLink(url) && (
            <a key={url} className="block underline" href={safeLiveLink(url) ?? undefined} target="_blank" rel="noreferrer">
              {t('liveMap.providerSource')}
            </a>
          ),
      )}
    </>
  );
}

/** Rows rendered before "show more": the feed can hold thousands of events and each row carries links and a time. */
export const EVENT_LIST_INITIAL = 50;

/** One-line event row: a fixed 26px grid of time, title (truncates), source, then links in their own non-shrinking cell. */
const COMPACT_ROW_CLASS = 'grid h-[26px] grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-2 text-xs';

/**
 * Event list: time, select button (title), source and links, plus `emptyState` when there are no events.
 * Only the first `EVENT_LIST_INITIAL` rows render until expanded; `resetKey` (the active filters) collapses it again.
 * Rows use the one-line layout unless `inDialog` is set, in which case the stacked layout is kept.
 *
 * @param props.onSelect Called with the event key when its title button is pressed.
 * @param props.inDialog Use the stacked row layout (no caller currently sets this).
 * @param props.terminal Render terminal-skin rows (`HH:MM KIND title #tag`) instead of the default rows.
 */
function LiveEventList({
  events,
  onSelect,
  emptyState,
  resetKey,
  inDialog = false,
  terminal = false,
}: {
  events: StoredEvent[];
  onSelect: (key: string) => void;
  emptyState: ReactNode;
  resetKey: string;
  inDialog?: boolean;
  terminal?: boolean;
}) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const shown = useShowMore(events, EVENT_LIST_INITIAL, resetKey);
  // One formatter per locale/timezone instead of one per row (same options as formatDateTime).
  const format = useMemo(() => {
    const formatter = new Intl.DateTimeFormat(normalizeFormattingLocale(display.locale), {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: display.timezone,
    });
    return (value: string) => formatter.format(new Date(value));
  }, [display.locale, display.timezone]);
  // Page rows are one line (time | title | source), 26px high, so the overview list shows at least five rows.
  const rowClass = `live-event-row ${inDialog ? 'py-1 text-xs' : COMPACT_ROW_CLASS}`;
  const titleClass = inDialog
    ? 'h-auto w-full justify-start whitespace-normal p-1 text-left'
    : 'h-6 min-w-0 justify-start truncate p-1 text-left';
  return (
    <div className={inDialog ? 'min-h-0' : 'min-h-0 flex-1 overflow-y-auto'}>
      <ul aria-label={t('liveMap.events')} className="divide-y divide-border">
        {shown.visible.map((event) =>
          terminal ? (
            <TerminalEventRow key={event.key} event={event} onSelect={onSelect} />
          ) : (
            <li key={event.key} className={rowClass}>
              {event.observed_at && Number.isFinite(Date.parse(event.observed_at)) ? (
                <time dateTime={event.observed_at} className="whitespace-nowrap tabular-nums text-muted-foreground">
                  {format(event.observed_at)}
                </time>
              ) : (
                <span className="whitespace-nowrap text-muted-foreground">{t('liveMap.timeUnknown')}</span>
              )}
              <Button type="button" variant="ghost" className={titleClass} onClick={() => onSelect(event.key)}>
                <span className="min-w-0 truncate">{event.title}</span>
              </Button>
              <span className="live-event-source flex min-w-0 items-center overflow-hidden whitespace-nowrap text-muted-foreground">
                {event.source}
              </span>
              {/* Links sit in their own cell so a long title or source truncates before they are pushed out of the row. */}
              <span className="flex shrink-0 items-center gap-2 whitespace-nowrap text-muted-foreground">
                <EventLinks event={event} separator={inDialog} />
              </span>
            </li>
          ),
        )}
      </ul>
      <div className="flex justify-center">
        <ShowMoreButton state={shown} />
      </div>
      {events.length === 0 && <div className="text-xs text-muted-foreground">{emptyState}</div>}
    </div>
  );
}

/** Per-provider attribution line with its status: config error, terms required, fetch error, last update or loading. */
function LiveSourceStatus({
  providers,
  config,
  statuses,
  inDialog = false,
}: {
  providers: string[];
  config: ReturnType<typeof useLiveFeed>['config'];
  statuses: Record<string, ProviderStatus>;
  inDialog?: boolean;
}) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  /** Status text for one provider; terms and config problems outrank fetch state. */
  const stateOf = (provider: string) => {
    const status = statuses[provider];
    if (config.isError) return t('liveMap.configLoadError');
    // The sources dialog (overview) has no gadget settings, so its wording points at the dialog itself.
    if (config.data && !termsAccepted(config, provider)) return t(inDialog ? 'liveMap.termsRequiredHere' : 'liveMap.termsRequired');
    if (status?.error) return t(status.error);
    return status?.fetchedAt
      ? t('liveMap.sourceUpdated', { when: formatDateTime(status.fetchedAt, display.locale, display.timezone) })
      : t('liveMap.sourceLoading');
  };
  return (
    <div
      className={inDialog ? 'text-xs text-muted-foreground' : 'max-h-20 shrink-0 overflow-y-auto text-xs text-muted-foreground'}
      aria-label={t('liveMap.sources')}
    >
      {providers.map((provider) => {
        const meta = providerMeta(provider);
        const attribution = config.data?.terms_catalog?.find((term) => term.id === provider)?.attribution;
        return (
          <p key={provider}>
            {meta.home ? (
              <a className="underline" href={meta.home} target="_blank" rel="noreferrer">
                {meta.name}
              </a>
            ) : (
              meta.name
            )}
            : {stateOf(provider)}
            {attribution && <span className="block">{attribution}</span>}
            {statuses[provider]?.staleSince
              ? ` · ${t('liveMap.staleNote', { when: formatDateTime(statuses[provider].staleSince, display.locale, display.timezone) })}`
              : ''}
            {statuses[provider]?.truncated ? ` · ${t('observation.truncated')}` : ''}
          </p>
        );
      })}
    </div>
  );
}

/** Weather overlay notes: legend and credit when ready, terms-required and error notices. */
function LiveWeatherNotes({
  weatherLayer,
  weatherState,
  config,
  isOwner,
}: {
  weatherLayer: string | null;
  weatherState: 'idle' | 'loading' | 'ready' | 'error';
  isOwner: boolean;
  config: ReturnType<typeof useLiveFeed>['config'];
}) {
  const t = useTranslations('dashboard');
  if (!weatherLayer) return null;
  // Weather tiles are owner-only; observers get a plain notice instead of failing requests.
  if (!isOwner)
    return (
      <p role="status" className="text-xs text-muted-foreground">
        {t('liveMap.ownerOnly')}
      </p>
    );
  return (
    <>
      {weatherState === 'ready' && (
        <p className="text-xs text-muted-foreground">
          {t(liveMapWeatherKey(weatherLayer))} ·{' '}
          <a href="https://openweathermap.org/api/weathermaps" target="_blank" rel="noreferrer" className="underline">
            {t('liveMap.weatherLegend')}
          </a>{' '}
          ·{' '}
          <a href="https://openweathermap.org/" target="_blank" rel="noreferrer" className="underline">
            {t('liveMap.weatherCredit')}
          </a>{' '}
          {config.data?.terms_catalog?.find((term) => term.id === 'openweather')?.attribution}
        </p>
      )}
      {config.data && !config.data.terms_catalog.some((term) => term.id === 'openweather' && term.accepted) && (
        <p role="status" className="text-xs text-destructive">
          {t('liveMap.termsRequired')}
        </p>
      )}
      {(weatherState === 'error' || config.isError) && (
        <p role="status" className="text-xs text-destructive">
          {t('liveMap.weatherError')}
        </p>
      )}
    </>
  );
}

/** Details card of the selected event, anchored over the map, with a close control. */
function SelectedEventPopup({ event, onClose }: { event: StoredEvent; onClose: () => void }) {
  const t = useTranslations('dashboard');
  return (
    <div className="absolute bottom-2 left-2 max-w-[90%] rounded bg-card/90 p-2 pr-9 text-xs">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="absolute right-0.5 top-0.5 h-6 w-6"
        aria-label={t('liveMap.closeEvent')}
        onClick={onClose}
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </Button>
      <strong>{event.title}</strong>
      <p>{event.summary}</p>
      <EventLinks event={event} separator={false} />
    </div>
  );
}

/** Current time, refreshed every minute so the period filter slides without a refetch. */
function useMinuteClock(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** Observe an element's content box; attach `setNode` as its ref. */
function useElementSize() {
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    if (!node) return;
    const resize = new ResizeObserver(([entry]) =>
      setSize({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) }),
    );
    resize.observe(node);
    return () => resize.disconnect();
  }, [node]);
  return { setNode, size };
}

/**
 * Filter row of the live map: tag chips (multi-toggle with an "All" reset), text search, refresh of proxied
 * providers, period select and, in the overview, the sources dialog opener.
 * @param onOpenSources Shown only when provided (overview).
 */
function LiveMapToolbar({
  toolbarStart,
  tags,
  onTags,
  query,
  onQuery,
  days,
  onDays,
  results,
  onRefresh,
  onOpenSources,
}: {
  toolbarStart?: ReactNode;
  tags: string[];
  onTags: Dispatch<SetStateAction<string[]>>;
  query: string;
  onQuery: (value: string) => void;
  days: string;
  onDays: (value: string) => void;
  results: ReturnType<typeof useLiveFeed>['results'];
  onRefresh: () => void;
  onOpenSources?: () => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <div className="flex shrink-0 flex-wrap gap-2">
      {toolbarStart}
      <div role="group" aria-label={t('liveMap.category')} className="flex flex-wrap items-center gap-1">
        <Button
          type="button"
          size="sm"
          variant={tags.length === 0 ? 'default' : 'outline'}
          aria-pressed={tags.length === 0}
          onClick={() => onTags([])}
        >
          {t('liveMap.categoryAll')}
        </Button>
        {LIVE_TAGS.map((tag) => (
          <Button
            key={tag}
            type="button"
            size="sm"
            variant={tags.includes(tag) ? 'default' : 'outline'}
            aria-pressed={tags.includes(tag)}
            onClick={() => onTags((current) => (current.includes(tag) ? current.filter((item) => item !== tag) : [...current, tag]))}
          >
            {t(LIVE_TAG_KEY[tag])}
          </Button>
        ))}
      </div>
      <Input
        type="search"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        aria-label={t('liveMap.search')}
        placeholder={t('liveMap.search')}
        className="h-8 w-40"
      />
      <Button type="button" size="sm" variant="outline" disabled={results.some((result) => result.isFetching)} onClick={onRefresh}>
        {t('liveMap.refresh')}
      </Button>
      <Select value={days} onValueChange={onDays}>
        <SelectTrigger aria-label={t('liveMap.period')} className="h-8 w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {['1', '7', '30'].map((value) => (
            <SelectItem key={value} value={value}>
              {t('map.range', { days: Number(value) })}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {onOpenSources && (
        <Button type="button" size="sm" variant="outline" onClick={onOpenSources}>
          {t('liveMap.sources')}
        </Button>
      )}
    </div>
  );
}

/**
 * Fetch bounded provider results while mounted. Overview keeps the map at a fixed short height with the event list
 * scrolling below it, and moves the long source status into an accessible dialog.
 */
/** Overview-only dialog listing provider statuses (children) above the live-map terms configuration. */
function LiveSourcesDialog({
  open,
  onOpenChange,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}) {
  const t = useTranslations('dashboard');
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl" closeLabel={t('close')}>
        <DialogHeader>
          <DialogTitle>{t('liveMap.sources')}</DialogTitle>
        </DialogHeader>
        {children}
        <LiveMapConfig />
      </DialogContent>
    </Dialog>
  );
}

/**
 * Why the event list is empty, in priority order: config loading, config failed (with retry), no accepted provider
 * (overview: button opening the sources dialog; gadget: pointer to gadget settings), else a genuinely empty range.
 * @param onOpenSources Provided only in the overview.
 */
function LiveEmptyState({
  config,
  accepted,
  onOpenSources,
}: {
  config: ReturnType<typeof useLiveFeed>['config'];
  accepted: string[];
  onOpenSources?: () => void;
}) {
  const t = useTranslations('dashboard');
  if (config.isPending) return <p role="status">{t('liveMap.configLoading')}</p>;
  if (config.isError)
    return (
      <div role="status" className="flex flex-wrap items-center gap-2">
        <span>{t('liveMap.configLoadError')}</span>
        <Button type="button" size="sm" variant="outline" onClick={() => void config.refetch()}>
          {t('liveMap.configRetry')}
        </Button>
      </div>
    );
  if (accepted.length === 0)
    return onOpenSources ? (
      <div className="flex flex-wrap items-center gap-2">
        <span>{t('liveMap.noSources')}</span>
        <Button type="button" size="sm" variant="outline" onClick={onOpenSources}>
          {t('liveMap.enableSources')}
        </Button>
      </div>
    ) : (
      <p>{t('liveMap.termsRequired')}</p>
    );
  return <p>{t('liveMap.empty')}</p>;
}

/**
 * Overview event block: the "Live events (n)" heading, the category legend counts and the event list.
 * The block keeps at least five rows visible; the map above takes the remaining height.
 *
 * @param props.events Filtered events shown by the list and counted in the heading and legend.
 * @param props.onSelect Called with an event key when its row is pressed.
 * @param props.emptyState Shown by the list when there are no events.
 * @param props.resetKey Filter key; changing it collapses the "show more" expansion.
 * @param props.terminal Terminal skin: `live events 37` heading and terminal rows.
 */
function OverviewEventsPanel({
  events,
  onSelect,
  emptyState,
  resetKey,
  terminal,
}: {
  events: StoredEvent[];
  onSelect: (key: string) => void;
  emptyState: ReactNode;
  resetKey: string;
  terminal: boolean;
}) {
  const t = useTranslations('dashboard');
  return (
    <div className={terminal ? 'live-events-term flex flex-col' : 'flex min-h-[10.5rem] shrink-0 flex-col'}>
      <div className="flex shrink-0 items-center gap-3">
        <h3 className="shrink-0 text-xs font-medium text-muted-foreground">
          {t('liveMap.events')} {terminal ? <span className="live-events-count">{events.length}</span> : `(${events.length})`}
        </h3>
        <LiveEventLegend events={events} />
      </div>
      <LiveEventList events={events} onSelect={onSelect} emptyState={emptyState} resetKey={resetKey} terminal={terminal} />
    </div>
  );
}

/**
 * Measured plot area of the live map: renders the WebGL map once it has a size, plus the selected-event card.
 *
 * @param props.setNode Ref callback of the element whose size drives the map.
 * @param props.showMap False until the plot has a measurable size (the map is not created before that).
 * @param props.mapProps Props forwarded to the map renderer (features, selection, size, weather overlay).
 * @param props.onClearSelection Closes the selected-event card.
 */
function LiveMapPlot({
  setNode,
  overview,
  showMap,
  mapProps,
  engine,
  selectedEvent,
  onClearSelection,
}: {
  setNode: (node: HTMLDivElement | null) => void;
  overview: boolean;
  showMap: boolean;
  mapProps: ComponentProps<typeof WorldMap> extends infer P ? Omit<P, 'projection'> : never;
  engine: 'globe' | 'flat';
  selectedEvent: StoredEvent | undefined;
  onClearSelection: () => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <div
      ref={setNode}
      role="group"
      aria-label={t('map.plotLabel')}
      className={`live-map-plot relative overflow-hidden rounded border border-border ${overview ? 'min-h-24 flex-1 basis-0' : 'min-h-48 flex-[2]'}`}
    >
      {showMap && (
        <div className="absolute inset-0">
          <Suspense fallback={<p role="status">{t('map.rendererLoading')}</p>}>
            <WorldMap {...mapProps} projection={engine === 'globe' ? 'globe' : 'mercator'} />
          </Suspense>
        </div>
      )}
      {selectedEvent && <SelectedEventPopup event={selectedEvent} onClose={onClearSelection} />}
    </div>
  );
}

/**
 * Terminal-skin overview pane (developer-theme `.dash-map`): title bar with the engine/collapse actions, refresh and
 * sources icons, one filter row, the map plot, the event list with its legend, and the credit footer line.
 * Filter state is owned by the caller so the default and terminal layouts share one set of filters.
 */
function TerminalOverviewMap(p: {
  filtered: StoredEvent[];
  days: string;
  onDays: (value: string) => void;
  tags: string[];
  onTags: Dispatch<SetStateAction<string[]>>;
  query: string;
  onQuery: (value: string) => void;
  toolbarStart?: ReactNode;
  refreshing: boolean;
  onRefresh: () => void;
  onOpenSources: () => void;
  plot: ReactNode;
  onSelect: (key: string) => void;
  emptyState: ReactNode;
  resetKey: string;
  sourcesDialog: ReactNode;
}) {
  const t = useTranslations('dashboard');
  return (
    <section className="live-map-term flex h-full min-h-0 w-full flex-col overflow-hidden bg-card text-card-foreground">
      <LiveMapTitleBar
        count={p.filtered.length}
        days={p.days}
        actions={p.toolbarStart}
        refreshing={p.refreshing}
        onRefresh={p.onRefresh}
        onOpenSources={p.onOpenSources}
      />
      <div className="live-map-term-body flex min-h-0 flex-1 flex-col gap-2">
        <LiveMapTerminalFilters
          tags={p.tags}
          onTags={p.onTags}
          query={p.query}
          onQuery={p.onQuery}
          days={p.days}
          onDays={p.onDays}
          count={p.filtered.length}
        />
        {p.plot}
        <OverviewEventsPanel events={p.filtered} onSelect={p.onSelect} emptyState={p.emptyState} resetKey={p.resetKey} terminal />
      </div>
      <p className="live-map-foot shrink-0">{t('map.basemapCredit')}</p>
      {p.sourcesDialog}
    </section>
  );
}

/** Default-skin live map layout: toolbar chips, plot, weather notes, credit line and event list (or overview panel). */
function DefaultLiveMap(p: {
  toolbarStart?: ReactNode;
  tags: string[];
  onTags: Dispatch<SetStateAction<string[]>>;
  query: string;
  onQuery: (value: string) => void;
  days: string;
  onDays: (value: string) => void;
  results: ReturnType<typeof useLiveFeed>['results'];
  onRefresh: () => void;
  overview: boolean;
  onOpenSources: () => void;
  plot: ReactNode;
  weather: ReactNode;
  filtered: StoredEvent[];
  onSelect: (key: string) => void;
  emptyState: ReactNode;
  resetKey: string;
  sourcesDialog: ReactNode;
  sourceStatus: ReactNode;
}) {
  const t = useTranslations('dashboard');
  return (
    <section className="flex h-full min-h-0 w-full flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
      <LiveMapToolbar
        toolbarStart={p.toolbarStart}
        tags={p.tags}
        onTags={p.onTags}
        query={p.query}
        onQuery={p.onQuery}
        days={p.days}
        onDays={p.onDays}
        results={p.results}
        onRefresh={p.onRefresh}
        onOpenSources={p.overview ? p.onOpenSources : undefined}
      />
      {p.plot}
      {p.weather}
      <p className="shrink-0 text-xs text-muted-foreground">{t('map.basemapCredit')}</p>
      {p.overview ? (
        <>
          <OverviewEventsPanel events={p.filtered} onSelect={p.onSelect} emptyState={p.emptyState} resetKey={p.resetKey} terminal={false} />
          {p.sourcesDialog}
        </>
      ) : (
        <>
          <LiveEventList events={p.filtered} onSelect={p.onSelect} emptyState={p.emptyState} resetKey={p.resetKey} />
          {p.sourceStatus}
        </>
      )}
    </section>
  );
}

/** Static part of the map renderer props: features, selection, measured size, interactivity and weather layer. */
function baseMapProps(p: {
  points: ReturnType<typeof useLiveFeed>['points'];
  activeSelection: string | null;
  size: { width: number; height: number };
  visible: boolean;
  isEditMode: boolean;
  weatherLayer: NonNullable<GadgetFilters['map_weather_layer']> | null;
}) {
  return {
    focusSelected: true,
    features: p.points,
    selectedFeatureId: p.activeSelection,
    width: p.size.width,
    height: p.size.height,
    visible: p.visible,
    interactive: !p.isEditMode,
    weatherLayer: p.weatherLayer,
  };
}

/**
 * Live map body: toolbar, map, event list and source status. Stays mounted while off-screen.
 * @param visible False while the gadget is off-screen or the tab hidden: feed polling and map interaction pause.
 */
function LiveMapContent({
  scope,
  engine,
  weatherLayer,
  isEditMode,
  overview = false,
  toolbarStart,
  visible = true,
}: {
  scope: GadgetScope;
  engine: 'globe' | 'flat';
  weatherLayer: NonNullable<GadgetFilters['map_weather_layer']> | null;
  isEditMode: boolean;
  overview?: boolean;
  toolbarStart?: ReactNode;
  visible?: boolean;
}) {
  const terminal = useSkin() === 'terminal';
  const { setNode, size } = useElementSize();
  const [tags, setTags] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [days, setDays] = useState('7');
  const now = useMinuteClock();
  const [selected, setSelected] = useState<string | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [weatherState, setWeatherState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const feed = useLiveFeed(scope, { tags, days, query, now }, visible);
  const { workspaceId, isOwner, providers, accepted, config, results, events: filtered, points, statuses } = feed;
  const selectedEvent = filtered.find((event) => event.key === selected);
  // A selection whose event left the filtered list (filters, expiry) reads as cleared everywhere.
  const activeSelection = selectedEvent ? selected : null;
  const listResetKey = `${tags.join(',')}|${days}|${query}`;
  const mapProps = {
    ...baseMapProps({ points, activeSelection, size, visible, isEditMode, weatherLayer }),
    onSelectFeature: setSelected,
    weatherWorkspaceId: isOwner && termsAccepted(config, 'openweather') ? workspaceId : undefined,
    weatherRevision: config.data?.revision,
    onWeatherStateChange: setWeatherState,
  };
  const emptyState = (
    <LiveEmptyState config={config} accepted={accepted} onOpenSources={overview ? () => setSourcesOpen(true) : undefined} />
  );
  const plot = (
    <LiveMapPlot
      setNode={setNode}
      overview={overview}
      showMap={size.width > 0 && size.height > 0}
      mapProps={mapProps}
      engine={engine}
      selectedEvent={selectedEvent}
      onClearSelection={() => setSelected(null)}
    />
  );
  const sourcesDialog = (
    <LiveSourcesDialog open={sourcesOpen} onOpenChange={setSourcesOpen}>
      <LiveSourceStatus providers={providers} config={config} statuses={statuses} inDialog />
    </LiveSourcesDialog>
  );
  // Props both layouts share; each layout picks what it renders (the terminal one ignores `results`/`weather`).
  const shared = {
    toolbarStart,
    tags,
    onTags: setTags,
    query,
    onQuery: setQuery,
    days,
    onDays: setDays,
    filtered,
    onSelect: setSelected,
    emptyState,
    resetKey: listResetKey,
    plot,
    sourcesDialog,
    onRefresh: feed.refresh,
    onOpenSources: () => setSourcesOpen(true),
  };
  if (overview && terminal) return <TerminalOverviewMap {...shared} refreshing={results.some((result) => result.isFetching)} />;
  return (
    <DefaultLiveMap
      {...shared}
      results={results}
      overview={overview}
      weather={<LiveWeatherNotes weatherLayer={weatherLayer} weatherState={weatherState} config={config} isOwner={isOwner} />}
      sourceStatus={<LiveSourceStatus providers={providers} config={config} statuses={statuses} />}
    />
  );
}
