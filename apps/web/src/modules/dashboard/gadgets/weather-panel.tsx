'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow } from '@/modules/observations/api';
import type { GadgetInstance } from '../api';

/** Show scoped Open-Meteo forecast points with original units, timezone and attribution. */
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
  const items = (query.data?.items ?? []).filter((item) => item.source_id === sourceId && item.provider === 'open_meteo');

  if (!sourceId) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('weatherSourceRequired')}</p>;
  if (query.isPending) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('observationLoading')}</p>;
  if (query.isError) return <p role="alert" className="p-3 text-sm text-destructive">{t('observationLoadFailed')}</p>;
  if (!items.length) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('observationEmpty')}</p>;

  return <section className="flex h-full min-h-0 flex-col gap-2 overflow-hidden bg-card p-3 text-card-foreground">
    <header className="flex items-center justify-between gap-2 border-b border-border pb-2">
      <h2 className="text-sm font-semibold">{instance.title || t('weatherTitle')}</h2>
      <a className="text-xs text-muted-foreground underline" href="https://open-meteo.com/" target="_blank" rel="noreferrer">{t('openMeteoAttribution')}</a>
    </header>
    <p className="text-xs text-muted-foreground">{t('weatherSourceTime', { source: sourceId })}</p>
    {query.data?.truncated && <p role="status" className="text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    <ul className="min-h-0 flex-1 divide-y divide-border overflow-y-auto" aria-label={t('weatherForecastReadings')}>
      {items.map((item) => <li key={item.id} className="flex items-center justify-between gap-3 py-2 text-sm">
        <span className="min-w-0"><span className="block font-medium">{item.metric}</span><time className="block text-xs text-muted-foreground" dateTime={item.observed_at}>{formatDateTime(item.observed_at, display.locale, item.timezone ?? display.timezone)} · {item.timezone ?? 'UTC'}</time></span>
        <span className="shrink-0 font-mono">{item.value === null || item.quality === 'missing' ? t('weatherMissingValue') : `${item.value} ${item.unit}`}</span>
      </li>)}
    </ul>
  </section>;
}
