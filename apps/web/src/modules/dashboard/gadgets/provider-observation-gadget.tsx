'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { formatChartValue } from '@/core/charts/recharts-wrapper';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow, type WorldObservation } from '@/modules/observations/api';
import { getConnectorConfiguration } from '@/modules/sources/api';
import { isStale, periodKind } from '@/modules/sources/freshness';
import type { GadgetInstance } from '../api';
import { METRIC_FALLBACK_LOOKBACK_DAYS, useEffectiveLookback } from './use-effective-lookback';
import { useReportGadgetFreshness } from '../gadget-freshness';

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
  open_er_api: { name: 'ExchangeRate-API', href: 'https://www.exchangerate-api.com/docs/free' },
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

/** Formats an observation by locale while retaining its provider supplied currency or unit. */
function formatObservationValue(item: WorldObservation, locale: ReturnType<typeof useDisplayPreferences>['locale']): string {
  if (item.value === null) return '—';
  const smallValuePrecision = item.value !== 0 && Math.abs(item.value) < 0.0001
    ? Math.min(10, Math.ceil(-Math.log10(Math.abs(item.value))) + 2)
    : 4;
  return formatChartValue(item.value, {
    currency: item.currency ?? undefined,
    unit: item.currency ? undefined : item.unit,
    locale,
    precision: item.currency || item.unit === '%' || item.unit === 'percent' ? 2 : smallValuePrecision,
    compact: true,
  });
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
  const symbols = definition.scope?.symbols ?? [];
  // Unset lookback follows the bound provider's catalog default (annual World Bank ~10 years), never below 30 days.
  const { lookbackDays: rawLookback, ready: lookbackReady } = useEffectiveLookback(sourceId, definition.scope?.lookback_days, METRIC_FALLBACK_LOOKBACK_DAYS, true);
  const lookbackDays = Math.min(rawLookback, 3660);
  const query = useQuery({
    queryKey: ['world-observations', 'provider', instance.id, sourceId, metrics, symbols, lookbackDays],
    queryFn: ({ signal }) => {
      const to = new Date(Date.now() + 86_400_000);
      // `to` runs one day ahead (provider clock skew); keep the whole window within the API's 3660-day maximum.
      const from = new Date(to.getTime() - Math.min(lookbackDays + 1, 3660) * 86_400_000);
      return listWorldObservationWindow({ sourceIds: [sourceId], metrics, symbols, from, to, maxPages: 1 }, signal);
    },
    enabled: Boolean(sourceId) && lookbackReady, staleTime: 60_000,
  });
  const sourceQuery = useQuery({ queryKey: ['connector-configuration', sourceId], queryFn: ({ signal }) => getConnectorConfiguration(sourceId, signal), enabled: Boolean(sourceId), staleTime: 300_000 });
  const items = latestPerSeries((query.data?.items ?? []).filter((item) => item.source_id === sourceId));
  useReportGadgetFreshness(items.reduce<string | null>((max, item) => (!max || item.collected_at > max ? item.collected_at : max), null));
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
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1" data-testid="observation-row">
            <span className="min-w-0 break-words font-medium">{item.symbol ?? item.region ?? item.metric}</span>
            <span className="flex flex-wrap items-baseline gap-x-2 font-mono text-base">
              <span className="whitespace-nowrap" title={item.value === null ? undefined : `${item.value} ${item.currency ?? item.unit}`} aria-label={item.value === null ? t('weatherMissingValue') : `${item.value} ${item.currency ?? item.unit}`}>
                {formatObservationValue(item, display.locale)}
              </span>
              {presentation && <a className="font-sans text-xs text-muted-foreground underline" href={presentation.href} target="_blank" rel="noreferrer">{t('providerObsAttribution', { provider: presentation.name })}</a>}
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
