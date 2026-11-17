'use client';

import React, { useMemo } from 'react';
import { Area, AreaChart, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { ChartDataPoint } from '@/core/charts/chart-format';

/** Props for the axis-free Sparkline. */
export interface SparklineProps {
  /** Time-ordered points; fewer than two renders nothing. */
  data: ChartDataPoint[];
  /** Fixed pixel height (default 40). */
  height?: number;
  /** Optional fixed pixel width; omitted, the chart fills its parent's width. */
  width?: number;
  /** Epoch ms from which the series is drawn dashed (forecast). */
  forecastFrom?: number;
  /** Show a hover tooltip with start, end, min, max and % change. */
  tooltip?: boolean;
  /** Use a plain line instead of a filled area. */
  line?: boolean;
}

/**
 * Converts a point timestamp to epoch ms.
 *
 * @param t ISO string or epoch ms.
 * @returns Epoch ms (NaN when unparseable).
 */
function toMs(t: string | number): number {
  return typeof t === 'number' ? t : Date.parse(t);
}

/**
 * Recharts implementation of the sparkline, loaded lazily by `Sparkline` so Recharts stays out of the initial bundle.
 * Tiny axis-free trend chart in `--chart-1`. The sized, `aria-hidden` wrapper lives in `Sparkline`, so it exists (with the caller's height) before this chunk loads.
 * The forecast tail is a second dashed series that shares the boundary point so the line stays continuous.
 *
 * @param props Data, size and optional forecast boundary / tooltip.
 * @returns The bare chart filling the wrapper width (or `width` px when given), or null with fewer than two points.
 */
export function SparklineChart({ data, height = 40, width, forecastFrom, tooltip = false, line = false }: SparklineProps) {
  const rows = useMemo(() => {
    const mapped = data.map((p) => ({ ts: toMs(p.timestamp), past: p.value as number | null, future: null as number | null }));
    if (forecastFrom === undefined) return mapped;
    const split = mapped.findIndex((r) => r.ts >= forecastFrom);
    // No point before the boundary means no past segment: stay solid, dashes only mean something next to a past line.
    if (split <= 0) return mapped;
    return mapped.map((r, i) => ({
      ts: r.ts,
      past: i <= split ? r.past : null,
      future: i >= split ? r.past : null,
    }));
  }, [data, forecastFrom]);

  if (data.length < 2) return null;

  const stroke = 'var(--chart-1)';
  const margin = { top: 2, right: 2, bottom: 2, left: 2 };
  const common = (
    <>
      <XAxis dataKey="ts" hide />
      <YAxis hide domain={['dataMin', 'dataMax']} />
      {tooltip && <Tooltip content={<SparkTooltip data={data} />} />}
    </>
  );
  const dashed = forecastFrom !== undefined;

  // Explicit width renders the chart directly; otherwise ResponsiveContainer fills the parent so nothing is clipped.
  const sized = { height, data: rows, margin } as const;
  const chart = line ? (
    <LineChart {...sized} width={width}>
      {common}
      <Line dataKey="past" stroke={stroke} strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
      {dashed && (
        <Line dataKey="future" stroke={stroke} strokeWidth={1.5} strokeDasharray="3 3" dot={false} isAnimationActive={false} connectNulls />
      )}
    </LineChart>
  ) : (
    <AreaChart {...sized} width={width}>
      {common}
      <Area
        dataKey="past"
        type="monotone"
        stroke={stroke}
        strokeWidth={1.5}
        fill={stroke}
        fillOpacity={0.12}
        dot={false}
        isAnimationActive={false}
        connectNulls
      />
      {dashed && (
        <Area
          dataKey="future"
          type="monotone"
          stroke={stroke}
          strokeWidth={1.5}
          strokeDasharray="3 3"
          fill={stroke}
          fillOpacity={0.06}
          dot={false}
          isAnimationActive={false}
          connectNulls
        />
      )}
    </AreaChart>
  );

  return width === undefined ? (
    <ResponsiveContainer width="100%" height={height}>
      {chart}
    </ResponsiveContainer>
  ) : (
    chart
  );
}

/**
 * Tooltip body summarising the whole series (start, end, min, max, % change) regardless of hover position.
 *
 * @param props The full point list; Recharts injects `active`.
 * @returns A compact summary card while hovering, otherwise null.
 */
function SparkTooltip({ data, active }: { data: ChartDataPoint[]; active?: boolean }) {
  if (!active) return null;
  const values = data.map((p) => p.value);
  const start = values[0];
  const end = values[values.length - 1];
  const pct = start !== 0 ? ((end - start) / Math.abs(start)) * 100 : null;
  return (
    <div className="rounded-md border border-border bg-popover px-2 py-1 text-[11px] tabular-nums text-popover-foreground shadow-sm">
      <div>
        {start} → {end}
        {pct === null ? '' : ` (${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%)`}
      </div>
      <div className="text-muted-foreground">
        {Math.min(...values)} – {Math.max(...values)}
      </div>
    </div>
  );
}
