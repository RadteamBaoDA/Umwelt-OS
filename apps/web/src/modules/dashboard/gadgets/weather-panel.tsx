'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { Button } from '@/components/ui/button';
import { TitleBarSlot } from '@/components/gadget-title-slot';
import { listWorldObservationWindow, type WorldObservation } from '@/modules/observations';
import { listAllSources, sourceKeys } from '@/modules/sources';
import type { GadgetInstance } from '../api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { Sparkline } from './sparkline';
import { staleInfo } from './chart-range';
import { useSourceInfo } from './use-effective-lookback';
import { getDateTimeFormat, getNumberFormat } from '@/modules/shared';

const DAY_MS = 86_400_000;

/** One reading that has a usable numeric value. */
interface Reading {
  ts: number;
  value: number;
  unit: string;
  zone: string | null;
}

/**
 * Keeps the readings of one metric family that carry a real value, ascending by time.
 *
 * @param items Open-Meteo observations.
 * @param match Predicate on the metric id.
 * @returns Ascending readings; missing/null values are dropped.
 */
function readingsOf(items: WorldObservation[], match: (metric: string) => boolean): Reading[] {
  return items
    .filter((item) => match(item.metric) && item.value !== null && item.quality !== 'missing')
    .map((item) => ({ ts: Date.parse(item.observed_at), value: item.value as number, unit: item.unit, zone: item.timezone }))
    .filter((reading) => Number.isFinite(reading.ts))
    .sort((a, b) => a.ts - b.ts);
}

/**
 * Reading closest in time to `now` (the current conditions); ties go to the earlier reading.
 *
 * @param readings Ascending readings.
 * @param now Epoch ms.
 * @returns The nearest reading, or undefined when empty.
 */
export function nearestReading<T extends { ts: number }>(readings: T[], now: number): T | undefined {
  return readings.reduce<T | undefined>(
    (best, reading) => (!best || Math.abs(reading.ts - now) < Math.abs(best.ts - now) ? reading : best),
    undefined,
  );
}

/**
 * Weather gadget dispatcher: one bound source renders the full panel, 2+ (up to 5 cities) a compact per-city list.
 * Splitting keeps each body's hook order fixed.
 *
 * @param props Gadget instance whose source ids and metric scope select the Open-Meteo readings.
 * @returns Single-city panel or the multi-city list.
 */
export function WeatherPanel(props: { instance: GadgetInstance }) {
  return (props.instance.definition.source_ids?.length ?? 0) > 1 ? (
    <WeatherCityList instance={props.instance} />
  ) : (
    <SingleWeatherPanel {...props} />
  );
}

/**
 * Compact multi-city list: one row per bound source (name, current temperature, today's low/high, 24h sparkline).
 * Each row owns its query so one failing city never blanks the others.
 *
 * @param props Gadget instance with 2+ Open-Meteo sources.
 * @returns Accessible list of city rows.
 */
function WeatherCityList({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const ids = instance.definition.source_ids ?? [];
  const info = useSourceInfo(ids);
  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      <ul className="divide-y divide-border" aria-label={t('weather.cities')}>
        {ids.map((id, index) => (
          <WeatherCityRow
            key={id}
            instanceId={instance.id}
            sourceId={id}
            name={info.data?.[id]?.name || `${t('weather.title')} ${index + 1}`}
            metrics={instance.definition.scope?.metrics ?? []}
            lookbackDays={Math.min(instance.definition.scope?.lookback_days ?? 3, 3)}
          />
        ))}
      </ul>
      <a className="mt-auto text-xs text-muted-foreground underline" href="https://open-meteo.com/" target="_blank" rel="noreferrer">
        {t('openMeteoAttribution')}
      </a>
    </section>
  );
}

/**
 * One city row of {@link WeatherCityList}: reads its own source's readings and shows the reading nearest to now,
 * today's low/high in the reading's timezone and a 24h temperature sparkline.
 *
 * @param props Source to read, its display name and the shared metric/lookback scope.
 * @returns List item, with a placeholder while loading or on failure/no data.
 */
function WeatherCityRow({
  instanceId,
  sourceId,
  name,
  metrics,
  lookbackDays,
}: {
  instanceId: string;
  sourceId: string;
  name: string;
  metrics: string[];
  lookbackDays: number;
}) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const query = useQuery({
    queryKey: ['world-observations', 'weather', instanceId, sourceId, metrics, lookbackDays],
    queryFn: ({ signal }) =>
      listWorldObservationWindow(
        { sourceIds: [sourceId], metrics, from: new Date(Date.now() - DAY_MS), to: new Date(Date.now() + lookbackDays * DAY_MS) },
        signal,
      ),
    staleTime: 60_000,
  });
  const items = (query.data?.items ?? []).filter((item) => item.source_id === sourceId && item.provider === 'open_meteo');
  const temps = readingsOf(items, (m) => m.startsWith('temperature'));
  const now = query.dataUpdatedAt || Math.max(...temps.map((r) => r.ts), 0);
  const current = nearestReading(temps, now);
  const zone = current?.zone ?? display.timezone;
  /** Calendar-day key in the reading's timezone. */
  const dayKey = (ts: number) => getDateTimeFormat('en-CA', { dateStyle: 'short', timeZone: zone }).format(new Date(ts));
  const todays = temps.filter((r) => dayKey(r.ts) === dayKey(now));
  const unit = current?.unit ?? '';
  const next24h = temps.filter((r) => r.ts >= now && r.ts <= now + DAY_MS).map((r) => ({ timestamp: r.ts, value: r.value }));
  return (
    <li className="flex items-center gap-3 py-2" data-testid="weather-city">
      <span className="min-w-0 flex-1 truncate text-sm text-foreground">{name}</span>
      {query.isPending ? (
        <span className="text-xs text-muted-foreground">…</span>
      ) : current ? (
        <>
          {next24h.length >= 2 && (
            <span className="hidden w-20 shrink-0 @[24rem]/gadget:block" role="img" aria-label={t('weather.next24h')}>
              <Sparkline data={next24h} height={28} forecastFrom={now} line />
            </span>
          )}
          {todays.length > 0 && (
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {t('weather.lowHigh', {
                min: `${Math.round(Math.min(...todays.map((r) => r.value)))}${unit}`,
                max: `${Math.round(Math.max(...todays.map((r) => r.value)))}${unit}`,
              })}
            </span>
          )}
          <span className="w-16 shrink-0 text-right text-sm font-semibold tabular-nums text-foreground" data-testid="weather-city-temp">
            {Math.round(current.value * 10) / 10}
            {unit}
          </span>
        </>
      ) : (
        <span className="text-xs text-muted-foreground">{query.isError ? t('observation.loadFailed') : t('weather.missingValue')}</span>
      )}
    </li>
  );
}

/** Chart row per hour: temperature split into a solid past and a dashed forecast, plus precipitation. */
type ChartRow = { ts: number; past: number | null; future: number | null; precip: number | null };
/** Min/max temperature of one calendar day (in the reading's timezone). */
type DayRange = { ts: number; min: number; max: number };

/** Everything the weather panel renders, derived purely from the loaded readings. */
interface WeatherModel {
  heroTemp: Reading | undefined;
  heroWind: Reading | undefined;
  heroPrecip: Reading | undefined;
  zone: string;
  chartRows: ChartRow[];
  dayRows: DayRange[];
  spanMin: number;
  span: number;
  today: DayRange | undefined;
  unit: string;
  next24h: { timestamp: number; value: number }[];
}

/** Calendar-day key in a timezone, used to group readings into day rows. */
function dayKeyIn(zone: string, ts: number): string {
  return getDateTimeFormat('en-CA', { dateStyle: 'short', timeZone: zone }).format(new Date(ts));
}

/** One chart row per hour: temperature split at the forecast boundary (the shared point stays in both series). */
function buildChartRows(temps: Reading[], precips: Reading[], now: number): ChartRow[] {
  const rowsByTs = new Map<number, ChartRow>();
  /** Existing chart row for an hour, or an empty row so temperature and precipitation merge per timestamp. */
  const rowAt = (ts: number) => rowsByTs.get(ts) ?? { ts, past: null, future: null, precip: null };
  const boundary = nearestReading(temps, now)?.ts ?? now;
  for (const r of temps)
    rowsByTs.set(r.ts, { ...rowAt(r.ts), past: r.ts <= boundary ? r.value : null, future: r.ts >= boundary ? r.value : null });
  for (const r of precips) rowsByTs.set(r.ts, { ...rowAt(r.ts), precip: r.value });
  return [...rowsByTs.values()].sort((a, b) => a.ts - b.ts);
}

/** Per-day min/max temperature, from yesterday's end onward, oldest first. */
function buildDayRows(temps: Reading[], zone: string, now: number): DayRange[] {
  const days = new Map<string, DayRange>();
  for (const r of temps) {
    const key = dayKeyIn(zone, r.ts);
    const day = days.get(key);
    days.set(
      key,
      day ? { ts: day.ts, min: Math.min(day.min, r.value), max: Math.max(day.max, r.value) } : { ts: r.ts, min: r.value, max: r.value },
    );
  }
  return [...days.values()].filter((day) => day.ts >= now - DAY_MS).sort((a, b) => a.ts - b.ts);
}

/**
 * Derives hero readings, chart rows, day ranges and the next-24h series. Pure.
 *
 * @param items Open-Meteo observations of the configured source.
 * @param now Epoch ms of the data fetch (kept out of render so it stays pure).
 * @param displayTimezone Fallback timezone when the hero reading carries none.
 */
function buildWeatherModel(items: WorldObservation[], now: number, displayTimezone: string): WeatherModel {
  const temps = readingsOf(items, (m) => m.startsWith('temperature'));
  const precips = readingsOf(items, (m) => m.startsWith('precipitation'));
  const winds = readingsOf(items, (m) => m.startsWith('wind_speed'));
  const heroTemp = nearestReading(temps, now);
  const zone = heroTemp?.zone ?? displayTimezone;
  const dayRows = buildDayRows(temps, zone, now);
  const spanMin = Math.min(...dayRows.map((d) => d.min));
  const spanMax = Math.max(...dayRows.map((d) => d.max));
  return {
    heroTemp,
    heroWind: nearestReading(winds, now),
    heroPrecip: nearestReading(precips, now),
    zone,
    chartRows: buildChartRows(temps, precips, now),
    dayRows,
    spanMin,
    span: spanMax - spanMin || 1,
    // Compact strip: today's low/high (calendar day in the reading zone) and the next 24h for a sparkline.
    today: dayRows.find((day) => dayKeyIn(zone, day.ts) === dayKeyIn(zone, now)),
    unit: heroTemp?.unit ?? '',
    next24h: temps.filter((r) => r.ts >= now && r.ts <= now + DAY_MS).map((r) => ({ timestamp: r.ts, value: r.value })),
  };
}

/** Loads the Open-Meteo readings of the first source and reports the newest collection time as freshness. */
function useWeatherReadings(instance: GadgetInstance) {
  const definition = instance.definition;
  const sourceId = definition.source_ids?.[0] ?? '';
  const metrics = definition.scope?.metrics ?? [];
  const lookbackDays = Math.min(definition.scope?.lookback_days ?? 3, 3);
  const query = useQuery({
    queryKey: ['world-observations', 'weather', instance.id, sourceId, metrics, lookbackDays],
    queryFn: ({ signal }) => {
      const to = new Date(Date.now() + lookbackDays * DAY_MS);
      const from = new Date(Date.now() - DAY_MS);
      return listWorldObservationWindow({ sourceIds: [sourceId], metrics, from, to }, signal);
    },
    enabled: Boolean(sourceId),
    staleTime: 60_000,
  });
  const items = (query.data?.items ?? []).filter((item) => item.source_id === sourceId && item.provider === 'open_meteo');
  useReportGadgetFreshness(items.reduce<string | null>((max, item) => (!max || item.collected_at > max ? item.collected_at : max), null));
  return { sourceId, query, items };
}

/**
 * Weather body (demo `.pane.weather`, both skins): big current temperature, one muted summary line (low, high,
 * rain, wind), then the next-24h temperature line with `now … +24h` ticks. Presentation of the derived model; the Open-Meteo attribution link stays as a small muted footer.
 *
 * @param props Derived model, the shared number format and the display timezone/locale for the mid tick.
 * @returns Compact weather section.
 */
function WeatherTerminal({
  model,
  now,
  locale,
}: {
  model: WeatherModel;
  now: number;
  locale: ReturnType<typeof useDisplayPreferences>['locale'];
}) {
  const t = useTranslations('dashboard');
  const { heroTemp, heroWind, heroPrecip, today, unit, next24h, zone } = model;
  const parts = [
    today && t('weather.termLowHigh', { min: Math.round(today.min), max: Math.round(today.max) }),
    heroPrecip && t('weather.termRain', { value: Math.round(heroPrecip.value) }),
    heroWind && t('weather.termWind', { value: Math.round(heroWind.value * 10) / 10, unit: heroWind.unit }),
  ].filter(Boolean);
  const mid = getDateTimeFormat(normalizeFormattingLocale(locale), {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: zone,
  }).format(new Date(now + DAY_MS / 2));
  return (
    <section className="weather-term flex h-full min-h-0 flex-col gap-1 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      <p className="weather-term-figure m-0" data-testid="weather-hero">
        {heroTemp ? Math.round(heroTemp.value * 10) / 10 : '—'}
        <small>{unit}</small>
      </p>
      {parts.length > 0 && (
        <p className="weather-term-line m-0" data-testid="weather-minmax">
          {parts.join(' · ')}
        </p>
      )}
      {next24h.length >= 2 && (
        <div role="img" aria-label={t('weather.next24h')} data-testid="weather-sparkline">
          <Sparkline data={next24h} height={64} forecastFrom={now} line />
          <div className="weather-term-ticks" aria-hidden="true">
            <span>{t('weather.termNow')}</span>
            <span>{mid}</span>
            <span>+24h</span>
          </div>
        </div>
      )}
      <a className="weather-term-attr mt-auto" href="https://open-meteo.com/" target="_blank" rel="noreferrer">
        {t('openMeteoAttribution')}
      </a>
    </section>
  );
}

/**
 * Muted "last good data 2h ago · next retry 13:20:00" line for a failing weather read. Facts come from the bound source
 * row in the shared `sourceKeys.listAll` cache (the one the dashboard already loads; no extra request when it is warm).
 *
 * @param props Bound source id; `failingOnly` hides the line unless the source's latest error is newer than its latest
 *   success (used on the empty branch, where a read succeeded but the collector is failing).
 * @returns The line, or null when the source row carries neither a last success nor a next due time.
 */
function WeatherStaleLine({ sourceId, failingOnly = false }: { sourceId: string; failingOnly?: boolean }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const sources = useQuery({ queryKey: sourceKeys.listAll, queryFn: listAllSources, enabled: Boolean(sourceId) });
  // Wall-clock facts are judged as of the source list fetch so render stays pure.
  const source = sources.data?.items.find((item) => item.id === sourceId);
  const info = staleInfo(source, sources.dataUpdatedAt);
  if (!info) return null;
  // Same "failing" rule as the frame badge and statusline: an error newer than the last success.
  if (
    failingOnly &&
    !(source?.last_error_at && (!source.last_success_at || Date.parse(source.last_error_at) > Date.parse(source.last_success_at)))
  )
    return null;
  const next =
    info.nextDueMs === null
      ? null
      : getDateTimeFormat(normalizeFormattingLocale(display.locale), {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
          timeZone: display.timezone,
        }).format(new Date(info.nextDueMs));
  const text = [info.ago && t('weather.lastGood', { ago: info.ago }), next && t('weather.nextRetry', { next })].filter(Boolean).join(' · ');
  return (
    <p className="m-0 text-xs text-muted-foreground" data-testid="weather-stale-line">
      {text}
    </p>
  );
}

/**
 * Last successful temperature, kept by react-query across a failed refetch, as `16.2°C · stale`.
 *
 * @param props Cached readings and the epoch ms they were fetched (the "now" for picking the nearest reading).
 * @returns The muted reading, or null when there is no temperature reading.
 */
function WeatherStaleReading({ items, now }: { items: WorldObservation[]; now: number }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const { heroTemp } = buildWeatherModel(items, now, display.timezone);
  if (!heroTemp) return null;
  const value = getNumberFormat(display.locale, { maximumFractionDigits: 1, minimumFractionDigits: 1 }).format(heroTemp.value);
  return (
    <p className="weather-stale-reading m-0 px-3 text-2xl text-muted-foreground tabular-nums" data-testid="weather-stale-reading">
      {t('weather.staleReading', { value, unit: heroTemp.unit })}
    </p>
  );
}

/**
 * Weather gadget: big current temperature, one muted summary line (low, high, rain, wind) and the next-24h line with
 * `now … +24h` ticks; the Open-Meteo attribution is the body link only.
 *
 * @param props Gadget instance whose first source and metric scope select the Open-Meteo readings.
 * On a failed read the error notice also lists the bound source's last good data age and next retry, and the last
 * cached temperature as `16.2°C · stale` when react-query still holds it.
 *
 * @returns Weather panel, or a setup/loading/error/empty notice.
 */
function SingleWeatherPanel({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const { sourceId, query, items } = useWeatherReadings(instance);

  if (!sourceId) return <GadgetNotice kind="setup" message={t('weather.sourceRequired')} />;
  if (query.isPending) return <GadgetSkeleton variant="list" label={t('observation.loading')} />;
  if (query.isError)
    return (
      <>
        {/* `[ retry ]` in the pane title bar (demo weather error pane); refetches the same query. */}
        <TitleBarSlot>
          <span className="gadget-failed" aria-hidden="true">
            {t('gadget.metaFailed')}
          </span>
          <Button type="button" size="sm" onClick={() => void query.refetch()}>
            {t('retry')}
          </Button>
        </TitleBarSlot>
        <GadgetNotice kind="error" message={t('observation.loadFailed')} detail={<WeatherStaleLine sourceId={sourceId} />} />
        {/* react-query keeps the last good data on a failed refetch: show that reading, muted, instead of nothing. */}
        {items.length > 0 && <WeatherStaleReading items={items} now={query.dataUpdatedAt} />}
      </>
    );
  if (!items.length)
    return <GadgetNotice kind="empty" message={t('observation.empty')} detail={<WeatherStaleLine sourceId={sourceId} failingOnly />} />;

  // Fetch time of the data, not wall-clock in render: keeps render pure; falls back to the newest reading when unset.
  const now = query.dataUpdatedAt || Math.max(...items.map((item) => Date.parse(item.observed_at)).filter(Number.isFinite), 0);
  const model = buildWeatherModel(items, now, display.timezone);
  return <WeatherTerminal model={model} now={now} locale={display.locale} />;
}
