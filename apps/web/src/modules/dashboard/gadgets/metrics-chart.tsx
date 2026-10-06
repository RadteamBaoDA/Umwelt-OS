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
  const daily = useTranslations('daily');
  const display = useDisplayPreferences();

  const definition = instance.definition;
  const targetSymbol =
    definition.scope?.symbols?.[0] ||
    definition.filters?.keywords?.[0] ||
    'Market data';

  const [timeRange, setTimeRange] = useState<ChartTimeRange>('7d');
  const [chartKind, setChartKind] = useState<ChartKind>('area');

  // Load or generate chart data
  const data = useMemo(() => {
    // Never invent market values: without a connected provider the series is empty (R12).
    return initialData ?? [];
  }, [initialData]);

  // Derive latest value, start value, and absolute/percent change
  const { latestValue, startValue, changeAmount, changePercent, isPositive } = useMemo(() => {
    if (!data || data.length === 0) {
      return {
        latestValue: 0,
        startValue: 0,
        changeAmount: 0,
        changePercent: null,
        isPositive: true,
      };
    }
    const first = data[0].value;
    const last = data[data.length - 1].value;
    const diff = last - first;
    const pct = first !== 0 ? (diff / first) * 100 : null;
    return {
      latestValue: last,
      startValue: first,
      changeAmount: diff,
      changePercent: pct,
      isPositive: diff >= 0,
    };
  }, [data]);

  const series: ChartSeries[] = [
    {
      dataKey: 'value',
      name: targetSymbol,
      currency,
      unit,
    },
  ];

  if (data.length === 0) {
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
              {formatChartValue(latestValue, { currency, unit, locale: display.locale })}
            </span>

            {/* Change delta with visible sign, percentage, and text tag (not color only) */}
            <div
              className={`inline-flex items-center gap-0.5 text-xs font-mono font-semibold ${
                isPositive ? 'text-primary' : 'text-destructive'
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
                {formatChartValue(changeAmount, { currency, unit, locale: display.locale })})
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
            title="Area chart"
            aria-label="Switch to area chart view"
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
            title="Line chart"
            aria-label="Switch to line chart view"
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
            title="Bar chart"
            aria-label="Switch to bar chart view"
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
          {data.length} observations
        </span>
      </div>

      {/* Main Responsive Recharts View */}
      <div className="flex-1 w-full min-h-[140px] shrink-0 relative">
        {chartKind === 'area' && (
          <BaseAreaChart
            data={data}
            series={series}
            currency={currency}
            unit={unit}
            providerDelay={providerDelay}
            height="100%"
          />
        )}
        {chartKind === 'line' && (
          <BaseLineChart
            data={data}
            series={series}
            currency={currency}
            unit={unit}
            providerDelay={providerDelay}
            height="100%"
          />
        )}
        {chartKind === 'bar' && (
          <BaseBarChart
            data={data}
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
