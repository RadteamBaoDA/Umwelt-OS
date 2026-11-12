'use client';

import { useTranslations } from 'next-intl';
import { lazy, Suspense, useEffect, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import type { GadgetFilters, GadgetInstance, GadgetScope } from '../api';
import { safeLiveLink } from '../live-map-data';
import { LiveMapConfig } from '../live-map-config';
import { LIVE_TAGS, providerMeta } from '../live-feed/sources';
import type { StoredEvent } from '../live-feed/store';
import { termsAccepted, useLiveFeed, type ProviderStatus } from '../live-feed/use-live-feed';

const WorldMap = lazy(() => import('./world-map').then((module) => ({ default: module.WorldMap })));

/** Mount live queries only while this gadget is visible, allowing the last observer to abort shared requests. */
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
      {visible && (
        <LiveMapContent
          scope={instance.definition.scope ?? {}}
          engine={instance.definition.filters?.map_engine ?? 'globe'}
          weatherLayer={instance.definition.filters?.map_weather_layer ?? null}
          isEditMode={isEditMode}
        />
      )}
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

/** Event list: select button, source and time, links, plus the empty note. */
function LiveEventList({
  events,
  onSelect,
  inDialog = false,
}: {
  events: StoredEvent[];
  onSelect: (key: string) => void;
  inDialog?: boolean;
}) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  return (
    <div className={inDialog ? 'min-h-0' : 'min-h-0 flex-1 overflow-y-auto'}>
      <ul aria-label={t('liveMapEvents')} className="divide-y divide-border">
        {events.map((event) => (
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
                <time dateTime={event.observed_at}>{formatDateTime(event.observed_at, display.locale, display.timezone)}</time>
              ) : (
                t('liveMapTimeUnknown')
              )}
            </span>
            <EventLinks event={event} separator />
          </li>
        ))}
      </ul>
      {events.length === 0 && <p className="text-xs text-muted-foreground">{t('liveMapEmpty')}</p>}
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
    if (config.data && !termsAccepted(config, provider)) return t('liveMapTermsRequired');
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
}: {
  weatherLayer: string | null;
  weatherState: 'idle' | 'loading' | 'ready' | 'error';
  config: ReturnType<typeof useLiveFeed>['config'];
}) {
  const t = useTranslations('dashboard');
  if (!weatherLayer) return null;
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

function LiveMapContent({
  scope,
  engine,
  weatherLayer,
  isEditMode,
  overview = false,
  toolbarStart,
}: {
  scope: GadgetScope;
  engine: 'globe' | 'flat';
  weatherLayer: NonNullable<GadgetFilters['map_weather_layer']> | null;
  isEditMode: boolean;
  overview?: boolean;
  toolbarStart?: ReactNode;
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
  const feed = useLiveFeed(scope, { tags, days, query, now });
  const { workspaceId, providers, config, results, events: filtered, points, statuses } = feed;
  const selectedEvent = filtered.find((event) => event.key === selected);
  const mapProps = {
    focusSelected: true,
    features: points,
    selectedFeatureId: selected,
    onSelectFeature: setSelected,
    width: size.width,
    height: size.height,
    visible: true,
    interactive: !isEditMode,
    weatherLayer,
    weatherWorkspaceId: termsAccepted(config, 'openweather') ? workspaceId : undefined,
    weatherRevision: config.data?.revision,
    onWeatherStateChange: setWeatherState,
  };
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
        {selectedEvent && (
          <div className="absolute bottom-2 left-2 max-w-[90%] rounded bg-card/90 p-2 text-xs">
            <strong>{selectedEvent.title}</strong>
            <p>{selectedEvent.summary}</p>
            <EventLinks event={selectedEvent} separator={false} />
          </div>
        )}
      </div>
      <LiveWeatherNotes weatherLayer={weatherLayer} weatherState={weatherState} config={config} />
      {overview ? (
        <>
          <h3 className="shrink-0 text-xs font-medium text-muted-foreground">
            {t('liveMapEvents')} ({filtered.length})
          </h3>
          <LiveEventList events={filtered} onSelect={setSelected} />
          <LiveSourcesDialog open={sourcesOpen} onOpenChange={setSourcesOpen}>
            <LiveSourceStatus providers={providers} config={config} statuses={statuses} inDialog />
          </LiveSourcesDialog>
        </>
      ) : (
        <>
          <LiveEventList events={filtered} onSelect={setSelected} />
          <LiveSourceStatus providers={providers} config={config} statuses={statuses} />
        </>
      )}
    </section>
  );
}
