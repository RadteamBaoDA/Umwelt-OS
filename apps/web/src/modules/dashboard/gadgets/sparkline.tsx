'use client';

import dynamic from 'next/dynamic';
import React from 'react';
import type { SparklineProps } from './sparkline-chart';

export type { SparklineProps } from './sparkline-chart';

/** Recharts-backed chart, split into its own chunk; it renders only the inner chart, inside the wrapper `Sparkline` owns. */
const LazySparklineChart = dynamic(() => import('./sparkline-chart').then((m) => m.SparklineChart), {
  loading: () => null,
});

/**
 * Tiny axis-free trend chart in `--chart-1` (see `SparklineChart`). Fewer than two points render nothing, without
 * loading the chart chunk. Decorative (`aria-hidden`).
 *
 * @param props Data, size and optional forecast boundary / tooltip.
 * @returns The sized wrapper around the lazily loaded chart, or null with fewer than two points.
 */
export function Sparkline(props: SparklineProps) {
  if (props.data.length < 2) return null;
  // The box (caller height, full width, test id) is rendered here, not in the chunk or its loading state, because
  // next/dynamic loading components do not receive props: this keeps the size right before the chunk arrives.
  return (
    <div aria-hidden="true" className="w-full overflow-hidden" style={{ height: props.height ?? 40 }} data-testid="sparkline">
      <LazySparklineChart {...props} />
    </div>
  );
}
