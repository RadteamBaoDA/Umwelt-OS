'use client';

import { useTheme } from 'next-themes';
import { useTranslations } from 'next-intl';
import React, { useEffect, useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from 'recharts';
import { BarChart3 } from 'lucide-react';
import { normalizeFormattingLocale, type AppLocaleId } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';

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

/** Props for the SSR-safe RechartsContainer. */
export interface RechartsContainerProps {
  /** Height in pixels or CSS units (defaults to 100%). */
  height?: number | string;
  /** Minimum height in pixels to prevent SVG collapses before container measurement. */
  minHeight?: number;
  /** Optional container class name. */
  className?: string;
  /** Chart element tree. */
  children: React.ReactNode;
}

/** Props for the clean zero-data empty state card. */
export interface ChartEmptyStateProps {
  /** Headline title for the empty state. */
  title?: string;
  /** Explanation or reason for absent data. */
  message?: string;
  /** Optional class name. */
  className?: string;
}

/** Theme-derived color palette for charts in light and dark modes. */
export interface ChartThemeColors {
  primary: string;
  secondary: string;
  accent: string;
  danger: string;
  grid: string;
  axisText: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
  series: string[];
}

/**
 * Returns theme-aware CSS colors for SVG charts based on dark/light mode.
 *
 * @param _isDark Ignored; kept for call-site compatibility because CSS variables follow the theme.
 * @returns Set of color tokens compatible with SVG fills and strokes.
 */
export function getChartColors(_isDark?: boolean): ChartThemeColors {
  // CSS variables resolve per theme in the browser, so no literal colors or dark/light branch is needed.
  return {
    primary: 'var(--chart-1)',
    secondary: 'var(--chart-2)',
    accent: 'var(--chart-4)',
    danger: 'var(--danger)',
    grid: 'var(--line)',
    axisText: 'var(--muted)',
    tooltipBg: 'var(--surface)',
    tooltipBorder: 'var(--line)',
    tooltipText: 'var(--text)',
    series: ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)'],
  };
}

/** Maximum cached Intl formatter instances per type to bound memory across locales and currencies. */
const FORMAT_CACHE_LIMIT = 64;
const NUMBER_FORMAT_CACHE = new Map<string, Intl.NumberFormat>();
const DATE_FORMAT_CACHE = new Map<string, Intl.DateTimeFormat>();

/**
 * Returns a cached Intl.NumberFormat instance keyed by canonical locale and options,
 * evicting the oldest entry when the cache reaches FORMAT_CACHE_LIMIT.
 */
function getCachedNumberFormat(locale: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${locale}:${JSON.stringify(options)}`;
  let formatter = NUMBER_FORMAT_CACHE.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, options);
    if (NUMBER_FORMAT_CACHE.size >= FORMAT_CACHE_LIMIT) {
      const oldestKey = NUMBER_FORMAT_CACHE.keys().next().value;
      if (oldestKey !== undefined) NUMBER_FORMAT_CACHE.delete(oldestKey);
    }
    NUMBER_FORMAT_CACHE.set(key, formatter);
  }
  return formatter;
}

/**
 * Returns a cached Intl.DateTimeFormat instance keyed by canonical locale and options,
 * evicting the oldest entry when the cache reaches FORMAT_CACHE_LIMIT.
 */
function getCachedDateFormat(locale: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale}:${JSON.stringify(options)}`;
  let formatter = DATE_FORMAT_CACHE.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(locale, options);
    if (DATE_FORMAT_CACHE.size >= FORMAT_CACHE_LIMIT) {
      const oldestKey = DATE_FORMAT_CACHE.keys().next().value;
      if (oldestKey !== undefined) DATE_FORMAT_CACHE.delete(oldestKey);
    }
    DATE_FORMAT_CACHE.set(key, formatter);
  }
  return formatter;
}

/**
 * Formats a numeric value according to currency, unit, and locale.
 *
 * @param value The number to format.
 * @param options Currency, unit, locale, precision, and optional compact notation for large values.
 * @returns Localized formatted string.
 */
export function formatChartValue(
  value: number,
  options?: {
    currency?: string;
    unit?: string;
    locale?: AppLocaleId;
    precision?: number;
    compact?: boolean;
  },
): string {
  if (value === null || value === undefined || isNaN(value)) {
    return '—';
  }

  const locale = options?.locale ?? 'en-us';
  const canonicalLocale = normalizeFormattingLocale(locale);
  const precision = options?.precision ?? 2;
  const compact = Boolean(options?.compact && Math.abs(value) >= 1000);
  const maximumFractionDigits = compact ? Math.min(precision, 1) : precision;

  if (options?.currency) {
    try {
      return getCachedNumberFormat(canonicalLocale, {
        style: 'currency',
        currency: options.currency,
        ...(compact ? { notation: 'compact' as const } : {}),
        maximumFractionDigits,
        minimumFractionDigits: 0,
      }).format(value);
    } catch {
      return `${value.toFixed(precision)} ${options.currency}`;
    }
  }

  if (options?.unit === '%' || options?.unit === 'percent') {
    return getCachedNumberFormat(canonicalLocale, {
      style: 'percent',
      ...(compact ? { notation: 'compact' as const } : {}),
      maximumFractionDigits,
    }).format(value > 1 ? value / 100 : value);
  }

  const formatted = getCachedNumberFormat(canonicalLocale, {
    ...(compact ? { notation: 'compact' as const } : {}),
    maximumFractionDigits,
  }).format(value);

  return options?.unit ? `${formatted} ${options.unit}` : formatted;
}

/** Fixed Y-axis width (px): fits compact labels such as "-77.2K" at 11px so ticks never wrap or clip. */
const Y_AXIS_WIDTH = 52;

/**
 * Computes a Y domain padded around the plotted values so a series far from zero (e.g. a 77,194 price) is not
 * flattened by a zero-based axis. Flat series get a padding proportional to their magnitude (or 1 at zero).
 *
 * @param data Chart rows.
 * @param series Series whose dataKeys are plotted.
 * @returns [min, max] with 10% range padding, or ['auto','auto'] when no finite value exists.
 */
export function paddedYDomain(data: ChartDataPoint[], series: ChartSeries[]): [number | 'auto', number | 'auto'] {
  let min = Infinity;
  let max = -Infinity;
  for (const row of data) {
    for (const s of series) {
      const v = Number(row[s.dataKey] ?? row.value);
      if (Number.isFinite(v)) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
  }
  if (min === Infinity) return ['auto', 'auto'];
  const pad = max > min ? (max - min) * 0.1 : Math.abs(max) * 0.01 || 1;
  return [min - pad, max + pad];
}

/** Assumed upper bound of ticks across the Y domain; a conservative (smaller) step yields more digits. */
const Y_TICK_COUNT = 5;

/**
 * Formats a Y tick compactly without unit or currency (the header already shows them) so it stays on one line.
 * When the plotted domain span is known, fraction digits follow the tick step (in the compact unit) so adjacent
 * ticks over a narrow range never collapse to the same label (e.g. "77.5K" repeated).
 *
 * @param value Tick value.
 * @param locale Application locale.
 * @param span Optional numeric Y-domain span (max - min); omit to use one compact fraction digit.
 * @returns Compact localized number, e.g. "77.2K" or "77.37K" for a narrow span.
 */
export function formatYTick(value: number, locale: AppLocaleId, span?: number): string {
  if (!span || !Number.isFinite(span) || span <= 0 || !Number.isFinite(value)) {
    return formatChartValue(value, { locale, precision: 2, compact: true });
  }
  const abs = Math.abs(value);
  const scale = abs >= 1e9 ? 1e9 : abs >= 1e6 ? 1e6 : abs >= 1e3 ? 1e3 : 1;
  const step = span / Y_TICK_COUNT / scale;
  const digits = Math.min(4, Math.max(0, Math.ceil(-Math.log10(step) - 1e-9)));
  return getCachedNumberFormat(normalizeFormattingLocale(locale), {
    ...(abs >= 1000 ? { notation: 'compact' as const } : {}),
    maximumFractionDigits: digits,
  }).format(value);
}

/**
 * Returns the numeric span of a padded Y domain.
 *
 * @param domain Domain from {@link paddedYDomain}.
 * @returns max - min, or undefined when the domain is not numeric.
 */
function domainSpan(domain: [number | 'auto', number | 'auto']): number | undefined {
  return typeof domain[0] === 'number' && typeof domain[1] === 'number' ? domain[1] - domain[0] : undefined;
}

/**
 * Formats a timestamp into an abbreviated label suitable for chart axes or tooltips.
 *
 * @param timestamp ISO timestamp string, number ms, or Date object.
 * @param locale Current application locale.
 * @param timezone Target timezone identifier.
 * @param formatStyle Abbreviation style ('compact' for axis, 'full' for tooltip).
 * @returns Localized date-time string.
 */
export function formatChartTimestamp(
  timestamp: string | number,
  locale: AppLocaleId = 'en-us',
  timezone = 'UTC',
  formatStyle: 'compact' | 'full' = 'compact',
): string {
  try {
    const date = new Date(timestamp);
    if (isNaN(date.getTime())) return String(timestamp);

    const canonicalLocale = normalizeFormattingLocale(locale);

    if (formatStyle === 'compact') {
      return getCachedDateFormat(canonicalLocale, {
        month: 'short',
        day: 'numeric',
        timeZone: timezone,
      }).format(date);
    }

    return getCachedDateFormat(canonicalLocale, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: timezone,
    }).format(date);
  } catch {
    return String(timestamp);
  }
}

/**
 * Clean zero-data empty state component for charts.
 *
 * @param props Title, message, and layout classes.
 * @returns Accessible empty state container.
 */
export function ChartEmptyState({
  title,
  message,
  className = '',
}: ChartEmptyStateProps) {
  const ts = useTranslations('uiSweep');
  title ??= ts('chNoData');
  message ??= ts('chNoDataMsg');
  return (
    <div
      role="status"
      aria-label={title}
      className={`flex flex-col items-center justify-center h-full min-h-[160px] p-6 text-center text-muted-foreground ${className}`}
    >
      <div className="w-10 h-10 rounded-full bg-muted/20 flex items-center justify-center mb-2">
        <BarChart3 className="w-5 h-5 text-muted-foreground/70" />
      </div>
      <p className="text-xs font-semibold text-foreground mb-1">{title}</p>
      <p className="text-[11px] max-w-xs leading-relaxed text-muted-foreground">{message}</p>
    </div>
  );
}

/**
 * Custom tooltip renderer for Recharts time-series charts.
 * Presents formatted timestamp, value, units, currency, and provider delay metadata.
 *
 * @param props Tooltip active state, payload values, labels, currency, unit, and locale.
 * @returns Accessible tooltip element.
 */
export function ChartCustomTooltip({
  active,
  payload,
  label,
  currency,
  unit,
  providerDelay,
  locale = 'en-us',
  timezone = 'UTC',
}: {
  active?: boolean;
  payload?: Array<{ name?: string; value?: number; color?: string; dataKey?: string }>;
  label?: string | number;
  currency?: string;
  unit?: string;
  providerDelay?: string | null;
  locale?: AppLocaleId;
  timezone?: string;
}) {
  const ts = useTranslations('uiSweep');
  if (!active || !payload || payload.length === 0) return null;

  return (
    <div className="rounded-lg border border-border bg-card p-2.5 shadow-md text-card-foreground text-xs min-w-[140px] z-50">
      {label !== undefined && (
        <div className="text-[11px] text-muted-foreground mb-1.5 font-mono">
          {formatChartTimestamp(label, locale, timezone, 'full')}
        </div>
      )}
      <div className="space-y-1">
        {payload.map((item, idx) => (
          <div key={idx} className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-1.5 min-w-0">
              <span
                className="w-2 rounded-full shrink-0 h-2"
                style={{ backgroundColor: item.color }}
              />
              <span className="text-muted-foreground truncate">{item.name || item.dataKey}</span>
            </div>
            <span className="font-semibold font-mono text-foreground">
              {typeof item.value === 'number'
                ? formatChartValue(item.value, { currency, unit, locale })
                : String(item.value ?? '—')}
            </span>
          </div>
        ))}
      </div>
      {providerDelay && (
        <div className="mt-2 pt-1.5 border-t border-border/60 text-[10px] text-muted-foreground flex items-center justify-between">
          <span>{ts('chFreshness')}</span>
          <span className="font-mono">{providerDelay}</span>
        </div>
      )}
    </div>
  );
}

/**
 * Screen-reader accessible alternative table for chart data points.
 * Ensures charts are not color-only or visual-only per AGENTS.md guidelines.
 *
 * @param props Series data points, series descriptions, and optional title.
 * @returns Screen-reader accessible table element.
 */
export function ChartAccessibleTable({
  data,
  series,
  title,
}: {
  data: ChartDataPoint[];
  series: ChartSeries[];
  title?: string;
}) {
  const ts = useTranslations('uiSweep');
  return (
    <div className="sr-only">
      <h4>{title || ts('chSummary')}</h4>
      <table>
        <caption>{ts('chTable')}</caption>
        <thead>
          <tr>
            <th scope="col">{ts('chTimestamp')}</th>
            {series.map((s) => (
              <th key={s.dataKey} scope="col">
                {s.name} ({s.currency || s.unit || ts('chValue')})
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.slice(0, 50).map((row, index) => (
            <tr key={index}>
              <td>{String(row.timestamp)}</td>
              {series.map((s) => (
                <td key={s.dataKey}>{String(row[s.dataKey] ?? row.value)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Recharts wrapper isolating SSR and browser measurement.
 * Mounts only after client hydration and guards against SVG zero-size crashes.
 *
 * @param props Container configuration and children.
 * @returns Fully responsive, client-safe chart container.
 */
export function RechartsContainer({
  height = '100%',
  minHeight = 140,
  className = '',
  children,
}: RechartsContainerProps) {
  const ts = useTranslations('uiSweep');
  const [isMounted, setIsMounted] = useState<boolean>(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncs state to an external or prop change; reset-on-change is intentional here
    setIsMounted(true);
  }, []);

  if (!isMounted) {
    return (
      <div
        className={`w-full flex items-center justify-center bg-card/50 animate-pulse rounded-lg ${className}`}
        style={{ height, minHeight }}
      >
        <span className="sr-only">{ts('chLoading')}</span>
      </div>
    );
  }

  return (
    <div className={`w-full relative ${className}`} style={{ height, minHeight }}>
      <ResponsiveContainer width="100%" height="100%" minHeight={minHeight}>
        {children as React.ReactElement}
      </ResponsiveContainer>
    </div>
  );
}

/** Props for the BaseAreaChart component. */
export interface BaseAreaChartProps {
  data: ChartDataPoint[];
  series?: ChartSeries[];
  height?: number | string;
  currency?: string;
  unit?: string;
  providerDelay?: string | null;
  emptyTitle?: string;
  emptyMessage?: string;
  showGrid?: boolean;
}

/**
 * Theme-aware area chart with a flat low-opacity fill (no gradient), padded Y domain and localized formatting.
 *
 * @param props Data points, series configuration, currency/unit, and styling.
 * @returns Accessible area chart component.
 */
export function BaseAreaChart({
  data,
  series = [{ dataKey: 'value', name: 'Value' }],
  height = 200,
  currency,
  unit,
  providerDelay,
  emptyTitle,
  emptyMessage,
  showGrid = true,
}: BaseAreaChartProps) {
  const { resolvedTheme } = useTheme();
  const display = useDisplayPreferences();

  const isDark = resolvedTheme === 'dark';
  const colors = useMemo(() => getChartColors(isDark), [isDark]);
  const yDomain = useMemo(() => paddedYDomain(data, series), [data, series]);

  if (!data || data.length === 0) {
    return <ChartEmptyState title={emptyTitle} message={emptyMessage} />;
  }

  return (
    <>
      <ChartAccessibleTable data={data} series={series} />
      <RechartsContainer height={height}>
        <AreaChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          {showGrid && (
            <CartesianGrid strokeDasharray="3 3" stroke={colors.grid} vertical={false} />
          )}
          <XAxis
            dataKey="timestamp"
            stroke={colors.axisText}
            fontSize={11}
            tickLine={false}
            axisLine={{ stroke: colors.grid }}
            tickFormatter={(val) =>
              formatChartTimestamp(val, display.locale, display.timezone, 'compact')
            }
          />
          <YAxis
            stroke={colors.axisText}
            fontSize={11}
            tickLine={false}
            axisLine={false}
            width={Y_AXIS_WIDTH}
            domain={yDomain}
            tickFormatter={(val) => formatYTick(val, display.locale, domainSpan(yDomain))}
          />
          <Tooltip
            content={
              <ChartCustomTooltip
                currency={currency}
                unit={unit}
                providerDelay={providerDelay}
                locale={display.locale}
                timezone={display.timezone}
              />
            }
          />
          {series.map((s, idx) => {
            const color = s.color || colors.series[idx % colors.series.length];
            return (
              <Area
                key={s.dataKey}
                type="monotone"
                dataKey={s.dataKey}
                name={s.name}
                stroke={color}
                strokeWidth={2}
                fill={color}
                fillOpacity={0.12}
              />
            );
          })}
        </AreaChart>
      </RechartsContainer>
    </>
  );
}

/** Props for the BaseLineChart component. */
export interface BaseLineChartProps {
  data: ChartDataPoint[];
  series?: ChartSeries[];
  height?: number | string;
  currency?: string;
  unit?: string;
  providerDelay?: string | null;
  emptyTitle?: string;
  emptyMessage?: string;
  showGrid?: boolean;
}

/**
 * Theme-aware multi-line chart with accessible tooltips and customizable series strokes.
 *
 * @param props Data points, series configuration, currency/unit, and styling.
 * @returns Accessible line chart component.
 */
export function BaseLineChart({
  data,
  series = [{ dataKey: 'value', name: 'Value' }],
  height = 200,
  currency,
  unit,
  providerDelay,
  emptyTitle,
  emptyMessage,
  showGrid = true,
}: BaseLineChartProps) {
  const { resolvedTheme } = useTheme();
  const display = useDisplayPreferences();
  const isDark = resolvedTheme === 'dark';
  const colors = useMemo(() => getChartColors(isDark), [isDark]);
  const yDomain = useMemo(() => paddedYDomain(data, series), [data, series]);

  if (!data || data.length === 0) {
    return <ChartEmptyState title={emptyTitle} message={emptyMessage} />;
  }

  return (
    <>
      <ChartAccessibleTable data={data} series={series} />
      <RechartsContainer height={height}>
        <LineChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          {showGrid && (
            <CartesianGrid strokeDasharray="3 3" stroke={colors.grid} vertical={false} />
          )}
          <XAxis
            dataKey="timestamp"
            stroke={colors.axisText}
            fontSize={11}
            tickLine={false}
            axisLine={{ stroke: colors.grid }}
            tickFormatter={(val) =>
              formatChartTimestamp(val, display.locale, display.timezone, 'compact')
            }
          />
          <YAxis
            stroke={colors.axisText}
            fontSize={11}
            tickLine={false}
            axisLine={false}
            width={Y_AXIS_WIDTH}
            domain={yDomain}
            tickFormatter={(val) => formatYTick(val, display.locale, domainSpan(yDomain))}
          />
          <Tooltip
            content={
              <ChartCustomTooltip
                currency={currency}
                unit={unit}
                providerDelay={providerDelay}
                locale={display.locale}
                timezone={display.timezone}
              />
            }
          />
          {series.map((s, idx) => {
            const color = s.color || colors.series[idx % colors.series.length];
            return (
              <Line
                key={s.dataKey}
                type="monotone"
                dataKey={s.dataKey}
                name={s.name}
                stroke={color}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, strokeWidth: 1 }}
              />
            );
          })}
        </LineChart>
      </RechartsContainer>
    </>
  );
}

/** Props for the BaseBarChart component. */
export interface BaseBarChartProps {
  data: ChartDataPoint[];
  series?: ChartSeries[];
  height?: number | string;
  currency?: string;
  unit?: string;
  providerDelay?: string | null;
  emptyTitle?: string;
  emptyMessage?: string;
  showGrid?: boolean;
}

/**
 * Theme-aware bar chart with rounded edges and localized value formatting.
 *
 * @param props Data points, series configuration, currency/unit, and styling.
 * @returns Accessible bar chart component.
 */
export function BaseBarChart({
  data,
  series = [{ dataKey: 'value', name: 'Value' }],
  height = 200,
  currency,
  unit,
  providerDelay,
  emptyTitle,
  emptyMessage,
  showGrid = true,
}: BaseBarChartProps) {
  const { resolvedTheme } = useTheme();
  const display = useDisplayPreferences();
  const isDark = resolvedTheme === 'dark';
  const colors = useMemo(() => getChartColors(isDark), [isDark]);

  if (!data || data.length === 0) {
    return <ChartEmptyState title={emptyTitle} message={emptyMessage} />;
  }

  return (
    <>
      <ChartAccessibleTable data={data} series={series} />
      <RechartsContainer height={height}>
        <BarChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          {showGrid && (
            <CartesianGrid strokeDasharray="3 3" stroke={colors.grid} vertical={false} />
          )}
          <XAxis
            dataKey="timestamp"
            stroke={colors.axisText}
            fontSize={11}
            tickLine={false}
            axisLine={{ stroke: colors.grid }}
            tickFormatter={(val) =>
              formatChartTimestamp(val, display.locale, display.timezone, 'compact')
            }
          />
          <YAxis
            stroke={colors.axisText}
            fontSize={11}
            tickLine={false}
            axisLine={false}
            width={Y_AXIS_WIDTH}
            domain={[0, 'auto']}
            tickFormatter={(val) => formatYTick(val, display.locale)}
          />
          <Tooltip
            content={
              <ChartCustomTooltip
                currency={currency}
                unit={unit}
                providerDelay={providerDelay}
                locale={display.locale}
                timezone={display.timezone}
              />
            }
          />
          {series.map((s, idx) => {
            const color = s.color || colors.series[idx % colors.series.length];
            return (
              <Bar
                key={s.dataKey}
                dataKey={s.dataKey}
                name={s.name}
                fill={color}
                radius={[4, 4, 0, 0]}
              />
            );
          })}
        </BarChart>
      </RechartsContainer>
    </>
  );
}
