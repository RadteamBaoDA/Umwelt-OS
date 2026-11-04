'use client';

import React from 'react';
import { formatChartValue, type ChartDataPoint } from '@/core/charts/recharts-wrapper';
import { useDisplayPreferences } from '@/core/query-provider';
import { DeltaPill } from './gadget-kpi';
import type { BetterDirection } from './metric-direction';
import { Sparkline } from './sparkline';

/** Props for SeriesRow. */
export interface SeriesRowProps {
  label: string;
  value: number | null;
  currency?: string;
  unit?: string;
  series?: ChartDataPoint[];
  deltaPercent?: number | null;
  better?: BetterDirection;
  /** Custom value formatter; defaults to `formatChartValue` with the display locale. */
  format?: (value: number) => string;
}

/**
 * One multi-series list row: `label | sparkline (96x24) | value | delta`.
 *
 * @param props Label, latest value, formatting, optional history and delta.
 * @returns Flex row element.
 */
export function SeriesRow({ label, value, currency, unit, series, deltaPercent, better, format }: SeriesRowProps) {
  const display = useDisplayPreferences();
  return (
    <div className="flex items-center gap-3 py-1.5 text-sm">
      <span className="min-w-0 flex-1 truncate text-foreground">{label}</span>
      <span className="w-24 shrink-0">{series && series.length >= 2 ? <Sparkline data={series} width={96} height={24} line /> : null}</span>
      <span className="shrink-0 text-right font-medium tabular-nums text-foreground">
        {value === null ? '—' : format ? format(value) : formatChartValue(value, { currency, unit: currency ? unit : undefined, locale: display.locale })}
      </span>
      {deltaPercent !== undefined && <DeltaPill percent={deltaPercent} better={better} />}
    </div>
  );
}
