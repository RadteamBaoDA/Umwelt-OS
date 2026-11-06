/** Recharts-free chart types and value formatting, split out so non-chart code does not pull Recharts into shared chunks. */
import { formatCurrency, formatNumber } from '@/core/format';
import { type AppLocaleId } from '@/core/i18n';

/** Single data point in a time-series or categorical chart. */
export interface ChartDataPoint {
  /** Timestamp (ISO string or unix ms) or category label. */
  timestamp: string | number;
  /** Primary numeric value. */
  value: number;
  /** Optional secondary or multi-series values. */
  [key: string]: unknown;
}

/** Configuration descriptor for a named series rendered on a chart. */
export interface ChartSeries {
  /** Property key matching the numeric value on each data point. */
  dataKey: string;
  /** Human-readable display label for legends and tooltips. */
  name: string;
  /** Primary stroke or fill color; if omitted, resolved from theme palette. */
  color?: string;
  /** Unit indicator (e.g. '%', 'pts', 'MB'). */
  unit?: string;
  /** Currency code if this series represents money (e.g. 'USD', 'VND'). */
  currency?: string;
}

/** Options accepted by `formatChartValue`. */
export interface ChartValueFormatOptions {
  currency?: string;
  unit?: string;
  locale?: AppLocaleId;
  precision?: number;
  compact?: boolean;
}

/**
 * Formats a currency amount; an invalid ISO currency code makes Intl throw, in which case the value falls back to
 * `<fixed-precision number> <code>` instead of failing the chart render.
 */
function formatChartCurrency(
  value: number,
  currency: string,
  locale: AppLocaleId,
  precision: number,
  maximumFractionDigits: number,
  compactNotation: { notation?: 'compact' },
): string {
  try {
    return formatCurrency(value, locale, currency, {
      ...compactNotation,
      maximumFractionDigits,
      minimumFractionDigits: 0,
    });
  } catch {
    return `${value.toFixed(precision)} ${currency}`;
  }
}

/**
 * Formats a numeric value according to currency, unit, and locale.
 *
 * @param value The number to format.
 * @param options Currency, unit, locale, precision, and optional compact notation for large values.
 * @returns Localized formatted string.
 */
export function formatChartValue(value: number, options?: ChartValueFormatOptions): string {
  if (value === null || value === undefined || isNaN(value)) {
    return '—';
  }

  const { currency, unit, locale = 'en-us', precision = 2, compact: wantsCompact } = options ?? {};
  const compact = Boolean(wantsCompact && Math.abs(value) >= 1000);
  const maximumFractionDigits = compact ? Math.min(precision, 1) : precision;
  const compactNotation = compact ? { notation: 'compact' as const } : {};

  if (currency) {
    return formatChartCurrency(value, currency, locale, precision, maximumFractionDigits, compactNotation);
  }

  if (unit === '%' || unit === 'percent') {
    return formatNumber(value > 1 ? value / 100 : value, locale, {
      style: 'percent',
      ...compactNotation,
      maximumFractionDigits,
    });
  }

  const formatted = formatNumber(value, locale, {
    ...compactNotation,
    maximumFractionDigits,
  });

  return unit ? `${formatted} ${unit}` : formatted;
}
