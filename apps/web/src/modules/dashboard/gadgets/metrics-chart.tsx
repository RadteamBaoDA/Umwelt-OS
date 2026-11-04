'use client';

import { Activity, BarChart2, LineChart as LineChartIcon } from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useMemo, useState } from 'react';
import {
  BaseAreaChart,
  BaseBarChart,
  BaseLineChart,
  ChartHistoryNote,
  formatChartValue,
  type ChartDataPoint,
  type ChartSeries,
} from '@/core/charts/recharts-wrapper';
import { useDisplayPreferences } from '@/core/query-provider';
import type { GadgetInstance } from '../api';
import { DeltaPill } from './gadget-kpi';

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
 * Header is label, 40px figure and neutral-aware delta pill; the range tabs sit under the plot and the kind
 * toggle shows only in wide (expanded) containers. Provider delay/collection text is reported to the frame footer by
 * FinanceChart. Its 140px chart minimum remain nonshrinking inside a bounded scroll root.
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
  const { currentValue, changePercent, isFlat } = useMemo(() => {
    if (!visiblePoints || visiblePoints.length === 0) {
      return {
        currentValue: 0,
        changeValue: 0,
        changePercent: null,
        isPositive: true,
        isFlat: true,
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
      isPositive: diff > 0,
      // Zero change is neutral: no arrow, no green/red.
      isFlat: diff === 0 || !Number.isFinite(diff),
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
    <div className="flex flex-col h-full bg-card text-card-foreground p-3 space-y-2 overflow-y-auto overflow-x-hidden">
      {/* Label above a 40px figure and delta pill; the kind toggle only appears in wide containers (expanded view). */}
      <div className="flex shrink-0 items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <div className="truncate text-xs text-muted-foreground">{targetSymbol}</div>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="text-figure text-foreground">
              {formatChartValue(currentValue, { currency, unit: currency ? unit : undefined, locale: display.locale })}
            </span>
            {/* Non-currency units are secondary to the figure itself. */}
            {!currency && unit && <span className="text-xs text-muted-foreground">{unit}</span>}
            {/* Flat or unknown change is neutral grey (never a false green arrow). */}
            {/* A single observation has no previous value: show the unknown dash, not a false 0.0%. */}
            <DeltaPill percent={visiblePoints.length < 2 ? null : isFlat ? 0 : changePercent} />
          </div>
        </div>

        {/* Chart Kind Toggle (Area / Line / Bar) */}
        <div className="hidden shrink-0 items-center gap-0.5 rounded-md border border-border bg-muted/20 p-0.5 @[40rem]/gadget:flex">
          {([
            ['area', Activity, ts('chartArea'), ts('chartAreaAria')],
            ['line', LineChartIcon, ts('chartLine'), ts('chartLineAria')],
            ['bar', BarChart2, ts('chartBar'), ts('chartBarAria')],
          ] as const).map(([kind, Icon, title, aria]) => (
            <button
              key={kind}
              type="button"
              onClick={() => setChartKind(kind)}
              className={`rounded p-1 text-xs transition-colors ${chartKind === kind ? 'bg-card font-semibold text-primary' : 'text-muted-foreground hover:text-foreground'}`}
              title={title}
              aria-label={aria}
            >
              <Icon className="size-3.5" />
            </button>
          ))}
        </div>
      </div>

      {/* Main Responsive Recharts View */}
      {/* With fewer than 2 points a plot is only a lone dot on a misleading axis; the history note below explains it. */}
      {visiblePoints.length >= 2 && <div className="flex-1 w-full min-h-[140px] shrink-0 relative">
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
      </div>}
      {/* Range selector as quiet text tabs under the plot */}
      <div className="flex shrink-0 items-center justify-between text-xs">
        <div className="flex items-center gap-1" role="group" aria-label={ts('chartRangeAria')}>
          {(['24h', '7d', '30d', '90d', '1y', 'all'] as ChartTimeRange[]).map((range) => (
            <button
              key={range}
              type="button"
              onClick={() => setTimeRange(range)}
              aria-pressed={timeRange === range}
              className={`rounded px-1.5 py-0.5 text-[11px] tabular-nums transition-colors ${timeRange === range ? 'font-semibold text-foreground underline underline-offset-4' : 'text-muted-foreground hover:text-foreground'}`}
            >
              {range.toUpperCase()}
            </button>
          ))}
        </div>
        <span className="text-xs tabular-nums text-muted-foreground">{t('chartObservationCount', { count: visiblePoints.length })}</span>
      </div>
      <div className="shrink-0"><ChartHistoryNote data={chartData} /></div>
    </div>
  );
}
