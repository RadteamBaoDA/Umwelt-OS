'use client';

import { ArrowDownRight, ArrowUpRight } from 'lucide-react';
import React from 'react';
import { formatChartValue, type ChartDataPoint } from '@/core/charts/chart-format';
import { useDisplayPreferences } from '@/core/query-provider';
import type { BetterDirection } from './metric-direction';
import { Sparkline } from './sparkline';

/** Props for DeltaPill. */
export interface DeltaPillProps {
  /** Signed percent change; null/NaN/0 renders neutral grey with no arrow. */
  percent: number | null | undefined;
  /** Which direction is an improvement; colours by outcome, not direction. Default neutral. */
  better?: BetterDirection;
  /** Period label such as "7d" (already localized by the caller). */
  periodLabel?: string;
}

/** Outcome tone: flat or neutral-direction deltas are neutral; otherwise positive when the move matches `better`. */
function deltaTone(flat: boolean, up: boolean, better: 'up' | 'down' | 'neutral'): 'neutral' | 'positive' | 'negative' {
  if (flat || better === 'neutral') return 'neutral';
  return (up ? better === 'up' : better === 'down') ? 'positive' : 'negative';
}

/**
 * Signed percent-change pill. Colour reflects outcome (good/bad per `better`); the sign, arrow and text
 * carry the meaning too, so colour is never the only cue. Zero or unknown change is neutral with no arrow.
 *
 * @param props Percent, better-direction and optional period label.
 * @returns Inline pill element.
 */
export function DeltaPill({ percent, better = 'neutral', periodLabel }: DeltaPillProps) {
  const known = percent !== null && percent !== undefined && Number.isFinite(percent);
  const flat = !known || percent === 0;
  const up = !flat && (percent as number) > 0;
  const tone = deltaTone(flat, up, better);
  const toneClass =
    tone === 'positive'
      ? 'bg-pill-positive text-pill-positive-foreground'
      : tone === 'negative'
        ? 'bg-pill-negative text-pill-negative-foreground'
        : 'bg-muted text-muted-foreground';
  const Arrow = up ? ArrowUpRight : ArrowDownRight;
  return (
    <span
      data-testid="delta-pill"
      data-tone={tone}
      className={`inline-flex items-center gap-0.5 whitespace-nowrap rounded-full px-1.5 py-0.5 text-xs font-medium tabular-nums ${toneClass}`}
    >
      {!flat && <Arrow className="size-3" aria-hidden="true" />}
      <span>{!known ? '—' : flat ? '0.0%' : `${up ? '+' : '−'}${Math.abs(percent as number).toFixed(1)}%`}</span>
      {periodLabel && <span className="font-normal opacity-80">{periodLabel}</span>}
    </span>
  );
}

/** Props for GadgetKpi. */
export interface GadgetKpiProps {
  /** Muted 12px label above the figure. */
  label: string;
  /** Current value; null shows an em dash. */
  value: number | null;
  currency?: string;
  unit?: string;
  /** `lg` = 40px figure (default), `sm` = 24px. */
  size?: 'lg' | 'sm';
  /** Signed percent change vs the previous period. */
  deltaPercent?: number | null;
  better?: BetterDirection;
  periodLabel?: string;
  /** Optional history for the trailing sparkline. */
  series?: ChartDataPoint[];
  sparkHeight?: number;
  /** Custom figure formatter; defaults to `formatChartValue` with the display locale. */
  format?: (value: number) => string;
}

/**
 * KPI block: label, figure, delta pill and optional sparkline. Figure formatting reuses `formatChartValue`
 * with the user's display locale.
 *
 * @param props Label, value, formatting, delta and optional series.
 * @returns Stacked KPI element.
 */
export function GadgetKpi({
  label,
  value,
  currency,
  unit,
  size = 'lg',
  deltaPercent,
  better,
  periodLabel,
  series,
  sparkHeight = 40,
  format,
}: GadgetKpiProps) {
  const display = useDisplayPreferences();
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="truncate text-xs text-muted-foreground">{label}</div>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className={`${size === 'sm' ? 'text-figure-sm' : 'text-figure'} text-foreground`}>
          {value === null
            ? '—'
            : format
              ? format(value)
              : formatChartValue(value, { currency, unit: currency ? unit : undefined, locale: display.locale })}
        </span>
        {!currency && unit && value !== null && <span className="text-xs text-muted-foreground">{unit}</span>}
        {deltaPercent !== undefined && <DeltaPill percent={deltaPercent} better={better} periodLabel={periodLabel} />}
      </div>
      {series && series.length >= 2 && <Sparkline data={series} height={sparkHeight} />}
    </div>
  );
}
