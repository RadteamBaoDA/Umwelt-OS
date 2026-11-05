'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Bar, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from 'recharts';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow, type WorldObservation } from '@/modules/observations';
import type { GadgetInstance } from '../api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { Sparkline } from './sparkline';
import { formatMetricLabel } from './metric-label';
import { useSourceInfo } from './use-effective-lookback';
import { getDateTimeFormat, getNumberFormat } from '@/modules/shared';

const DAY_MS = 86_400_000;

/** One reading that has a usable numeric value. */
interface Reading { ts: number; value: number; unit: string; zone: string | null }

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
  return readings.reduce<T | undefined>((best, reading) => (!best || Math.abs(reading.ts - now) < Math.abs(best.ts - now) ? reading : best), undefined);
}

/**
 * Weather gadget dispatcher: one bound source renders the full panel, 2+ (up to 5 cities) a compact per-city list.
 * Splitting keeps each body's hook order fixed.
 *
 * @param props Gadget instance whose source ids and metric scope select the Open-Meteo readings.
 * @returns Single-city panel or the multi-city list.
 */
export function WeatherPanel(props: { instance: GadgetInstance }) {
  return (props.instance.definition.source_ids?.length ?? 0) > 1 ? <WeatherCityList instance={props.instance} /> : <SingleWeatherPanel {...props} />;
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
  return <section className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
    <ul className="divide-y divide-border" aria-label={t('weatherCities')}>
      {ids.map((id, index) => <WeatherCityRow key={id} instanceId={instance.id} sourceId={id} name={info.data?.[id]?.name || `${t('weatherTitle')} ${index + 1}`}
        metrics={instance.definition.scope?.metrics ?? []} lookbackDays={Math.min(instance.definition.scope?.lookback_days ?? 3, 3)} />)}
    </ul>
    <a className="mt-auto text-xs text-muted-foreground underline" href="https://open-meteo.com/" target="_blank" rel="noreferrer">{t('openMeteoAttribution')}</a>
  </section>;
}

/**
 * One city row of {@link WeatherCityList}: reads its own source's readings and shows the reading nearest to now,
 * today's low/high in the reading's timezone and a 24h temperature sparkline.
 *
 * @param props Source to read, its display name and the shared metric/lookback scope.
 * @returns List item, with a placeholder while loading or on failure/no data.
 */
function WeatherCityRow({ instanceId, sourceId, name, metrics, lookbackDays }: { instanceId: string; sourceId: string; name: string; metrics: string[]; lookbackDays: number }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const query = useQuery({
    queryKey: ['world-observations', 'weather', instanceId, sourceId, metrics, lookbackDays],
    queryFn: ({ signal }) => listWorldObservationWindow({ sourceIds: [sourceId], metrics, from: new Date(Date.now() - DAY_MS), to: new Date(Date.now() + lookbackDays * DAY_MS) }, signal),
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
  return <li className="flex items-center gap-3 py-2" data-testid="weather-city">
    <span className="min-w-0 flex-1 truncate text-sm text-foreground">{name}</span>
    {query.isPending ? <span className="text-xs text-muted-foreground">…</span> : current ? <>
      {next24h.length >= 2 && <span className="hidden w-20 shrink-0 @[24rem]/gadget:block" role="img" aria-label={t('weatherNext24h')}><Sparkline data={next24h} height={28} forecastFrom={now} line /></span>}
      {todays.length > 0 && <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{t('weatherLowHigh', { min: `${Math.round(Math.min(...todays.map((r) => r.value)))}${unit}`, max: `${Math.round(Math.max(...todays.map((r) => r.value)))}${unit}` })}</span>}
      <span className="w-16 shrink-0 text-right text-sm font-semibold tabular-nums text-foreground" data-testid="weather-city-temp">{Math.round(current.value * 10) / 10}{unit}</span>
    </> : <span className="text-xs text-muted-foreground">{query.isError ? t('observationLoadFailed') : t('weatherMissingValue')}</span>}
  </li>;
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
  for (const r of temps) rowsByTs.set(r.ts, { ...rowAt(r.ts), past: r.ts <= boundary ? r.value : null, future: r.ts >= boundary ? r.value : null });
  for (const r of precips) rowsByTs.set(r.ts, { ...rowAt(r.ts), precip: r.value });
  return [...rowsByTs.values()].sort((a, b) => a.ts - b.ts);
}

/** Per-day min/max temperature, from yesterday's end onward, oldest first. */
function buildDayRows(temps: Reading[], zone: string, now: number): DayRange[] {
  const days = new Map<string, DayRange>();
  for (const r of temps) {
    const key = dayKeyIn(zone, r.ts);
    const day = days.get(key);
    days.set(key, day ? { ts: day.ts, min: Math.min(day.min, r.value), max: Math.max(day.max, r.value) } : { ts: r.ts, min: r.value, max: r.value });
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

/** Temperature line plus precipitation bars on one time axis (medium containers and up). */
function WeatherTimeline({ rows, now, tickFormat }: { rows: ChartRow[]; now: number; tickFormat: (ts: number) => string }) {
  const t = useTranslations('dashboard');
  return <div className="hidden h-36 shrink-0 @[24rem]/gadget:block" role="img" aria-label={t('weatherTimeline')} data-testid="weather-timeline">
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={tickFormat} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} minTickGap={32} />
        <YAxis yAxisId="temp" width={32} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} domain={['auto', 'auto']} />
        <YAxis yAxisId="precip" orientation="right" hide domain={[0, (max: number) => Math.max(max * 3, 1)]} />
        <Bar yAxisId="precip" dataKey="precip" fill="var(--muted-foreground)" fillOpacity={0.35} isAnimationActive={false} />
        <Line yAxisId="temp" dataKey="past" stroke="var(--chart-1)" strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
        <Line yAxisId="temp" dataKey="future" stroke="var(--chart-1)" strokeWidth={1.5} strokeDasharray="3 3" dot={false} isAnimationActive={false} connectNulls />
        <ReferenceLine yAxisId="temp" x={now} stroke="var(--muted-foreground)" strokeDasharray="2 2" />
      </ComposedChart>
    </ResponsiveContainer>
  </div>;
}

/** Large containers: one min-max row per day, computed client-side from the hourly readings. */
function WeatherDays({ model, weekday }: { model: WeatherModel; weekday: Intl.DateTimeFormat }) {
  const t = useTranslations('dashboard');
  const { dayRows, spanMin, span } = model;
  return <ul className="hidden shrink-0 divide-y divide-border @[40rem]/gadget:block" aria-label={t('weatherDays')}>
    {dayRows.map((day) => <li key={day.ts} className="flex items-center gap-3 py-1.5 text-sm tabular-nums" data-testid="weather-day">
      <span className="w-12 shrink-0 text-muted-foreground">{weekday.format(new Date(day.ts))}</span>
      <span className="w-10 shrink-0 text-right text-muted-foreground">{Math.round(day.min)}</span>
      <span className="relative h-1.5 min-w-0 flex-1 rounded-full bg-muted" aria-hidden="true">
        <span className="absolute inset-y-0 rounded-full bg-chart-1" style={{ left: `${((day.min - spanMin) / span) * 100}%`, width: `${Math.max(((day.max - day.min) / span) * 100, 4)}%` }} />
      </span>
      <span className="w-10 shrink-0 text-foreground">{Math.round(day.max)}</span>
    </li>)}
  </ul>;
}

/** Hero block: current temperature, with wind and precipitation beside it. */
function WeatherHero({ model, number }: { model: WeatherModel; number: Intl.NumberFormat }) {
  const t = useTranslations('dashboard');
  const { heroTemp, heroWind, heroPrecip } = model;
  /** Formats a reading as `value unit`, or the localized missing-value placeholder. */
  const withUnit = (r: Reading | undefined) => (r ? `${number.format(r.value)} ${r.unit}` : t('weatherMissingValue'));
  return <div className="flex flex-wrap items-end gap-x-4 gap-y-1" aria-label={t('weatherForecastReadings')} role="group">
    <div className="min-w-0">
      <div className="text-xs text-muted-foreground">{t('weatherNow')}</div>
      <div className="text-figure text-foreground" data-testid="weather-hero">
        {heroTemp ? <>{number.format(heroTemp.value)}<span className="ml-1 text-sm font-normal text-muted-foreground">{heroTemp.unit}</span></> : '—'}
      </div>
    </div>
    <dl className="flex flex-col gap-0.5 pb-1 text-xs text-muted-foreground tabular-nums">
      {heroWind && <div><dt className="inline">{formatMetricLabel('wind_speed_10m', t)}: </dt><dd className="inline">{withUnit(heroWind)}</dd></div>}
      {heroPrecip && <div><dt className="inline">{formatMetricLabel('precipitation', t)}: </dt><dd className="inline">{withUnit(heroPrecip)}</dd></div>}
    </dl>
  </div>;
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
    enabled: Boolean(sourceId), staleTime: 60_000,
  });
  const items = (query.data?.items ?? []).filter((item) => item.source_id === sourceId && item.provider === 'open_meteo');
  useReportGadgetFreshness(items.reduce<string | null>((max, item) => (!max || item.collected_at > max ? item.collected_at : max), null));
  return { sourceId, query, items };
}

/**
 * Weather gadget: current temperature hero with wind/precipitation beside it, a combined timeline (temperature line
 * past solid / forecast dashed, precipitation bars, "now" marker) and per-day min-max rows. The window runs from 24h
 * back to the configured forecast horizon (max 3 days). Container width decides what shows: small = hero only,
 * medium = + timeline, large = + day rows. Open-Meteo attribution is the body link only.
 *
 * @param props Gadget instance whose first source and metric scope select the Open-Meteo readings.
 * @returns Weather panel, or a setup/loading/error/empty notice.
 */
function SingleWeatherPanel({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const { sourceId, query, items } = useWeatherReadings(instance);
  const formatLocale = normalizeFormattingLocale(display.locale);
  const number = getNumberFormat(display.locale, { maximumFractionDigits: 1, minimumFractionDigits: 1 });

  if (!sourceId) return <GadgetNotice kind="setup" message={t('weatherSourceRequired')} />;
  if (query.isPending) return <GadgetSkeleton variant="list" label={t('observationLoading')} />;
  if (query.isError) return <GadgetNotice kind="error" message={t('observationLoadFailed')} />;
  if (!items.length) return <GadgetNotice kind="empty" message={t('observationEmpty')} />;

  // Fetch time of the data, not wall-clock in render: keeps render pure; falls back to the newest reading when unset.
  const now = query.dataUpdatedAt || Math.max(...items.map((item) => Date.parse(item.observed_at)).filter(Number.isFinite), 0);
  const model = buildWeatherModel(items, now, display.timezone);
  const { today, unit, next24h, chartRows, dayRows, zone } = model;
  const weekday = getDateTimeFormat(formatLocale, { weekday: 'short', timeZone: zone });
  /** X-axis tick label: short weekday in the reading's timezone. */
  const tickFormat = (ts: number) => weekday.format(new Date(ts));

  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
    {query.data?.truncated && <p role="status" className="text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    <WeatherHero model={model} number={number} />

    {/* All sizes: today's low/high; the sparkline fills in for small containers that lack the full timeline. */}
    {today && <p className="m-0 text-xs text-muted-foreground tabular-nums" data-testid="weather-minmax">{t('weatherLowHigh', { min: `${Math.round(today.min)}${unit}`, max: `${Math.round(today.max)}${unit}` })}</p>}
    {next24h.length >= 2 && <div className="shrink-0 @[24rem]/gadget:hidden" role="img" aria-label={t('weatherNext24h')} data-testid="weather-sparkline"><Sparkline data={next24h} height={40} forecastFrom={now} line /></div>}

    {/* Medium containers and up: temperature line + precipitation bars on one time axis. */}
    {chartRows.length >= 2 && <WeatherTimeline rows={chartRows} now={now} tickFormat={tickFormat} />}

    {/* Large containers: one min-max row per day, computed client-side from the hourly readings. */}
    {dayRows.length > 0 && <WeatherDays model={model} weekday={weekday} />}

    <a className="mt-auto text-xs text-muted-foreground underline" href="https://open-meteo.com/" target="_blank" rel="noreferrer">{t('openMeteoAttribution')}</a>
  </section>;
}
