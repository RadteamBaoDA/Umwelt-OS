'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { Bar, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from 'recharts';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow, type WorldObservation } from '@/modules/observations/api';
import type { GadgetInstance } from '../api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { Sparkline } from './sparkline';
import { formatMetricLabel } from './metric-label';

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
 * Weather gadget: current temperature hero with wind/precipitation beside it, a combined timeline (temperature line
 * past solid / forecast dashed, precipitation bars, "now" marker) and per-day min-max rows. The window runs from 24h
 * back to the configured forecast horizon (max 3 days). Container width decides what shows: small = hero only,
 * medium = + timeline, large = + day rows. Open-Meteo attribution is the body link only.
 *
 * @param props Gadget instance whose first source and metric scope select the Open-Meteo readings.
 * @returns Weather panel, or a setup/loading/error/empty notice.
 */
export function WeatherPanel({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
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

  const formatLocale = normalizeFormattingLocale(display.locale);
  const number = new Intl.NumberFormat(display.locale, { maximumFractionDigits: 1, minimumFractionDigits: 1 });

  if (!sourceId) return <GadgetNotice kind="setup" message={t('weatherSourceRequired')} />;
  if (query.isPending) return <GadgetSkeleton variant="list" label={t('observationLoading')} />;
  if (query.isError) return <GadgetNotice kind="error" message={t('observationLoadFailed')} />;
  if (!items.length) return <GadgetNotice kind="empty" message={t('observationEmpty')} />;

  // Fetch time of the data, not wall-clock in render: keeps render pure; falls back to the newest reading when unset.
  const now = query.dataUpdatedAt || Math.max(...items.map((item) => Date.parse(item.observed_at)).filter(Number.isFinite), 0);
  const temps = readingsOf(items, (m) => m.startsWith('temperature'));
  const precips = readingsOf(items, (m) => m.startsWith('precipitation'));
  const winds = readingsOf(items, (m) => m.startsWith('wind_speed'));
  const heroTemp = nearestReading(temps, now);
  const heroWind = nearestReading(winds, now);
  const heroPrecip = nearestReading(precips, now);
  const zone = heroTemp?.zone ?? display.timezone;

  // One chart row per hour: temperature split into a solid past and a dashed forecast sharing the boundary point.
  const rowsByTs = new Map<number, { ts: number; past: number | null; future: number | null; precip: number | null }>();
  /** Existing chart row for an hour, or an empty row so temperature and precipitation merge per timestamp. */
  const rowAt = (ts: number) => rowsByTs.get(ts) ?? { ts, past: null, future: null, precip: null };
  const boundary = nearestReading(temps, now)?.ts ?? now;
  for (const r of temps) rowsByTs.set(r.ts, { ...rowAt(r.ts), past: r.ts <= boundary ? r.value : null, future: r.ts >= boundary ? r.value : null });
  for (const r of precips) rowsByTs.set(r.ts, { ...rowAt(r.ts), precip: r.value });
  const chartRows = [...rowsByTs.values()].sort((a, b) => a.ts - b.ts);

  /** Calendar-day key in the reading's timezone, used to group readings into day rows. */
  const dayKey = (ts: number) => new Intl.DateTimeFormat('en-CA', { dateStyle: 'short', timeZone: zone }).format(new Date(ts));
  const days = new Map<string, { ts: number; min: number; max: number }>();
  for (const r of temps) {
    const key = dayKey(r.ts);
    const day = days.get(key);
    days.set(key, day ? { ts: day.ts, min: Math.min(day.min, r.value), max: Math.max(day.max, r.value) } : { ts: r.ts, min: r.value, max: r.value });
  }
  const dayRows = [...days.values()].filter((day) => day.ts >= now - DAY_MS).sort((a, b) => a.ts - b.ts);
  const spanMin = Math.min(...dayRows.map((d) => d.min));
  const spanMax = Math.max(...dayRows.map((d) => d.max));
  const span = spanMax - spanMin || 1;
  const weekday = new Intl.DateTimeFormat(formatLocale, { weekday: 'short', timeZone: zone });
  /** X-axis tick label: short weekday in the reading's timezone. */
  const tickFormat = (ts: number) => weekday.format(new Date(ts));
  // Compact strip: today's low/high (calendar day in the reading zone) and the next 24h for a sparkline.
  const today = dayRows.find((day) => dayKey(day.ts) === dayKey(now));
  const unit = heroTemp?.unit ?? '';
  const next24h = temps.filter((r) => r.ts >= now && r.ts <= now + DAY_MS).map((r) => ({ timestamp: r.ts, value: r.value }));
  /** Formats a reading as `value unit`, or the localized missing-value placeholder. */
  const withUnit = (r: Reading | undefined) => (r ? `${number.format(r.value)} ${r.unit}` : t('weatherMissingValue'));

  return <section className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
    {query.data?.truncated && <p role="status" className="text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    <div className="flex flex-wrap items-end gap-x-4 gap-y-1" aria-label={t('weatherForecastReadings')} role="group">
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
    </div>

    {/* All sizes: today's low/high; the sparkline fills in for small containers that lack the full timeline. */}
    {today && <p className="m-0 text-xs text-muted-foreground tabular-nums" data-testid="weather-minmax">{t('weatherLowHigh', { min: `${Math.round(today.min)}${unit}`, max: `${Math.round(today.max)}${unit}` })}</p>}
    {next24h.length >= 2 && <div className="shrink-0 @[24rem]/gadget:hidden" role="img" aria-label={t('weatherNext24h')} data-testid="weather-sparkline"><Sparkline data={next24h} height={40} forecastFrom={now} line /></div>}

    {/* Medium containers and up: temperature line + precipitation bars on one time axis. */}
    {chartRows.length >= 2 && <div className="hidden h-36 shrink-0 @[24rem]/gadget:block" role="img" aria-label={t('weatherTimeline')} data-testid="weather-timeline">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={chartRows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
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
    </div>}

    {/* Large containers: one min-max row per day, computed client-side from the hourly readings. */}
    {dayRows.length > 0 && <ul className="hidden shrink-0 divide-y divide-border @[40rem]/gadget:block" aria-label={t('weatherDays')}>
      {dayRows.map((day) => <li key={day.ts} className="flex items-center gap-3 py-1.5 text-sm tabular-nums" data-testid="weather-day">
        <span className="w-12 shrink-0 text-muted-foreground">{weekday.format(new Date(day.ts))}</span>
        <span className="w-10 shrink-0 text-right text-muted-foreground">{Math.round(day.min)}</span>
        <span className="relative h-1.5 min-w-0 flex-1 rounded-full bg-muted" aria-hidden="true">
          <span className="absolute inset-y-0 rounded-full bg-chart-1" style={{ left: `${((day.min - spanMin) / span) * 100}%`, width: `${Math.max(((day.max - day.min) / span) * 100, 4)}%` }} />
        </span>
        <span className="w-10 shrink-0 text-foreground">{Math.round(day.max)}</span>
      </li>)}
    </ul>}

    <a className="mt-auto text-xs text-muted-foreground underline" href="https://open-meteo.com/" target="_blank" rel="noreferrer">{t('openMeteoAttribution')}</a>
  </section>;
}
