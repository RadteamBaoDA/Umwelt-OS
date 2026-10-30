'use client';

import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  BarChart2,
  Calendar,
  Clock,
  LineChart as LineChartIcon,
  Maximize2,
  TrendingUp,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import {
  BaseAreaChart,
  BaseBarChart,
  BaseLineChart,
  formatChartValue,
  type ChartDataPoint,
  type ChartSeries,
} from '@/core/charts/recharts-wrapper';
import { useDisplayPreferences } from '@/core/query-provider';
import type { GadgetInstance } from '../api';

/** Time range options supported by the financial chart filter. */
export type ChartTimeRange = '24h' | '7d' | '30d' | '90d' | '1y' | 'all';

/** Chart visualization kind selectable by the user. */
export type ChartKind = 'area' | 'line' | 'bar';

/** Lookback window durations in milliseconds for each bounded ChartTimeRange. */
const TIME_RANGE_MS: Record<Exclude<ChartTimeRange, 'all'>, number> = {
  '24h': 86_400_000,
  '7d': 7 * 86_400_000,
  '30d': 30 * 86_400_000,
  '90d': 90 * 86_400_000,
  '1y': 365 * 86_400_000,
};

/**
 * Parses a chart data point timestamp (ISO string or epoch ms) into epoch milliseconds.
 *
 * @param timestamp - Raw timestamp string or number from ChartDataPoint.
 * @returns Epoch milliseconds, or NaN when unparseable.
 */
function parsePointTimestampMs(timestamp: string | number): number {
  if (typeof timestamp === 'number') {
    return Number.isFinite(timestamp) ? timestamp : Number.NaN;
  }
  return Date.parse(timestamp);
}

/** Props for the MetricsChart gadget component. */
export interface MetricsChartProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional pre-loaded time series data points. */
  initialData?: ChartDataPoint[];
  /** Currency code supplied by the data provider; none is assumed. */
  currency?: string;
  /** Unit indicator (e.g. '%', 'pts'). */
  unit?: string;
  /** Provider latency or freshness notice; shown only when the provider supplies it. */
  providerDelay?: string | null;
}

/**
 * Standard Financial and Activity Metrics Chart gadget template.
 * Renders high-precision time-series data using Recharts via shadcn conventions,
 * supporting Area, Line, and Bar modes, time range filtering (24h/7d/30d/90d/1y/all),
 * unit/currency formatting, change calculations, and provider latency indicators.
 * Its control bars and 140px chart minimum remain nonshrinking inside a bounded scroll root.
 *
 * @param props Gadget instance configuration and time series options.
 * @returns Accessible metrics chart gadget component.
 */
export function MetricsChart({
  instance,
  initialData,
  currency,
  unit,
  providerDelay,
}: MetricsChartProps) {
  const t = useTranslations('dashboard');
  const ts = useTranslations('uiSweep');
  const daily = useTranslations('daily');
  const display = useDisplayPreferences();

  const definition = instance.definition;
  const targetSymbol =
    definition.scope?.symbols?.[0] ||
    definition.filters?.keywords?.[0] ||
    ts('marketData');

  const [timeRange, setTimeRange] = useState<ChartTimeRange>('7d');
  const [chartKind, setChartKind] = useState<ChartKind>('area');

  // Load or generate chart data
  const points = useMemo(() => {
    // Never invent market values: without a connected provider the series is empty (R12).
    return initialData ?? [];
  }, [initialData]);

  // Filter points to the active timeRange relative to the maximum valid timestamp in points;
  // if timestamps are unparseable and filtering leaves 0 points, fall back to all points.
  const visiblePoints = useMemo(() => {
    if (timeRange === 'all' || points.length === 0) {
      return points;
    }
    const parsedTimestamps = points.map((point) => parsePointTimestampMs(point.timestamp));
    const validTimestamps = parsedTimestamps.filter((ts) => Number.isFinite(ts));
    if (validTimestamps.length === 0) {
      return points;
    }
    const maxTimestamp = Math.max(...validTimestamps);
    const cutoff = maxTimestamp - TIME_RANGE_MS[timeRange];
    const filtered = points.filter((_, index) => {
      const ts = parsedTimestamps[index];
      return Number.isFinite(ts) && ts >= cutoff;
    });
    return filtered.length > 0 ? filtered : points;
  }, [points, timeRange]);

  const chartData = visiblePoints;

  // Derive current value, period change value, and percent change over the filtered visiblePoints window
  const { currentValue, changeValue, changePercent, isPositive } = useMemo(() => {
    if (!visiblePoints || visiblePoints.length === 0) {
      return {
        currentValue: 0,
        changeValue: 0,
        changePercent: null,
        isPositive: true,
      };
    }
    const first = visiblePoints[0].value;
    const last = visiblePoints[visiblePoints.length - 1].value;
    const diff = visiblePoints.length >= 2 ? last - first : last - first;
    const pct = first !== 0 ? (diff / first) * 100 : null;
    return {
      currentValue: last,
      changeValue: diff,
      changePercent: pct,
      isPositive: diff >= 0,
    };
  }, [visiblePoints]);

  const series: ChartSeries[] = [
    {
      dataKey: 'value',
      name: targetSymbol,
      currency,
      unit,
    },
  ];

  if (points.length === 0) {
    return <p role="status" className="p-3 text-sm text-muted-foreground">{daily('noGadgetData')}</p>;
  }

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-3 overflow-y-auto overflow-x-hidden">
      {/* Top Header: Symbol, Value, Change & Controls */}
      <div className="flex shrink-0 items-start justify-between border-b border-border pb-2.5">
        <div className="space-y-1">
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground font-mono">
            <span className="font-bold text-foreground">{targetSymbol}</span>
            <span>•</span>
            <span>{providerDelay || daily('freshnessUnknown')}</span>
          </div>

          <div className="flex items-baseline gap-2">
            <span className="text-xl font-bold font-mono tracking-tight text-foreground">
              {formatChartValue(currentValue, { currency, unit, locale: display.locale })}
            </span>

            {/* Change delta with visible sign, percentage, and text tag (not color only) */}
            <div
              className={`inline-flex items-center gap-0.5 text-xs font-mono font-semibold ${
                isPositive ? 'text-positive' : 'text-destructive'
              }`}
            >
              {isPositive ? (
                <ArrowUpRight className="w-3.5 h-3.5" />
              ) : (
                <ArrowDownRight className="w-3.5 h-3.5" />
              )}
              <span>
                {isPositive ? '+' : ''}
                {changePercent === null ? '—' : `${changePercent.toFixed(2)}%`}
              </span>
              <span className="text-[10px] text-muted-foreground font-normal">
                ({isPositive ? '+' : ''}
                {formatChartValue(changeValue, { currency, unit, locale: display.locale })})
              </span>
            </div>
          </div>
        </div>

        {/* Chart Kind Toggle (Area / Line / Bar) */}
        <div className="flex items-center gap-0.5 border border-border rounded-md p-0.5 bg-muted/20">
          <button
            type="button"
            onClick={() => setChartKind('area')}
            className={`p-1 rounded text-xs transition-colors ${
              chartKind === 'area'
                ? 'bg-card text-primary font-bold shadow-xs'
                : 'text-muted-foreground hover:text-foreground'
            }`}
            title={ts('chartArea')}
            aria-label={ts('chartAreaAria')}
          >
            <Activity className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setChartKind('line')}
            className={`p-1 rounded text-xs transition-colors ${
              chartKind === 'line'
                ? 'bg-card text-primary font-bold shadow-xs'
                : 'text-muted-foreground hover:text-foreground'
            }`}
            title={ts('chartLine')}
            aria-label={ts('chartLineAria')}
          >
            <LineChartIcon className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setChartKind('bar')}
            className={`p-1 rounded text-xs transition-colors ${
              chartKind === 'bar'
                ? 'bg-card text-primary font-bold shadow-xs'
                : 'text-muted-foreground hover:text-foreground'
            }`}
            title={ts('chartBar')}
            aria-label={ts('chartBarAria')}
          >
            <BarChart2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Time Range Filter Bar */}
      <div className="flex shrink-0 items-center justify-between text-xs">
        <div className="flex items-center gap-1">
          {(['24h', '7d', '30d', '90d', '1y', 'all'] as ChartTimeRange[]).map((range) => (
            <button
              key={range}
              type="button"
              onClick={() => setTimeRange(range)}
              className={`px-1.5 py-0.5 rounded text-[11px] font-mono transition-colors ${
                timeRange === range
                  ? 'bg-primary/20 text-primary font-bold'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {range.toUpperCase()}
            </button>
          ))}
        </div>

        <span className="text-[10px] text-muted-foreground font-mono">
          {visiblePoints.length} observations
        </span>
      </div>

      {/* Main Responsive Recharts View */}
      <div className="flex-1 w-full min-h-[140px] shrink-0 relative">
        {chartKind === 'area' && (
          <BaseAreaChart
            data={chartData}
            series={series}
            currency={currency}
            unit={unit}
            providerDelay={providerDelay}
            height="100%"
          />
        )}
        {chartKind === 'line' && (
          <BaseLineChart
            data={chartData}
            series={series}
            currency={currency}
            unit={unit}
            providerDelay={providerDelay}
            height="100%"
          />
        )}
        {chartKind === 'bar' && (
          <BaseBarChart
            data={chartData}
            series={series}
            currency={currency}
            unit={unit}
            providerDelay={providerDelay}
            height="100%"
          />
        )}
      </div>
    </div>
  );
}
