'use client';

import * as React from 'react';
import { NextIntlClientProvider } from 'next-intl';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { BarChart3Icon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  ChartAccessibleTable,
  formatChartValue,
  type ChartDataPoint,
  type ChartSeries,
} from '@/core/charts/recharts-wrapper';
import { normalizeFormattingLocale, type AppLocaleId } from '@/core/i18n';
import { messages } from '@/core/messages';
import { SampleDataBadge } from '../sample-data-badge';
import { genUiCopy, resolveWidgetLocale } from '../../copy';
import { ExportOverlay } from '../../sandbox/export-overlay';
import { chartToStandaloneHtml } from '../../sandbox/chart-export';
import { GENERATIVE_CHART_TOKENS, resolveSeriesColor } from './config';

/** Flexible input datum accepted by GenerativeBarChart. */
export interface GenerativeBarDatum {
  /** Category or timestamp label displayed along the X axis. */
  label?: string;
  /** Alternative category field name. */
  name?: string;
  /** Alternative timestamp field name. */
  timestamp?: string | number;
  /** Primary numeric value for single-series bar charts. */
  value?: number;
  /** Additional keyed numeric series values. */
  [key: string]: unknown;
}

/** Flexible series descriptor supporting both frontend ChartSeries and backend ChartPayload series. */
export interface GenerativeChartSeriesInput {
  /** Frontend canonical dataKey. */
  dataKey?: string;
  /** Backend `ChartPayload.series[].key` alias. */
  key?: string;
  /** Frontend canonical series display name. */
  name?: string;
  /** Backend `ChartPayload.series[].label` alias. */
  label?: string;
  /** Frontend semantic color CSS variable. */
  color?: string;
  /** Backend `ChartPayload.series[].color_token` alias. */
  color_token?: string;
  /** Optional ISO currency code. */
  currency?: string;
  /** Optional unit suffix. */
  unit?: string;
}

/** Rendering variant selected by the persisted `chart.chart_type` (pie has its own widget). */
export type GenerativeCartesianChartType = 'bar' | 'line' | 'scatter' | 'distribution';

/** Props accepted by the native structured GenerativeBarChart component. */
export interface GenerativeBarChartProps {
  /** Chart headline title. */
  title: string;
  /** Optional subtitle or context description. */
  description?: string;
  /** Backend `ChartPayload.subtitle` alias. */
  subtitle?: string;
  /** Categorical or time-series data points. */
  data: GenerativeBarDatum[];
  /** Optional property key for category labels on each datum. */
  categoryKey?: string;
  /** Backend `ChartPayload.x_key` snake_case alias for `categoryKey`. */
  x_key?: string;
  /** Optional multi-series descriptors; defaults to a single 'value' series. */
  series?: Array<ChartSeries | GenerativeChartSeriesInput>;
  /** Optional ISO currency code (e.g. 'USD', 'VND'). */
  currency?: string;
  /** Optional measurement unit (e.g. '%', 'ms', 'req/s'). */
  unit?: string;
  /** Optional provenance source label. */
  source?: string;
  /** True when data is illustrative sample data (renders a "Sample data" badge). */
  isSample?: boolean;
  /** Application locale ('en-us' | 'vi-vi') for number/currency formatting. */
  locale?: AppLocaleId;
  /** Chart viewport height in pixels (defaults to 240). */
  height?: number;
  /** Plot variant: grouped bars (default), lines, scatter points or gapless histogram bars. */
  chartType?: GenerativeCartesianChartType;
}

/**
 * Normalizes flexible GenerativeBarDatum items into canonical ChartDataPoint records
 * required by Recharts and ChartAccessibleTable.
 *
 * @param data - Raw bar chart input array.
 * @param categoryKey - Optional property name holding the category label.
 * @returns Normalized ChartDataPoint array.
 */
export function normalizeBarData(data: GenerativeBarDatum[], categoryKey?: string): ChartDataPoint[] {
  return (data ?? []).map((item, idx) => {
    const customCategory = categoryKey ? item[categoryKey] : undefined;
    const label = customCategory ?? item.label ?? item.name ?? item.timestamp ?? `#${idx + 1}`;
    const numericValue = typeof item.value === 'number' ? item.value : Number(item.value ?? 0);
    return {
      ...item,
      timestamp: String(label),
      value: Number.isNaN(numericValue) ? 0 : numericValue,
    };
  });
}

/**
 * Normalizes frontend (`dataKey`/`name`) and backend (`key`/`label`/`color_token`) series descriptors
 * into canonical ChartSeries, defaulting to a single `value` series. Shared by bar and pie widgets.
 *
 * @param series - Raw series descriptors (may be empty or undefined).
 * @param title - Fallback series display name.
 * @param currency - Default ISO currency applied to each series.
 * @param unit - Default unit applied to each series.
 * @returns Non-empty canonical series list.
 */
export function normalizeBarSeries(
  series: Array<ChartSeries | GenerativeChartSeriesInput> | undefined,
  title: string,
  currency?: string,
  unit?: string,
): ChartSeries[] {
  if (series && series.length > 0) {
    return series.map((s, idx) => {
      const raw = s as GenerativeChartSeriesInput;
      return {
        dataKey: raw.dataKey || raw.key || 'value',
        name: raw.name || raw.label || title || 'Value',
        color: resolveSeriesColor(idx, raw.color ?? raw.color_token),
        currency: raw.currency ?? currency,
        unit: raw.unit ?? unit,
      };
    });
  }
  return [{ dataKey: 'value', name: title || 'Value', color: resolveSeriesColor(0), currency, unit }];
}

/**
 * Projects normalized rows to `{x, y}` scatter points; x is the numeric category label when it parses
 * as a finite number, otherwise the row index so categorical data still plots deterministically.
 *
 * @param rows - Normalized chart rows (`timestamp` holds the category label).
 * @param dataKey - Series key supplying the y value.
 * @returns Scatter points in row order.
 */
function scatterPoints(rows: ChartDataPoint[], dataKey: string): Array<{ x: number; y: number }> {
  return rows.map((row, idx) => {
    const x = Number(row.timestamp);
    const y = Number(row[dataKey] ?? row.value ?? 0);
    return { x: Number.isFinite(x) ? x : idx, y: Number.isFinite(y) ? y : 0 };
  });
}

/**
 * Native structured GenerativeBarChart widget using Recharts primitives (`BarChart`, `Bar`,
 * `XAxis`, `YAxis`, `CartesianGrid`, `Tooltip`, `Legend`), semantic `--chart-1..5` tokens,
 * `formatChartValue`, and `ChartAccessibleTable` from `@/core/charts/recharts-wrapper`.
 *
 * @param props - GenerativeBarChartProps configuration.
 * @returns Accessible, theme-aware bar chart card.
 */
export function GenerativeBarChart({
  title,
  description,
  subtitle,
  data,
  categoryKey,
  x_key,
  series,
  currency,
  unit,
  source,
  isSample,
  locale,
  height = 240,
  chartType = 'bar',
}: GenerativeBarChartProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const canonicalLocale = normalizeFormattingLocale(resolvedLocale);
  const copy = {
    emptyText: genUiCopy(resolvedLocale).genUiBarEmpty,
    sourceLabel: genUiCopy(resolvedLocale).genUiTableSource,
  };
  const resolvedDescription = description ?? subtitle;
  const resolvedCategoryKey = categoryKey ?? x_key;

  const normalizedData = React.useMemo(
    () => normalizeBarData(data, resolvedCategoryKey),
    [data, resolvedCategoryKey],
  );
  const resolvedSeries = React.useMemo(
    () => normalizeBarSeries(series, title, currency, unit),
    [currency, series, title, unit],
  );

  /** Standalone export (inline SVG + data table, no remote origins) for copy/download. */
  const exportHtml = React.useMemo(
    () =>
      chartToStandaloneHtml({
        title,
        description: resolvedDescription,
        source,
        sourceLabel: copy.sourceLabel,
        lang: resolvedLocale === 'vi-vi' ? 'vi' : 'en',
        kind: chartType === 'bar' ? 'bar' : chartType,
        series: resolvedSeries.map((s) => ({ name: s.name, color: s.color ?? '' })),
        rows: normalizedData.map((row) => ({
          label: String(row.timestamp),
          values: resolvedSeries.map((s) => {
            const v = Number(row[s.dataKey] ?? row.value ?? 0);
            return Number.isFinite(v) ? v : 0;
          }),
        })),
        format: (v) => formatChartValue(v, { currency, unit, locale: resolvedLocale }),
      }),
    [title, resolvedDescription, source, copy.sourceLabel, resolvedLocale, chartType, resolvedSeries, normalizedData, currency, unit],
  );

  return (
    <ExportOverlay title={title} html={exportHtml} ready={normalizedData.length > 0}>
    <Card
      data-testid="generative-bar-chart"
      className="my-2 w-full max-w-2xl border-border bg-surface text-foreground shadow-sm"
    >
      <CardHeader className="gap-1.5 border-b border-border pb-4">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <BarChart3Icon className="size-4" aria-hidden="true" />
            </span>
            <div>
              <CardTitle className="text-base font-semibold text-foreground">{title}</CardTitle>
              {resolvedDescription && (
                <CardDescription className="mt-0.5 text-xs text-muted-foreground">
                  {resolvedDescription}
                </CardDescription>
              )}
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            {isSample && <SampleDataBadge locale={resolvedLocale} />}
            {(currency || unit) && (
              <Badge variant="outline" className="font-mono text-xs">
                {currency ?? unit}
              </Badge>
            )}
          </div>
        </div>
      </CardHeader>

      <CardContent className="pt-4">
        {normalizedData.length === 0 ? (
          <p
            role="status"
            data-testid="bar-chart-empty"
            className="py-8 text-center text-xs text-muted-foreground"
          >
            {copy.emptyText}
          </p>
        ) : (
          <NextIntlClientProvider
            locale={canonicalLocale}
            messages={messages[resolvedLocale]}
          >
            <ChartAccessibleTable
              data={normalizedData}
              series={resolvedSeries}
              title={title}
            />
            <div className="w-full overflow-x-auto" data-testid="bar-chart-canvas"
              data-chart-type={chartType}>
              {chartType === 'line' && (
                <LineChart
                  width={560}
                  height={height}
                  data={normalizedData}
                  margin={{ top: 10, right: 16, left: 4, bottom: 8 }}
                >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke={GENERATIVE_CHART_TOKENS.gridStroke}
                  vertical={false}
                />
                <XAxis
                  dataKey="timestamp"
                  stroke={GENERATIVE_CHART_TOKENS.axisStroke}
                  fontSize={11}
                  tickLine={false}
                />
                <YAxis
                  stroke={GENERATIVE_CHART_TOKENS.axisStroke}
                  fontSize={11}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(val: number) =>
                    formatChartValue(Number(val), {
                      currency,
                      unit,
                      locale: resolvedLocale,
                      precision: 0,
                    })
                  }
                />
                <Tooltip
                  formatter={(val: unknown) =>
                    formatChartValue(Number(val), {
                      currency,
                      unit,
                      locale: resolvedLocale,
                    })
                  }
                  contentStyle={{
                    backgroundColor: GENERATIVE_CHART_TOKENS.tooltipBackground,
                    borderColor: GENERATIVE_CHART_TOKENS.tooltipBorder,
                    color: GENERATIVE_CHART_TOKENS.tooltipText,
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                />
                <Legend wrapperStyle={{ fontSize: '12px' }} />
                  {resolvedSeries.map((s, idx) => {
                    const stroke = resolveSeriesColor(idx, s.color);
                    return (
                      <Line
                        key={s.dataKey}
                        type="monotone"
                        dataKey={s.dataKey}
                        name={s.name}
                        stroke={stroke}
                        strokeWidth={2}
                        dot={{ r: 3, fill: stroke }}
                      />
                    );
                  })}
                </LineChart>
              )}
              {chartType === 'scatter' && (
                <ScatterChart width={560} height={height} margin={{ top: 10, right: 16, left: 4, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={GENERATIVE_CHART_TOKENS.gridStroke} />
                  <XAxis
                    type="number"
                    dataKey="x"
                    name={resolvedCategoryKey ?? 'x'}
                    stroke={GENERATIVE_CHART_TOKENS.axisStroke}
                    fontSize={11}
                    tickLine={false}
                  />
                  <YAxis
                    type="number"
                    dataKey="y"
                    stroke={GENERATIVE_CHART_TOKENS.axisStroke}
                    fontSize={11}
                    tickLine={false}
                    axisLine={false}
                  />
                <Tooltip
                  formatter={(val: unknown) =>
                    formatChartValue(Number(val), {
                      currency,
                      unit,
                      locale: resolvedLocale,
                    })
                  }
                  contentStyle={{
                    backgroundColor: GENERATIVE_CHART_TOKENS.tooltipBackground,
                    borderColor: GENERATIVE_CHART_TOKENS.tooltipBorder,
                    color: GENERATIVE_CHART_TOKENS.tooltipText,
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                />
                <Legend wrapperStyle={{ fontSize: '12px' }} />
                  {resolvedSeries.map((s, idx) => (
                    <Scatter
                      key={s.dataKey}
                      name={s.name}
                      data={scatterPoints(normalizedData, s.dataKey)}
                      fill={resolveSeriesColor(idx, s.color)}
                    />
                  ))}
                </ScatterChart>
              )}
              {(chartType === 'bar' || chartType === 'distribution') && (
                <BarChart
                  width={560}
                  height={height}
                  data={normalizedData}
                  // Histogram bins touch each other; grouped bars keep the default category gap.
                  barCategoryGap={chartType === 'distribution' ? 0 : undefined}
                  margin={{ top: 10, right: 16, left: 4, bottom: 8 }}
                >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke={GENERATIVE_CHART_TOKENS.gridStroke}
                  vertical={false}
                />
                <XAxis
                  dataKey="timestamp"
                  stroke={GENERATIVE_CHART_TOKENS.axisStroke}
                  fontSize={11}
                  tickLine={false}
                />
                <YAxis
                  stroke={GENERATIVE_CHART_TOKENS.axisStroke}
                  fontSize={11}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(val: number) =>
                    formatChartValue(Number(val), {
                      currency,
                      unit,
                      locale: resolvedLocale,
                      precision: 0,
                    })
                  }
                />
                <Tooltip
                  formatter={(val: unknown) =>
                    formatChartValue(Number(val), {
                      currency,
                      unit,
                      locale: resolvedLocale,
                    })
                  }
                  contentStyle={{
                    backgroundColor: GENERATIVE_CHART_TOKENS.tooltipBackground,
                    borderColor: GENERATIVE_CHART_TOKENS.tooltipBorder,
                    color: GENERATIVE_CHART_TOKENS.tooltipText,
                    borderRadius: '8px',
                    fontSize: '12px',
                  }}
                />
                <Legend wrapperStyle={{ fontSize: '12px' }} />
                  {resolvedSeries.map((s, idx) => {
                    const fill = resolveSeriesColor(idx, s.color);
                    return (
                      <Bar
                        key={s.dataKey}
                        dataKey={s.dataKey}
                        name={s.name}
                        fill={fill}
                        stroke={chartType === 'distribution' ? GENERATIVE_CHART_TOKENS.tooltipBackground : undefined}
                        radius={chartType === 'distribution' ? 0 : [4, 4, 0, 0]}
                      />
                    );
                  })}
                </BarChart>
              )}
            </div>

            {/* Visible formatted summary chips for non-color-only readability */}
            <ul
              aria-label={`${title} summary`}
              className="mt-3 flex flex-wrap gap-2 border-t border-border pt-3 text-xs"
            >
              {normalizedData.map((row, idx) => {
                const primarySeries = resolvedSeries[0];
                const rawVal = Number(row[primarySeries.dataKey] ?? row.value ?? 0);
                const color = resolveSeriesColor(idx, primarySeries.color);
                return (
                  <li
                    key={`${String(row.timestamp)}-${idx}`}
                    data-testid={`bar-summary-item-${idx}`}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1"
                  >
                    <span
                      className="size-2 rounded-full shrink-0"
                      style={{ backgroundColor: color }}
                      data-color={color}
                      aria-hidden="true"
                    />
                    <span className="text-muted-foreground">{String(row.timestamp)}:</span>
                    <span className="font-mono font-semibold text-foreground">
                      {formatChartValue(rawVal, {
                        currency: primarySeries.currency ?? currency,
                        unit: primarySeries.unit ?? unit,
                        locale: resolvedLocale,
                      })}
                    </span>
                  </li>
                );
              })}
            </ul>
          </NextIntlClientProvider>
        )}
      </CardContent>
      {source && (
        <CardFooter className="border-t border-border py-2.5 text-[11px] text-muted-foreground">
          <span data-testid="bar-chart-source">
            <strong className="font-medium text-foreground">{copy.sourceLabel}:</strong> {source}
          </span>
        </CardFooter>
      )}
    </Card>
    </ExportOverlay>
  );
}
