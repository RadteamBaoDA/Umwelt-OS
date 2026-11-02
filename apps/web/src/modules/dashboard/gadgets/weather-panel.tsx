'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow } from '@/modules/observations/api';
import type { GadgetInstance } from '../api';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { formatMetricLabel } from './metric-label';

/** Show scoped forecast points in a bounded scroll root with original units and attribution. */
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
      const to = new Date(Date.now() + lookbackDays * 86_400_000);
      const from = new Date(Date.now() - 60_000);
      return listWorldObservationWindow({ sourceIds: [sourceId], metrics, from, to }, signal);
    },
    enabled: Boolean(sourceId), staleTime: 60_000,
  });
  // The API returns newest first; a forecast reads soonest first so the first row is the next hour.
  const items = (query.data?.items ?? [])
    .filter((item) => item.source_id === sourceId && item.provider === 'open_meteo')
    .sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at));

  // One decimal at most so 31 and 31.6 do not mix precision within a list.
  const valueFormat = new Intl.NumberFormat(display.locale, { maximumFractionDigits: 1, minimumFractionDigits: 1 });

  const formatLocale = normalizeFormattingLocale(display.locale);
  /** Calendar-day key in the reading's own timezone, used to group rows under one day heading. */
  const dayKey = (value: string, zone: string) => new Intl.DateTimeFormat('en-CA', { dateStyle: 'short', timeZone: zone }).format(new Date(value));

  if (!sourceId) return <GadgetNotice kind="setup" message={t('weatherSourceRequired')} />;
  if (query.isPending) return <GadgetSkeleton variant="list" label={t('observationLoading')} />;
  if (query.isError) return <GadgetNotice kind="error" message={t('observationLoadFailed')} />;
  if (!items.length) return <GadgetNotice kind="empty" message={t('observationEmpty')} />;

  return <section className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
    <header className="flex items-center justify-end gap-2 border-b border-border pb-2">
      <a className="text-xs text-muted-foreground underline" href="https://open-meteo.com/" target="_blank" rel="noreferrer">{t('openMeteoAttribution')}</a>
    </header>
    {query.data?.truncated && <p role="status" className="text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    <ul className="min-h-0 flex-1 divide-y divide-border overflow-y-auto" aria-label={t('weatherForecastReadings')}>
      {items.map((item, index) => {
        const zone = item.timezone ?? display.timezone;
        const previous = items[index - 1];
        // Day heading only when the calendar day changes, so rows carry just the time.
        const newDay = !previous || dayKey(previous.observed_at, previous.timezone ?? display.timezone) !== dayKey(item.observed_at, zone);
        return <li key={item.id} className="flex items-end justify-between gap-3 py-2 text-sm">
        <span className="min-w-0">{newDay && <span className="block text-xs font-semibold text-muted-foreground">{new Intl.DateTimeFormat(formatLocale, { dateStyle: 'medium', timeZone: zone }).format(new Date(item.observed_at))}</span>}{item.metric !== items[index - 1]?.metric && <span className="block font-medium">{formatMetricLabel(item.metric, t)}</span>}<time className="block text-xs text-muted-foreground" dateTime={item.observed_at}>{new Intl.DateTimeFormat(formatLocale, { timeStyle: 'short', timeZone: zone }).format(new Date(item.observed_at))}</time></span>
        <span className="shrink-0 tabular-nums">{item.value === null || item.quality === 'missing' ? t('weatherMissingValue') : `${valueFormat.format(item.value)} ${item.unit}`}</span>
      </li>;})}
    </ul>
  </section>;
}
