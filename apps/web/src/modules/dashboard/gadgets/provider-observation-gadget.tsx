'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow, type WorldObservation } from '@/modules/observations/api';
import { getConnectorConfiguration } from '@/modules/sources/api';
import { isStale, periodKind } from '@/modules/sources/freshness';
import type { GadgetInstance } from '../api';

/** Per-provider presentation: display name and attribution link shown next to the value. */
export const PROVIDER_PRESENTATION: Record<string, { name: string; href: string }> = {
  alternative_me: { name: 'Alternative.me', href: 'https://alternative.me/crypto/fear-and-greed-index/' },
  frankfurter: { name: 'Frankfurter', href: 'https://frankfurter.dev/' },
  ecb: { name: 'ECB', href: 'https://www.ecb.europa.eu/' },
  world_bank: { name: 'World Bank', href: 'https://data.worldbank.org/' },
  binance: { name: 'Binance', href: 'https://www.binance.com/' },
  coinpaprika: { name: 'CoinPaprika', href: 'https://coinpaprika.com/' },
  coingecko: { name: 'CoinGecko', href: 'https://www.coingecko.com/' },
  usgs: { name: 'USGS', href: 'https://earthquake.usgs.gov/' },
};

/** Keep only the newest observation per provider+metric+symbol+region so a series renders as its latest readings. */
export function latestPerSeries(items: WorldObservation[]): WorldObservation[] {
  const latest = new Map<string, WorldObservation>();
  for (const item of items) {
    const key = `${item.provider}|${item.metric}|${item.symbol ?? ''}|${item.region ?? ''}`;
    const current = latest.get(key);
    if (!current || item.observed_at > current.observed_at) latest.set(key, item);
  }
  return [...latest.values()].slice(0, 20);
}

/**
 * Generic provider observation gadget (Fear & Greed, FX, GDP, BTC price, USGS quakes). Shows the
 * latest value with provider attribution, FX reference date / annual period, separate observed,
 * published and collected times, a stale flag after two polling intervals, and keeps the last good
 * value visible with an error banner when a refresh fails.
 */
export function ProviderObservationGadget({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const definition = instance.definition;
  const sourceId = definition.source_ids?.[0] ?? '';
  const metrics = definition.scope?.metrics ?? [];
  const lookbackDays = Math.min(definition.scope?.lookback_days ?? 30, 366);
  const query = useQuery({
    queryKey: ['world-observations', 'provider', instance.id, sourceId, metrics, lookbackDays],
    queryFn: ({ signal }) => {
      const to = new Date(Date.now() + 86_400_000);
      const from = new Date(Date.now() - lookbackDays * 86_400_000);
      return listWorldObservationWindow({ sourceIds: [sourceId], metrics, from, to }, signal);
    },
    enabled: Boolean(sourceId), staleTime: 60_000,
  });
  const sourceQuery = useQuery({ queryKey: ['connector-configuration', sourceId], queryFn: ({ signal }) => getConnectorConfiguration(sourceId, signal), enabled: Boolean(sourceId), staleTime: 300_000 });
  const items = latestPerSeries((query.data?.items ?? []).filter((item) => item.source_id === sourceId));
  const fmt = (value: string) => formatDateTime(value, display.locale, display.timezone);

  if (!sourceId) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('providerObsSourceRequired')}</p>;
  if (query.isPending) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('observationLoading')}</p>;
  // No last good value to fall back on: plain error.
  if (!items.length && query.isError) return <p role="alert" className="p-3 text-sm text-destructive">{t('observationLoadFailed')}</p>;
  if (!items.length) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('observationEmpty')}</p>;

  const interval = sourceQuery.data?.configuration.schedule_interval_minutes;
  const lastCollected = items.reduce((max, item) => (item.collected_at > max ? item.collected_at : max), items[0].collected_at);
  const stale = isStale(lastCollected, interval);

  return <section className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
    {query.isError && <p role="alert" className="text-xs text-destructive">{t('providerObsRefreshFailed')}</p>}
    {stale && <p role="status" className="text-xs text-muted-foreground">{t('providerObsStale')}</p>}
    <ul className="divide-y divide-border" aria-label={instance.title || t('providerObsTitle')}>
      {items.map((item) => {
        const presentation = PROVIDER_PRESENTATION[item.provider];
        const kind = periodKind(item.provider);
        return <li key={item.id} className="space-y-1 py-2 text-sm">
          <div className="flex items-baseline justify-between gap-3">
            <span className="min-w-0 font-medium">{item.symbol ?? item.region ?? item.metric}</span>
            <span className="shrink-0 font-mono text-base">
              {item.value === null ? t('weatherMissingValue') : `${item.value} ${item.unit}`}
              {presentation && <a className="ml-2 font-sans text-xs text-muted-foreground underline" href={presentation.href} target="_blank" rel="noreferrer">{t('providerObsAttribution', { provider: presentation.name })}</a>}
            </span>
          </div>
          {kind === 'referenceDate' && item.reference_date && <p className="text-xs text-muted-foreground">{t('providerObsReferenceDate', { date: item.reference_date })}</p>}
          {kind === 'annualPeriod' && item.period && <p className="text-xs text-muted-foreground">{t('providerObsPeriod', { period: item.period })}</p>}
          <dl className="grid grid-cols-1 gap-x-3 text-xs text-muted-foreground sm:grid-cols-3">
            <div><dt className="inline">{t('providerObsObserved')}: </dt><dd className="inline"><time dateTime={item.observed_at}>{fmt(item.observed_at)}</time></dd></div>
            <div><dt className="inline">{t('providerObsPublished')}: </dt><dd className="inline">{item.published_at ? <time dateTime={item.published_at}>{fmt(item.published_at)}</time> : t('providerObsNotProvided')}</dd></div>
            <div><dt className="inline">{t('observationCollectedAt')}: </dt><dd className="inline"><time dateTime={item.collected_at}>{fmt(item.collected_at)}</time></dd></div>
          </dl>
        </li>;
      })}
    </ul>
  </section>;
}
