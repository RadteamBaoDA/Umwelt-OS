'use client';

import { Plus, Search } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { formatDateTime } from '@/core/i18n';
import { listWorldObservationWindow, type WorldObservation } from '@/modules/observations/api';
import { useDisplayPreferences } from '@/core/query-provider';
import type { GadgetInstance, HighlightRule } from '../api';
import { evaluateGadgetHighlights } from '../api';
import { useReportGadgetFreshness } from '../gadget-freshness';
import { GadgetNotice, GadgetSkeleton } from './gadget-notice';
import { metricBetterDirection } from './metric-direction';
import { deltaVsPrevious, formatObservationValue, historyPerSeries, seriesKey } from './provider-observation-gadget';
import { SeriesRow } from './series-row';
import { formatRelativeTime } from './relative-time';

/** Sort rank per status/severity (lower sorts first). */
const STATUS_RANK: Record<WatchlistItem['status'], number> = { alert: 0, matched: 1, monitoring: 2 };
const SEVERITY_RANK: Record<WatchlistItem['severity'], number> = { critical: 0, warning: 1, info: 2 };

/** Ticker to CoinGecko series key (observations of by-id assets carry the uppercased coin id, e.g. BITCOIN). */
const SYMBOL_ALIASES: Record<string, string> = { BTC: 'BITCOIN', ETH: 'ETHEREUM', SOL: 'SOLANA', XRP: 'RIPPLE', DOGE: 'DOGECOIN', ADA: 'CARDANO' };
/** History window for watchlist sparklines. */
const SPARK_LOOKBACK_DAYS = 30;

/**
 * Upper-case match keys for a watchlist symbol target: itself plus its known alias in either direction.
 *
 * @param target Raw symbol target text.
 * @returns Distinct upper-case candidates the observation series symbol may carry.
 */
function symbolCandidates(target: string): string[] {
  const up = target.trim().toUpperCase();
  const reverse = Object.entries(SYMBOL_ALIASES).find(([, id]) => id === up)?.[0];
  return [...new Set([up, SYMBOL_ALIASES[up], reverse].filter((v): v is string => Boolean(v)))];
}

/** Single monitored rule or target symbol entry on the watchlist table. */
export interface WatchlistItem {
  id: string;
  target: string;
  targetType: 'symbol' | 'keyword' | 'entity';
  condition: string;
  status: 'matched' | 'monitoring' | 'alert';
  severity: 'info' | 'warning' | 'critical';
  lastMatchedAt: string | null;
  matchReason?: string;
  matchCount: number;
}

/** Props for the WatchlistGadget component. */
export interface WatchlistGadgetProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional initial watchlist entries. */
  initialItems?: WatchlistItem[];
  /** Callback fired when user wants to add or adjust a rule. */
  onAddRule?: () => void;
}

/**
 * Standard Watchlist and Rules table gadget template.
 * Renders an alerting-count summary and rule rows sorted alerting first, with a 2px severity edge
 * (screen-reader text carries the severity), relative last-match time, tabular match count and
 * client-side search filtering.
 * The bounded root scrolls its controls and watchlist instead of clipping them in compact cards.
 *
 * @param props Gadget instance configuration and watchlist items.
 * @returns Accessible watchlist table gadget component.
 */
export function WatchlistGadget({
  instance,
  initialItems = [],
  onAddRule,
}: WatchlistGadgetProps) {
  const t = useTranslations('dashboard');
  const daily = useTranslations('daily');
  const display = useDisplayPreferences();

  const [filterQuery, setFilterQuery] = useState<string>('');

  const definition = instance.definition;
  const highlightRules: HighlightRule[] = definition.highlight_rules ?? [];
  const matchesQuery = useQuery({
    queryKey: ['dashboard-highlights', definition.id, definition.revision],
    enabled: (definition.renderer === 'highlights' || definition.renderer === 'watch_rules')
      && definition.source_ids.length > 0 && highlightRules.length > 0,
    queryFn: () => evaluateGadgetHighlights(definition.id),
  });
  const items: WatchlistItem[] = initialItems.length ? initialItems : (matchesQuery.data ?? []).map((match) => ({
    id: `${match.document_version_id}:${match.rule_id}`,
    target: match.title,
    targetType: 'keyword',
    condition: match.matched_keywords.join(', '),
    status: match.severity === 'critical' ? 'alert' : 'matched',
    severity: match.severity,
    lastMatchedAt: match.observed_at,
    matchReason: match.reason,
    matchCount: 1,
  }));

  // One batched observations query for every symbol target (never per row); sorted so the key is stable.
  const symbolTargets = [...new Set(items.filter((i) => i.targetType === 'symbol').flatMap((i) => symbolCandidates(i.target)))].sort();
  const sourceIds = definition.source_ids ?? [];
  const spark = useQuery({
    queryKey: ['world-observations', 'watchlist', instance.id, sourceIds, symbolTargets],
    enabled: symbolTargets.length > 0 && sourceIds.length > 0,
    staleTime: 60_000,
    queryFn: ({ signal }) => {
      const to = new Date(Date.now() + 86_400_000);
      const from = new Date(to.getTime() - (SPARK_LOOKBACK_DAYS + 1) * 86_400_000);
      return listWorldObservationWindow({ sourceIds, symbols: symbolTargets, from, to, maxPages: 2, history: true }, signal);
    },
  });
  // Latest observation and history per upper-cased series symbol, so rows match case-insensitively and by alias.
  const sparkItems = spark.data?.items ?? [];
  const history = historyPerSeries(sparkItems);
  const latestBySymbol = new Map<string, WorldObservation>();
  for (const o of sparkItems) {
    const key = (o.symbol ?? '').toUpperCase();
    const cur = latestBySymbol.get(key);
    if (key && o.value !== null && (!cur || o.observed_at > cur.observed_at)) latestBySymbol.set(key, o);
  }
  /** Latest observation matching a symbol item, or undefined (row then keeps its plain display). */
  const matchFor = (item: WatchlistItem) => item.targetType === 'symbol'
    ? symbolCandidates(item.target).map((c) => latestBySymbol.get(c)).find(Boolean)
    : undefined;

  // Filter items by search query
  const filteredItems = items.filter((item) => {
    if (!filterQuery) return true;
    const q = filterQuery.toLowerCase();
    return (
      item.target.toLowerCase().includes(q) ||
      item.condition.toLowerCase().includes(q) ||
      item.matchReason?.toLowerCase().includes(q)
    );
  });
  // Alerting first, then by severity, then most recently matched, so the rows that need attention lead.
  const sortedItems = [...filteredItems].sort((a, b) =>
    STATUS_RANK[a.status] - STATUS_RANK[b.status]
    || SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]
    || (b.lastMatchedAt ?? '').localeCompare(a.lastMatchedAt ?? ''));
  const alertingCount = items.filter((item) => item.status === 'alert').length;
  // The latest match time is the newest real data timestamp this gadget knows about.
  useReportGadgetFreshness(items.reduce<string | null>((max, i) => (i.lastMatchedAt && (!max || i.lastMatchedAt > max) ? i.lastMatchedAt : max), null));

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto overflow-x-hidden bg-card p-3 text-card-foreground">
      {/* Current summary: alerting count is the one figure; add-rule stays a quiet action. */}
      <div className="flex items-center justify-between gap-2">
        <p
          className="text-figure-sm"
          title={highlightRules.length > 0 ? daily('highlightRulesApplied', { count: highlightRules.length }) : undefined}
        >
          {t('watchlistSummary', { alerting: alertingCount, total: items.length })}
        </p>
        {onAddRule && (
          <Button type="button" variant="ghost" size="sm" onClick={onAddRule} className="h-7 shrink-0 gap-1 px-2 text-xs">
            <Plus className="w-3 h-3" aria-hidden="true" />
            <span>{daily('addRule')}</span>
          </Button>
        )}
      </div>

      {/* Search Filter Input */}
      {items.length > 0 && (
        <div className="relative shrink-0">
          <Search className="w-3.5 h-3.5 absolute left-2 top-2 text-muted-foreground pointer-events-none" aria-hidden="true" />
          <Input
            type="search"
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            placeholder={t('feedSearch')}
            aria-label={t('feedSearch')}
            className="h-7 w-full text-xs pl-7 bg-muted/20"
          />
        </div>
      )}

      {/* Empty State */}
      {matchesQuery.isPending && highlightRules.length > 0 && definition.source_ids.length > 0 && <GadgetSkeleton variant="table" className="p-0" label={t('gadgetDataLoading')} />}
      {matchesQuery.isError && <GadgetNotice kind="error" className="p-0" message={t('gadgetDataLoadFailed')} />}
      {!(matchesQuery.isPending && highlightRules.length > 0 && definition.source_ids.length > 0)
        && !matchesQuery.isError && filteredItems.length === 0 && (
        <GadgetNotice kind="empty" className="p-0" message={definition.renderer === 'highlights' ? daily('highlightsEmpty') : `${daily('watchlistEmpty')}. ${daily('watchlistEmptyDetail')}`} action={onAddRule ? { label: daily('addRule'), onClick: onAddRule } : { href: '/settings/sources', label: t('gadgetConfigureAction') }} />
      )}

      {/* Rule rows: severity is a 2px left edge (plus screen-reader text), time and count sit right, tabular. */}
      {sortedItems.length > 0 && (
        <ul className="min-h-0 flex-1 divide-y divide-border/50">
          {sortedItems.map((item) => {
            const obs = matchFor(item);
            const edge = item.severity === 'critical' ? 'border-destructive' : item.severity === 'warning' ? 'border-foreground/50' : 'border-transparent';
            // Symbol with live observations: label | sparkline | value | % change, direction-aware delta colour.
            if (obs) {
              const points = history.get(seriesKey(obs));
              return (
                <li key={item.id} className={`border-l-2 pl-3 ${edge}`} data-testid="watchlist-symbol-row">
                  <SeriesRow label={item.target} value={obs.value} currency={obs.currency ?? undefined} series={points}
                    format={(v) => formatObservationValue(obs, v, display.locale)} deltaPercent={deltaVsPrevious(points)} better={metricBetterDirection(obs.metric)} />
                  <span className="sr-only">{t(`watchlistSeverity_${item.severity}`)} · {t(`watchlistStatus_${item.status}`)}</span>
                </li>
              );
            }
            return (
            <li
              key={item.id}
              className={`flex items-start justify-between gap-3 border-l-2 py-2 pl-3 ${
                item.severity === 'critical' ? 'border-destructive' : item.severity === 'warning' ? 'border-foreground/50' : 'border-transparent'
              }`}
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground">{item.target}</p>
                <p className="truncate text-xs text-muted-foreground">{item.condition}</p>
                {item.matchReason && <p className="line-clamp-1 text-xs text-muted-foreground">{item.matchReason}</p>}
                <span className="sr-only">{t(`watchlistSeverity_${item.severity}`)} · {t(`watchlistStatus_${item.status}`)}</span>
              </div>
              <div className="shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                <p className={item.status === 'alert' ? 'font-semibold text-destructive' : undefined}>
                  {item.lastMatchedAt ? (
                    <time
                      dateTime={item.lastMatchedAt}
                      title={t('watchlistMatchedTitle', { time: formatDateTime(item.lastMatchedAt, display.locale, display.timezone) })}
                    >{formatRelativeTime(item.lastMatchedAt, display.locale)}</time>
                  ) : t('watchlistNever')}
                </p>
                <p>{t('watchlistMatchCount', { count: item.matchCount })}</p>
              </div>
            </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
