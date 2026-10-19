/**
 * Semantic chart token palette and styling helpers for Umwelt-OS Generative UI charts.
 * Enforces CSS custom property references (--chart-1 through --chart-5) without literal hex colors.
 */

/** Canonical 5-color semantic chart palette mapped from globals.css. */
export const CHART_PALETTE = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
] as const;

/** Type representing one of the semantic chart palette CSS variable strings. */
export type SemanticChartColor = (typeof CHART_PALETTE)[number];

/** Shared semantic axis, grid, and tooltip token references for Recharts primitives. */
export const GENERATIVE_CHART_TOKENS = {
  gridStroke: 'var(--line)',
  axisStroke: 'var(--muted)',
  tooltipBackground: 'var(--surface)',
  tooltipBorder: 'var(--line)',
  tooltipText: 'var(--text)',
} as const;

/**
 * Resolves a series or slice color from the semantic palette, cycling deterministically by index.
 * Accepts an optional semantic CSS variable override while rejecting literal hex values.
 *
 * @param index - Zero-based series or slice index.
 * @param customColor - Optional custom CSS variable string (e.g. 'var(--chart-3)').
 * @returns Semantic CSS color variable reference.
 */
export function resolveSeriesColor(index: number, customColor?: string): string {
  const trimmed = customColor?.trim();
  // Only allow semantic CSS variables; reject literal hex or rgb values to preserve theme switching.
  if (trimmed && trimmed.startsWith('var(--')) {
    return trimmed;
  }
  const safeIndex = Math.abs(Math.trunc(index)) % CHART_PALETTE.length;
  return CHART_PALETTE[safeIndex];
}

