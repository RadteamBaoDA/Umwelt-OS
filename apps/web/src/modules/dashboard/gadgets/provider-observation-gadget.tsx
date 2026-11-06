'use client';

import { useQueries, useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { formatChartValue, type ChartDataPoint } from '@/core/charts/chart-format';
import { formatDateTime, normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow, type WorldObservation } from '@/modules/observations';
import { getConnectorConfiguration } from '@/modules/sources';
import { isStale, periodKind } from '@/modules/sources';
import type { GadgetInstance } from '../api';
import { METRIC_FALLBACK_LOOKBACK_DAYS, useEffectiveLookback } from './use-effective-lookback';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetKpi } from './gadget-kpi';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { formatMetricLabel } from './metric-label';
import { metricBetterDirection } from './metric-direction';
import { SeriesRow } from './series-row';
import { getDateTimeFormat } from '@/modules/shared';

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
    const key = seriesKey(item);
    const current = latest.get(key);
    if (!current || item.observed_at > current.observed_at) latest.set(key, item);
  }
  return [...latest.values()].slice(0, 20);
}

/** Series identity shared by `latestPerSeries` and `historyPerSeries`; includes the source so two sources never merge. */
export function seriesKey(item: WorldObservation): string {
  return `${item.source_id}|${item.provider}|${item.metric}|${item.symbol ?? ''}|${item.region ?? ''}`;
}

/**
 * Builds a time-ordered point list per series from a history window. Rows without a value are skipped and
 * revisions of the same observed_at collapse to the last one read, so sparklines and deltas see one point per instant.
 *
 * @param items Observations (any order, any mix of series).
 * @returns Map from series key to ascending points.
 */
export function historyPerSeries(items: WorldObservation[]): Map<string, ChartDataPoint[]> {
  const byKey = new Map<string, Map<string, number>>();
  for (const item of items) {
    if (item.value === null) continue;
    const key = seriesKey(item);
    const points = byKey.get(key) ?? new Map<string, number>();
    points.set(item.observed_at, item.value);
    byKey.set(key, points);
  }
  return new Map(
    [...byKey].map(([key, points]) => [
      key,
      [...points].sort(([a], [b]) => a.localeCompare(b)).map(([timestamp, value]) => ({ timestamp, value })),
    ]),
  );
}

/**
 * Percent change of the newest point versus the one before it.
 *
 * @param points Ascending series points.
 * @returns Signed percent, or null when there is no previous point or it is zero.
 */
export function deltaVsPrevious(points: ChartDataPoint[] | undefined): number | null {
  if (!points || points.length < 2) return null;
  const prev = points[points.length - 2].value;
  return prev === 0 ? null : ((points[points.length - 1].value - prev) / Math.abs(prev)) * 100;
}

/**
 * Formats a provider calendar date (YYYY-MM-DD) as a localized date only. The date carries no time zone,
 * so it is read and printed in UTC to keep the same calendar day; anything unparseable is shown as given.
 */
function formatReferenceDate(value: string, locale: ReturnType<typeof useDisplayPreferences>['locale']): string {
  const date = new Date(`${value}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(date.getTime())) return value;
  return getDateTimeFormat(normalizeFormattingLocale(locale), { dateStyle: 'medium', timeZone: 'UTC' }).format(date);
}

/**
 * Formats one numeric value of an observation by locale, keeping the provider currency inline. Plain units are
 * rendered separately by the caller, so they are not part of the text. Small magnitudes keep enough fraction digits
 * to stay readable (0.0067 stays "0.0067") and large ones are compacted (2.5e13 becomes "25T").
 *
 * @param item Observation supplying currency and unit (percent units use 2 digits).
 * @param value The value to format; the history sparkline and delta reuse the same formatter.
 * @param locale Display locale.
 * @returns Localized text, or an em dash for non-finite input.
 */
export function formatObservationValue(
  item: WorldObservation,
  value: number,
  locale: ReturnType<typeof useDisplayPreferences>['locale'],
): string {
  const smallValuePrecision = value !== 0 && Math.abs(value) < 0.0001 ? Math.min(10, Math.ceil(-Math.log10(Math.abs(value))) + 2) : 4;
  return formatChartValue(value, {
    currency: item.currency ?? undefined,
    locale,
    precision: item.currency || item.unit === '%' || item.unit === 'percent' ? 2 : smallValuePrecision,
    compact: true,
  });
}

/** Bounded index units such as "index (0-100)" render as a "/ 100" suffix; anything else has none. */
function boundedIndexSuffix(item: WorldObservation): string | null {
  const bounded = !item.currency && item.value !== null ? /^index\s*\(\s*0\s*-\s*(\d+)\s*\)$/i.exec(item.unit ?? '') : null;
  return bounded ? `/ ${bounded[1]}` : null;
}

/**
 * Loads one observation window per bound source and merges them (latest value + history per series).
 * Reports provider attribution and the newest collection time as the frame footer.
 *
 * @returns Source ids, aggregate query status, merged latest rows, per-series history, per-source info and the
 * connector configuration query (its polling interval drives the stale flag).
 */
function useProviderObservations(instance: GadgetInstance) {
  const t = useTranslations('dashboard');
  const definition = instance.definition;
  const sourceIds = definition.source_ids ?? [];
  const sourceId = sourceIds[0] ?? '';
  const metrics = definition.scope?.metrics ?? [];
  const symbols = definition.scope?.symbols ?? [];
  // Unset lookback follows the bound provider's catalog default (annual World Bank ~10 years), never below 30 days.
  const {
    lookbackDays: rawLookback,
    ready: lookbackReady,
    catalog,
    sources,
  } = useEffectiveLookback(sourceIds, definition.scope?.lookback_days, METRIC_FALLBACK_LOOKBACK_DAYS, true);
  const lookbackDays = Math.min(rawLookback, 3660);
  const queries = useQueries({
    queries: sourceIds.map((id) => {
      const entry = sources[id]?.provider ? catalog.data?.[sources[id].provider as string] : undefined;
      const sourceSymbols = entry && !entry.symbol_keyed ? [] : symbols;
      return {
        queryKey: ['world-observations', 'provider', instance.id, id, metrics, sourceSymbols, lookbackDays],
        queryFn: ({ signal }: { signal: AbortSignal }) => {
          const to = new Date(Date.now() + 86_400_000);
          // `to` runs one day ahead (provider clock skew); keep the whole window within the API's 3660-day maximum.
          const from = new Date(to.getTime() - Math.min(lookbackDays + 1, 3660) * 86_400_000);
          return listWorldObservationWindow(
            { sourceIds: [id], metrics, symbols: sourceSymbols, from, to, maxPages: 2, history: true },
            signal,
          );
        },
        enabled: lookbackReady,
        staleTime: 60_000,
      };
    }),
  });
  const query = {
    isPending: queries.length === 0 || queries.some((item) => item.isPending),
    isError: queries.some((item) => item.isError),
  };
  const sourceQuery = useQuery({
    queryKey: ['connector-configuration', sourceId],
    queryFn: ({ signal }) => getConnectorConfiguration(sourceId, signal),
    enabled: Boolean(sourceId),
    staleTime: 300_000,
  });
  const sourceItems = queries.flatMap((item, index) => (item.data?.items ?? []).filter((row) => row.source_id === sourceIds[index]));
  const items = latestPerSeries(sourceItems);
  const history = historyPerSeries(sourceItems);
  const providerNames = [...new Set(items.map((item) => PROVIDER_PRESENTATION[item.provider]?.name).filter(Boolean))].join(', ');
  // Provider attribution + freshness live in the frame footer, not in the body.
  useReportGadgetFreshness(
    items.reduce<string | null>((max, item) => (!max || item.collected_at > max ? item.collected_at : max), null),
    providerNames ? t('providerObsAttribution', { provider: providerNames }) : null,
  );
  return { sourceIds, sourceId, query, items, history, sources, sourceQuery };
}

/** Reference date / annual period lines and the provider attribution links (hidden in small containers). */
function ObservationNotes({ items }: { items: WorldObservation[] }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  return (
    <div className="space-y-0.5 text-xs text-muted-foreground @max-[24rem]/gadget:hidden">
      {items.map((item) => {
        const kind = periodKind(item.provider);
        return (
          <div key={item.id}>
            {kind === 'referenceDate' && item.reference_date && (
              <p>{t('providerObsReferenceDate', { date: formatReferenceDate(item.reference_date, display.locale) })}</p>
            )}
            {kind === 'annualPeriod' && item.period && <p>{t('providerObsPeriod', { period: item.period })}</p>}
          </div>
        );
      })}
      {[...new Set(items.map((item) => item.provider))].map(
        (provider) =>
          PROVIDER_PRESENTATION[provider] && (
            <p key={provider}>
              <a className="underline" href={PROVIDER_PRESENTATION[provider].href} target="_blank" rel="noreferrer">
                {PROVIDER_PRESENTATION[provider].name}
              </a>
            </p>
          ),
      )}
    </div>
  );
}

/** One reading as a KPI (single series) or a list of SeriesRows (several), with observed/published/collected tooltips. */
function ObservationRows({
  instance,
  items,
  history,
  sources,
  sourceIds,
}: {
  instance: GadgetInstance;
  items: WorldObservation[];
  history: ReturnType<typeof historyPerSeries>;
  sources: ReturnType<typeof useEffectiveLookback>['sources'];
  sourceIds: string[];
}) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  /** Formats an observation timestamp with the display locale and timezone. */
  const fmt = (value: string) => formatDateTime(value, display.locale, display.timezone);
  /** Tooltip text listing the observed / published / collected times of one reading. */
  const timesTitle = (item: WorldObservation) =>
    [
      `${t('providerObsObserved')}: ${fmt(item.observed_at)}`,
      item.published_at ? `${t('providerObsPublished')}: ${fmt(item.published_at)}` : null,
      `${t('observationCollectedAt')}: ${fmt(item.collected_at)}`,
    ]
      .filter(Boolean)
      .join('\n');
  /** Row label: symbol, region or humanized metric, prefixed with the source name when several sources are bound. */
  const baseLabel = (item: WorldObservation) => item.symbol ?? item.region ?? formatMetricLabel(item.metric, t);
  const labelOf = (item: WorldObservation) =>
    sourceIds.length < 2
      ? baseLabel(item)
      : `${sources[item.source_id]?.name || PROVIDER_PRESENTATION[item.provider]?.name || item.provider} · ${baseLabel(item)}`;
  /** Unit text shown beside the figure: bounded-index suffix, else the plain unit (bare "index" adds nothing). */
  const unitOf = (item: WorldObservation) => boundedIndexSuffix(item) ?? (item.unit === 'index' ? undefined : item.unit);
  const single = items.length === 1 ? items[0] : null;
  if (single) {
    return (
      <div
        title={timesTitle(single)}
        data-testid="observation-row"
        role="group"
        aria-label={single.value === null ? t('weatherMissingValue') : `${single.value} ${single.currency ?? single.unit}`}
      >
        <GadgetKpi
          label={labelOf(single)}
          value={single.value}
          currency={single.currency ?? undefined}
          unit={unitOf(single)}
          format={(v) => formatObservationValue(single, v, display.locale)}
          deltaPercent={deltaVsPrevious(history.get(seriesKey(single)))}
          better={metricBetterDirection(single.metric)}
          series={history.get(seriesKey(single))}
        />
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border" aria-label={instance.title || t('providerObsTitle')}>
      {items.map((item) => (
        <li key={item.id} title={timesTitle(item)} data-testid="observation-row">
          <SeriesRow
            label={labelOf(item)}
            value={item.value}
            currency={item.currency ?? undefined}
            unit={unitOf(item)}
            format={(v) => formatObservationValue(item, v, display.locale)}
            series={history.get(seriesKey(item))}
            deltaPercent={deltaVsPrevious(history.get(seriesKey(item)))}
            better={metricBetterDirection(item.metric)}
          />
        </li>
      ))}
    </ul>
  );
}

/**
 * Generic provider observation gadget (Fear & Greed, FX, GDP, BTC price, USGS quakes). One series renders a KPI
 * (figure, delta vs previous observation, sparkline); several render SeriesRows.
 * Several bound sources (up to 5) each get their own query, because one shared request would drop symbol-less rows
 * (Fear & Greed, FX) under a `symbols` filter and share a row budget; their rows are merged (labels prefixed with
 * the source name) and the merged list is capped at 20. `symbols` apply only to symbol-keyed providers. Observed/published/collected times
 * sit in the value tooltip, attribution + freshness in the frame footer, FX reference date / annual period inline, a stale flag after two polling intervals, and keeps the last good
 * value visible with an error banner when a refresh fails.
 */
export function ProviderObservationGadget({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  const { sourceIds, sourceId, query, items, history, sources, sourceQuery } = useProviderObservations(instance);

  if (!sourceId) return <GadgetNotice kind="setup" message={t('providerObsSourceRequired')} />;
  if (query.isPending) return <GadgetSkeleton variant="metric" label={t('observationLoading')} />;
  // No last good value to fall back on: plain error.
  if (!items.length && query.isError) return <GadgetNotice kind="error" message={t('observationLoadFailed')} />;
  if (!items.length) return <GadgetNotice kind="empty" message={t('observationEmpty')} />;

  const interval = sourceQuery.data?.configuration.schedule_interval_minutes;
  const lastCollected = items.reduce((max, item) => (item.collected_at > max ? item.collected_at : max), items[0].collected_at);
  const stale = isStale(lastCollected, interval);

  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      {query.isError && (
        <p role="alert" className="text-xs text-destructive">
          {t('providerObsRefreshFailed')}
        </p>
      )}
      {stale && (
        <p role="status" className="text-xs text-muted-foreground">
          {t('providerObsStale')}
        </p>
      )}
      <ObservationRows instance={instance} items={items} history={history} sources={sources} sourceIds={sourceIds} />
      {/* Reference date / annual period and the attribution link: hidden in small containers (figure + delta + spark only). */}
      <ObservationNotes items={items} />
    </section>
  );
}
