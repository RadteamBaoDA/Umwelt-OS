'use client';

import { useQueries, useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError, apiRequest } from '@/core/api';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useWorkspace } from '@/core/workspace-context';
import type { GadgetInstance, GadgetScope } from '../api';
import { liveMapPoints, safeLiveLink, type LiveEvent } from '../live-map-data';

const FlatMap = lazy(() => import('./flat-map').then((module) => ({ default: module.FlatMap })));
const GlobeMap = lazy(() => import('./globe-map').then((module) => ({ default: module.GlobeMap })));
const DEFAULT_PROVIDERS = ['usgs', 'eonet', 'hackernews', 'lobsters', 'cisa_kev', 'nvd', 'business', 'fed'];
const ATTRIBUTION: Record<string, string> = { usgs: 'https://earthquake.usgs.gov/', eonet: 'https://eonet.gsfc.nasa.gov/', firms: 'https://firms.modaps.eosdis.nasa.gov/', hackernews: 'https://news.ycombinator.com/', lobsters: 'https://lobste.rs/', cisa_kev: 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog', nvd: 'https://nvd.nist.gov/', business: 'https://vnexpress.net/kinh-doanh', fed: 'https://www.federalreserve.gov/', github_releases: 'https://github.com/', sec: 'https://www.sec.gov/' };
const PROVIDER_NAME: Record<string, string> = { usgs: 'USGS', eonet: 'NASA EONET', firms: 'NASA FIRMS', hackernews: 'Hacker News', lobsters: 'Lobsters', cisa_kev: 'CISA KEV', nvd: 'NVD', business: 'VnExpress', fed: 'Federal Reserve', github_releases: 'GitHub', sec: 'SEC' };
const CATEGORY: Record<string, 'geo' | 'technology' | 'finance'> = {
  usgs: 'geo', eonet: 'geo', firms: 'geo', hackernews: 'technology', lobsters: 'technology',
  cisa_kev: 'technology', nvd: 'technology', github_releases: 'technology', business: 'finance', fed: 'finance', sec: 'finance',
};

type LiveResponse = { items: LiveEvent[]; fetched_at: string; truncated: boolean };

/** Fetch one provider through the workspace-scoped proxy; the signal cancels unobserved or switched requests. */
async function fetchLiveEvents(provider: string, scope: GadgetScope, signal: AbortSignal): Promise<LiveResponse> {
  const params = new URLSearchParams({ provider });
  if (provider === 'firms' && scope.live_map_country) params.set('country', scope.live_map_country);
  if (provider === 'github_releases') {
    if (scope.live_map_github_owner) params.set('owner', scope.live_map_github_owner);
    if (scope.live_map_github_repo) params.set('repo', scope.live_map_github_repo);
  }
  if (provider === 'sec' && scope.live_map_sec_cik) params.set('cik', scope.live_map_sec_cik);
  return apiRequest<LiveResponse>(`/api/v1/live-map/events?${params}`, { signal });
}

/** Translate only allowlisted server error codes; never render provider response bodies or secrets. */
function liveErrorKey(error: unknown, provider: string): 'liveMapTermsRequired' | 'liveMapConfigChanged' | 'liveMapFirmsSetup' | 'liveMapGithubSetup' | 'liveMapSecSetup' | 'liveMapSecContact' | 'liveMapRateLimited' | 'liveMapSourceError' {
  if (!(error instanceof ApiError)) return 'liveMapSourceError';
  if (error.message === 'provider_terms_ineligible') return 'liveMapTermsRequired';
  if (error.message === 'live_map_configuration_changed') return 'liveMapConfigChanged';
  if (error.message === 'contact_required') return 'liveMapSecContact';
  if (error.message === 'provider_scope_invalid') return provider === 'firms' ? 'liveMapFirmsSetup' : provider === 'github_releases' ? 'liveMapGithubSetup' : provider === 'sec' ? 'liveMapSecSetup' : 'liveMapSourceError';
  if (error.status === 429) return 'liveMapRateLimited';
  return 'liveMapSourceError';
}

/** Mount live queries only while this gadget is visible, allowing the last observer to abort shared requests. */
export function LiveMapGadget({ instance, isEditMode }: { instance: GadgetInstance; isEditMode: boolean }) {
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!node) return;
    let intersecting = false;
    /** Start collection only while the gadget intersects a visible page. */
    const update = () => setVisible(intersecting && document.visibilityState === 'visible');
    const observer = new IntersectionObserver(([entry]) => { intersecting = entry.isIntersecting; update(); });
    observer.observe(node);
    document.addEventListener('visibilitychange', update);
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', update); };
  }, [node]);
  return <div ref={setNode} className="h-full min-h-0">{visible && <LiveMapContent instance={instance} isEditMode={isEditMode} />}</div>;
}

/** Fetch bounded provider results while mounted; shared query observers deduplicate visible gadgets. */
function LiveMapContent({ instance, isEditMode }: { instance: GadgetInstance; isEditMode: boolean }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const workspace = useWorkspace();
  const scope = instance.definition.scope ?? {};
  const providers = scope.live_map_providers ?? DEFAULT_PROVIDERS;
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [category, setCategory] = useState('all');
  const [days, setDays] = useState('7');
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 60_000); return () => window.clearInterval(timer); }, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [weatherState, setWeatherState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  useEffect(() => {
    if (!node) return;
    const resize = new ResizeObserver(([entry]) => setSize({ width: Math.floor(entry.contentRect.width), height: Math.floor(entry.contentRect.height) }));
    resize.observe(node);
    return () => resize.disconnect();
  }, [node]);
  const visible = true;
  const weatherLayer = instance.definition.filters?.map_weather_layer ?? null;
  const config = useQuery({ queryKey: ['live-map-config', workspace.selection?.id], queryFn: ({ signal }) => apiRequest<{ revision: number; terms_catalog: { id: string; attribution: string }[] }>('/api/v1/live-map/config', { signal }), enabled: visible && Boolean(workspace.selection?.id), staleTime: 60_000, gcTime: 0 });
  const results = useQueries({ queries: providers.map((provider) => ({
    queryKey: ['live-map', workspace.selection?.id, provider, scope.live_map_country, scope.live_map_github_owner, scope.live_map_github_repo, scope.live_map_sec_cik],
    queryFn: ({ signal }: { signal: AbortSignal }) => fetchLiveEvents(provider, scope, signal),
    enabled: visible && Boolean(workspace.selection?.id), staleTime: 60_000, gcTime: 0,
    refetchInterval: visible ? 60_000 : false, refetchIntervalInBackground: false, retry: false,
  })) });
  const entries = results.flatMap((result, index) => (result.data?.items ?? []).map((event) => ({ ...event, provider: providers[index], key: `${providers[index]}:${event.id}` })));
  const cutoff = now - Number(days) * 86_400_000;
  const filtered = entries.filter((event) => (category === 'all' || CATEGORY[event.provider] === category) && (!event.observed_at || !Number.isFinite(Date.parse(event.observed_at)) || Date.parse(event.observed_at) >= cutoff));
  // Keep geometry identity stable across status updates so map interaction is not reset.
  const pointJson = JSON.stringify(liveMapPoints(filtered));
  // Keep the map's point identity stable until its serialized geometry changes.
  // eslint-disable-next-line react-hooks/preserve-manual-memoization
  const points = useMemo(() => JSON.parse(pointJson) as ReturnType<typeof liveMapPoints>, [pointJson]);
  const selectedEvent = filtered.find((event) => event.key === selected);
  const engine = instance.definition.filters?.map_engine ?? 'globe';
  const mapProps = { focusSelected: true, features: points, selectedFeatureId: selected, onSelectFeature: setSelected, width: size.width, height: size.height, visible, interactive: !isEditMode, weatherLayer, weatherWorkspaceId: config.data ? workspace.selection?.id : undefined, weatherRevision: config.data?.revision, onWeatherStateChange: setWeatherState };
  return <section className="flex h-full min-h-0 flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
    <div className="flex shrink-0 flex-wrap gap-2">
      <Select value={category} onValueChange={setCategory}><SelectTrigger aria-label={t('liveMapCategory')} className="h-8 w-36"><SelectValue /></SelectTrigger><SelectContent>{(['all', 'geo', 'technology', 'finance'] as const).map((value) => <SelectItem key={value} value={value}>{t(`liveMapCategory_${value}`)}</SelectItem>)}</SelectContent></Select>
      <Button type="button" size="sm" variant="outline" disabled={!visible || results.some((result) => result.isFetching)} onClick={() => { for (const result of results) void result.refetch(); }}>{t('liveMapRefresh')}</Button>
      <Select value={days} onValueChange={setDays}><SelectTrigger aria-label={t('liveMapPeriod')} className="h-8 w-32"><SelectValue /></SelectTrigger><SelectContent>{['1', '7', '30'].map((value) => <SelectItem key={value} value={value}>{t('mapRange', { days: Number(value) })}</SelectItem>)}</SelectContent></Select>
    </div>
    <div ref={setNode} role="group" aria-label={t('mapPlotLabel')} className="relative min-h-48 flex-[2] overflow-hidden rounded border border-border">
      {visible && size.width > 0 && size.height > 0 && <div className="absolute inset-0"><Suspense fallback={<p role="status">{t('mapRendererLoading')}</p>}>{engine === 'globe' ? <GlobeMap {...mapProps} /> : <FlatMap {...mapProps} />}</Suspense></div>}
      {selectedEvent && <div className="absolute bottom-2 left-2 max-w-[90%] rounded bg-card/90 p-2 text-xs"><strong>{selectedEvent.title}</strong><p>{selectedEvent.summary}</p>{safeLiveLink(selectedEvent.url) && <a className="underline" href={safeLiveLink(selectedEvent.url) ?? undefined} target="_blank" rel="noreferrer">{t('liveMapOpenEvent')}</a>}{selectedEvent.sources?.map(({ url }) => safeLiveLink(url) && <a key={url} className="block underline" href={safeLiveLink(url) ?? undefined} target="_blank" rel="noreferrer">{t('liveMapProviderSource')}</a>)}</div>}
    </div>
    {weatherLayer && weatherState === 'ready' && <p className="text-xs text-muted-foreground">{t(`liveMapWeather_${weatherLayer}`)} · <a href="https://openweathermap.org/api/weathermaps" target="_blank" rel="noreferrer" className="underline">{t('liveMapWeatherLegend')}</a> · <a href="https://openweathermap.org/" target="_blank" rel="noreferrer" className="underline">{t('liveMapWeatherCredit')}</a> {config.data?.terms_catalog?.find((term) => term.id === 'openweather')?.attribution}</p>}
    {weatherLayer && (weatherState === 'error' || config.isError) && <p role="status" className="text-xs text-destructive">{t('liveMapWeatherError')}</p>}
    <div className="min-h-0 flex-1 overflow-y-auto"><ul aria-label={t('liveMapEvents')} className="divide-y divide-border">{filtered.map((event) => <li key={event.key} className="py-1 text-xs"><Button type="button" variant="ghost" className="h-auto w-full justify-start whitespace-normal p-1 text-left" onClick={() => setSelected(event.key)}>{event.title}</Button><span className="text-muted-foreground">{event.source} · {event.observed_at && Number.isFinite(Date.parse(event.observed_at)) ? <time dateTime={event.observed_at}>{formatDateTime(event.observed_at, display.locale, display.timezone)}</time> : t('liveMapTimeUnknown')}</span>{safeLiveLink(event.url) && <> · <a className="underline" href={safeLiveLink(event.url) ?? undefined} target="_blank" rel="noreferrer">{t('liveMapOpenEvent')}</a></>}{event.sources?.map(({ url }) => safeLiveLink(url) && <a key={url} className="block underline" href={safeLiveLink(url) ?? undefined} target="_blank" rel="noreferrer">{t('liveMapProviderSource')}</a>)}</li>)}</ul>{filtered.length === 0 && <p className="text-xs text-muted-foreground">{t('liveMapEmpty')}</p>}</div>
    <div className="max-h-20 shrink-0 overflow-y-auto text-xs text-muted-foreground" aria-label={t('liveMapSources')}>{providers.map((provider, index) => <p key={provider}><a className="underline" href={ATTRIBUTION[provider]} target="_blank" rel="noreferrer">{PROVIDER_NAME[provider] ?? provider}</a>: {results[index].isError ? t(liveErrorKey(results[index].error, provider)) : results[index].data ? t('liveMapSourceUpdated', { when: formatDateTime(results[index].data.fetched_at, display.locale, display.timezone) }) : t('liveMapSourceLoading')}{config.data?.terms_catalog?.find((term) => term.id === provider)?.attribution && <span className="block">{config.data.terms_catalog.find((term) => term.id === provider)?.attribution}</span>}{results[index].data?.truncated ? ` · ${t('observationTruncated')}` : ''}</p>)}</div>
  </section>;
}
