'use client';

import { useTheme } from 'next-themes';
import React, { useEffect, useId, useMemo, useState } from 'react';
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

/**
 * Formats a numeric value according to currency, unit, and locale.
 *
 * @param value The number to format.
 * @param options Currency, unit, locale, and precision options.
 * @returns Localized formatted string.
 */
export function formatChartValue(
  value: number,
  options?: {
    currency?: string;
    unit?: string;
    locale?: AppLocaleId;
    precision?: number;
  },
): string {
  if (value === null || value === undefined || isNaN(value)) {
    return '—';
  }

  const locale = options?.locale ?? 'en-us';
  const canonicalLocale = normalizeFormattingLocale(locale);
  const precision = options?.precision ?? 2;

  if (options?.currency) {
    try {
      return new Intl.NumberFormat(canonicalLocale, {
        style: 'currency',
        currency: options.currency,
        maximumFractionDigits: precision,
        minimumFractionDigits: 0,
      }).format(value);
    } catch {
      return `${value.toFixed(precision)} ${options.currency}`;
    }
  }

  if (options?.unit === '%' || options?.unit === 'percent') {
    return new Intl.NumberFormat(canonicalLocale, {
      style: 'percent',
      maximumFractionDigits: precision,
    }).format(value > 1 ? value / 100 : value);
  }

  const formatted = new Intl.NumberFormat(canonicalLocale, {
    maximumFractionDigits: precision,
  }).format(value);

  return options?.unit ? `${formatted} ${options.unit}` : formatted;
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
      return new Intl.DateTimeFormat(canonicalLocale, {
        month: 'short',
        day: 'numeric',
        timeZone: timezone,
      }).format(date);
    }

    return new Intl.DateTimeFormat(canonicalLocale, {
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
  title = 'No data available',
  message = 'There is no observation or time-series data for the selected range.',
  className = '',
}: ChartEmptyStateProps) {
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
          <span>Provider freshness</span>
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
  return (
    <div className="sr-only">
      <h4>{title || 'Chart data summary'}</h4>
      <table>
        <caption>Accessible data table representing chart values</caption>
        <thead>
          <tr>
            <th scope="col">Timestamp</th>
            {series.map((s) => (
              <th key={s.dataKey} scope="col">
                {s.name} ({s.currency || s.unit || 'value'})
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
  const [isMounted, setIsMounted] = useState<boolean>(false);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  if (!isMounted) {
    return (
      <div
        className={`w-full flex items-center justify-center bg-card/50 animate-pulse rounded-lg ${className}`}
        style={{ height, minHeight }}
      >
        <span className="sr-only">Loading chart visualization...</span>
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
 * Theme-aware area chart with smooth gradient fills and localized formatting.
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
  const gradientId = useId();

  const isDark = resolvedTheme === 'dark';
  const colors = useMemo(() => getChartColors(isDark), [isDark]);

  if (!data || data.length === 0) {
    return <ChartEmptyState title={emptyTitle} message={emptyMessage} />;
  }

  return (
    <>
      <ChartAccessibleTable data={data} series={series} />
      <RechartsContainer height={height}>
        <AreaChart data={data} margin={{ top: 10, right: 10, left: -15, bottom: 0 }}>
          <defs>
            {series.map((s, idx) => {
              const color = s.color || colors.series[idx % colors.series.length];
              return (
                <linearGradient
                  key={s.dataKey}
                  id={`${gradientId}-${s.dataKey}`}
                  x1="0"
                  y1="0"
                  x2="0"
                  y2="1"
                >
                  <stop offset="5%" stopColor={color} stopOpacity={0.4} />
                  <stop offset="95%" stopColor={color} stopOpacity={0.02} />
                </linearGradient>
              );
            })}
          </defs>
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
            tickFormatter={(val) =>
              formatChartValue(val, {
                currency,
                unit,
                locale: display.locale,
                precision: 0,
              })
            }
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
                fillOpacity={1}
                fill={`url(#${gradientId}-${s.dataKey})`}
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

  if (!data || data.length === 0) {
    return <ChartEmptyState title={emptyTitle} message={emptyMessage} />;
  }

  return (
    <>
      <ChartAccessibleTable data={data} series={series} />
      <RechartsContainer height={height}>
        <LineChart data={data} margin={{ top: 10, right: 10, left: -15, bottom: 0 }}>
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
            tickFormatter={(val) =>
              formatChartValue(val, {
                currency,
                unit,
                locale: display.locale,
                precision: 0,
              })
            }
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
        <BarChart data={data} margin={{ top: 10, right: 10, left: -15, bottom: 0 }}>
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
            tickFormatter={(val) =>
              formatChartValue(val, {
                currency,
                unit,
                locale: display.locale,
                precision: 0,
              })
            }
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
