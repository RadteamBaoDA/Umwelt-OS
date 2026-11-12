'use client';

import { X } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { lazy, Suspense, useEffect, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ShowMoreButton, useShowMore } from '@/components/show-more';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import type { GadgetFilters, GadgetInstance, GadgetScope } from '../api';
import { safeLiveLink } from '../live-map-data';
import { LiveMapConfig } from '../live-map-config';
import { LIVE_TAGS, providerMeta } from '../live-feed/sources';
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
  const engineSelect = (
    <Select value={engine} onValueChange={(next) => setEngine(next as 'globe' | 'flat')}>
      <SelectTrigger aria-label={t('mapEngineLabel')} className="h-8 w-36">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="globe">{t('mapGlobeEngine')}</SelectItem>
        <SelectItem value="flat">{t('mapFlatEngine')}</SelectItem>
      </SelectContent>
    </Select>
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
          {engineSelect}
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
              {t('liveMapOpenEvent')}
            </a>
          </>
        ) : (
          <a className="underline" href={href} target="_blank" rel="noreferrer">
            {t('liveMapOpenEvent')}
          </a>
        ))}
      {event.sources?.map(
        ({ url }) =>
          safeLiveLink(url) && (
            <a key={url} className="block underline" href={safeLiveLink(url) ?? undefined} target="_blank" rel="noreferrer">
              {t('liveMapProviderSource')}
            </a>
          ),
      )}
    </>
  );
}

/** Rows rendered before "show more": the feed can hold thousands of events and each row carries links and a time. */
export const EVENT_LIST_INITIAL = 50;

/**
 * Event list: select button, source and time, links, plus `emptyState` when there are no events. Only the first
 * `EVENT_LIST_INITIAL` rows render until expanded; `resetKey` (the active filters) collapses it again.
 */
function LiveEventList({
  events,
  onSelect,
  emptyState,
  resetKey,
  inDialog = false,
}: {
  events: StoredEvent[];
  onSelect: (key: string) => void;
  emptyState: ReactNode;
  resetKey: string;
  inDialog?: boolean;
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
  return (
    <div className={inDialog ? 'min-h-0' : 'min-h-0 flex-1 overflow-y-auto'}>
      <ul aria-label={t('liveMapEvents')} className="divide-y divide-border">
        {shown.visible.map((event) => (
          <li key={event.key} className="py-1 text-xs">
            <Button
              type="button"
              variant="ghost"
              className="h-auto w-full justify-start whitespace-normal p-1 text-left"
              onClick={() => onSelect(event.key)}
            >
              {event.title}
            </Button>
            <span className="text-muted-foreground">
              {event.source} ·{' '}
              {event.observed_at && Number.isFinite(Date.parse(event.observed_at)) ? (
                <time dateTime={event.observed_at}>{format(event.observed_at)}</time>
              ) : (
                t('liveMapTimeUnknown')
              )}
            </span>
            <EventLinks event={event} separator />
          </li>
        ))}
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
    if (config.isError) return t('liveMapConfigLoadError');
    // The sources dialog (overview) has no gadget settings, so its wording points at the dialog itself.
    if (config.data && !termsAccepted(config, provider)) return t(inDialog ? 'liveMapTermsRequiredHere' : 'liveMapTermsRequired');
    if (status?.error) return t(status.error);
    return status?.fetchedAt
      ? t('liveMapSourceUpdated', { when: formatDateTime(status.fetchedAt, display.locale, display.timezone) })
      : t('liveMapSourceLoading');
  };
  return (
    <div
      className={inDialog ? 'text-xs text-muted-foreground' : 'max-h-20 shrink-0 overflow-y-auto text-xs text-muted-foreground'}
      aria-label={t('liveMapSources')}
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
              ? ` · ${t('liveMapStaleNote', { when: formatDateTime(statuses[provider].staleSince, display.locale, display.timezone) })}`
              : ''}
            {statuses[provider]?.truncated ? ` · ${t('observationTruncated')}` : ''}
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
        {t('liveMapOwnerOnly')}
      </p>
    );
  return (
    <>
      {weatherState === 'ready' && (
        <p className="text-xs text-muted-foreground">
          {t(`liveMapWeather_${weatherLayer}` as 'liveMapWeather_none')} ·{' '}
          <a href="https://openweathermap.org/api/weathermaps" target="_blank" rel="noreferrer" className="underline">
            {t('liveMapWeatherLegend')}
          </a>{' '}
          ·{' '}
          <a href="https://openweathermap.org/" target="_blank" rel="noreferrer" className="underline">
            {t('liveMapWeatherCredit')}
          </a>{' '}
          {config.data?.terms_catalog?.find((term) => term.id === 'openweather')?.attribution}
        </p>
      )}
      {config.data && !config.data.terms_catalog.some((term) => term.id === 'openweather' && term.accepted) && (
        <p role="status" className="text-xs text-destructive">
          {t('liveMapTermsRequired')}
        </p>
      )}
      {(weatherState === 'error' || config.isError) && (
        <p role="status" className="text-xs text-destructive">
          {t('liveMapWeatherError')}
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
        aria-label={t('liveMapCloseEvent')}
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
      <div role="group" aria-label={t('liveMapCategory')} className="flex flex-wrap items-center gap-1">
        <Button
          type="button"
          size="sm"
          variant={tags.length === 0 ? 'default' : 'outline'}
          aria-pressed={tags.length === 0}
          onClick={() => onTags([])}
        >
          {t('liveMapCategory_all')}
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
            {t(`liveMapTag_${tag}`)}
          </Button>
        ))}
      </div>
      <Input
        type="search"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        aria-label={t('liveMapSearch')}
        placeholder={t('liveMapSearch')}
        className="h-8 w-40"
      />
      <Button type="button" size="sm" variant="outline" disabled={results.some((result) => result.isFetching)} onClick={onRefresh}>
        {t('liveMapRefresh')}
      </Button>
      <Select value={days} onValueChange={onDays}>
        <SelectTrigger aria-label={t('liveMapPeriod')} className="h-8 w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {['1', '7', '30'].map((value) => (
            <SelectItem key={value} value={value}>
              {t('mapRange', { days: Number(value) })}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {onOpenSources && (
        <Button type="button" size="sm" variant="outline" onClick={onOpenSources}>
          {t('liveMapSources')}
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
          <DialogTitle>{t('liveMapSources')}</DialogTitle>
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
  if (config.isPending) return <p role="status">{t('liveMapConfigLoading')}</p>;
  if (config.isError)
    return (
      <div role="status" className="flex flex-wrap items-center gap-2">
        <span>{t('liveMapConfigLoadError')}</span>
        <Button type="button" size="sm" variant="outline" onClick={() => void config.refetch()}>
          {t('liveMapConfigRetry')}
        </Button>
      </div>
    );
  if (accepted.length === 0)
    return onOpenSources ? (
      <div className="flex flex-wrap items-center gap-2">
        <span>{t('liveMapNoSources')}</span>
        <Button type="button" size="sm" variant="outline" onClick={onOpenSources}>
          {t('liveMapEnableSources')}
        </Button>
      </div>
    ) : (
      <p>{t('liveMapTermsRequired')}</p>
    );
  return <p>{t('liveMapEmpty')}</p>;
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
  const t = useTranslations('dashboard');
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
    focusSelected: true,
    features: points,
    selectedFeatureId: activeSelection,
    onSelectFeature: setSelected,
    width: size.width,
    height: size.height,
    visible,
    interactive: !isEditMode,
    weatherLayer,
    weatherWorkspaceId: isOwner && termsAccepted(config, 'openweather') ? workspaceId : undefined,
    weatherRevision: config.data?.revision,
    onWeatherStateChange: setWeatherState,
  };
  const emptyState = (
    <LiveEmptyState config={config} accepted={accepted} onOpenSources={overview ? () => setSourcesOpen(true) : undefined} />
  );
  return (
    <section className="flex h-full min-h-0 w-full flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
      <LiveMapToolbar
        toolbarStart={toolbarStart}
        tags={tags}
        onTags={setTags}
        query={query}
        onQuery={setQuery}
        days={days}
        onDays={setDays}
        results={results}
        onRefresh={feed.refresh}
        onOpenSources={overview ? () => setSourcesOpen(true) : undefined}
      />
      <div
        ref={setNode}
        role="group"
        aria-label={t('mapPlotLabel')}
        className={`relative overflow-hidden rounded border border-border ${overview ? 'h-[clamp(11rem,30vh,20rem)] shrink-0' : 'min-h-48 flex-[2]'}`}
      >
        {size.width > 0 && size.height > 0 && (
          <div className="absolute inset-0">
            <Suspense fallback={<p role="status">{t('mapRendererLoading')}</p>}>
              <WorldMap {...mapProps} projection={engine === 'globe' ? 'globe' : 'mercator'} />
            </Suspense>
          </div>
        )}
        {selectedEvent && <SelectedEventPopup event={selectedEvent} onClose={() => setSelected(null)} />}
      </div>
      <LiveWeatherNotes weatherLayer={weatherLayer} weatherState={weatherState} config={config} isOwner={isOwner} />
      <p className="shrink-0 text-xs text-muted-foreground">{t('mapBasemapCredit')}</p>
      {overview ? (
        <>
          <h3 className="shrink-0 text-xs font-medium text-muted-foreground">
            {t('liveMapEvents')} ({filtered.length})
          </h3>
          <LiveEventList events={filtered} onSelect={setSelected} emptyState={emptyState} resetKey={listResetKey} />
          <LiveSourcesDialog open={sourcesOpen} onOpenChange={setSourcesOpen}>
            <LiveSourceStatus providers={providers} config={config} statuses={statuses} inDialog />
          </LiveSourcesDialog>
        </>
      ) : (
        <>
          <LiveEventList events={filtered} onSelect={setSelected} emptyState={emptyState} resetKey={listResetKey} />
          <LiveSourceStatus providers={providers} config={config} statuses={statuses} />
        </>
      )}
    </section>
  );
}
