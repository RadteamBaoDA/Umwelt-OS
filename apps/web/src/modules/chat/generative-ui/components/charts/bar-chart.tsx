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

/** Values every chart variant needs to format axes, tooltips and series. */
interface CartesianContext {
  data: ChartDataPoint[];
  series: ChartSeries[];
  height: number;
  currency?: string;
  unit?: string;
  locale: AppLocaleId;
  categoryKey?: string;
}

const CHART_MARGIN = { top: 10, right: 16, left: 4, bottom: 8 } as const;

/** Tooltip element with the themed surface and locale-aware value formatting. Plain element factory: Recharts must see its children directly. */
function themedTooltip({ currency, unit, locale }: CartesianContext) {
  return (
    <Tooltip
      key="tooltip"
      formatter={(val: unknown) => formatChartValue(Number(val), { currency, unit, locale })}
      contentStyle={{
        backgroundColor: GENERATIVE_CHART_TOKENS.tooltipBackground,
        borderColor: GENERATIVE_CHART_TOKENS.tooltipBorder,
        color: GENERATIVE_CHART_TOKENS.tooltipText,
        borderRadius: '8px',
        fontSize: '12px',
      }}
    />
  );
}

/**
 * Grid, category X axis, formatted Y axis, tooltip and legend shared by the line and bar plots.
 * Returned as plain elements (not wrapper components): Recharts discovers its children by type.
 */
function categoricalFurniture(ctx: CartesianContext) {
  const { currency, unit, locale } = ctx;
  return [
    <CartesianGrid key="grid" strokeDasharray="3 3" stroke={GENERATIVE_CHART_TOKENS.gridStroke} vertical={false} />,
    <XAxis key="x" dataKey="timestamp" stroke={GENERATIVE_CHART_TOKENS.axisStroke} fontSize={11} tickLine={false} />,
    <YAxis
      key="y"
      stroke={GENERATIVE_CHART_TOKENS.axisStroke}
      fontSize={11}
      tickLine={false}
      axisLine={false}
      tickFormatter={(val: number) => formatChartValue(Number(val), { currency, unit, locale, precision: 0 })}
    />,
    themedTooltip(ctx),
    <Legend key="legend" wrapperStyle={{ fontSize: '12px' }} />,
  ];
}

/** Line plot: one monotone line per series. */
function renderLinePlot(ctx: CartesianContext) {
  return (
    <LineChart width={560} height={ctx.height} data={ctx.data} margin={CHART_MARGIN}>
      {categoricalFurniture(ctx)}
      {ctx.series.map((s, idx) => {
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
  );
}

/** Scatter plot: numeric X (category label when numeric, else row index) against each series value. */
function renderScatterPlot(ctx: CartesianContext) {
  return (
    <ScatterChart width={560} height={ctx.height} margin={CHART_MARGIN}>
      <CartesianGrid strokeDasharray="3 3" stroke={GENERATIVE_CHART_TOKENS.gridStroke} />
      <XAxis
        type="number"
        dataKey="x"
        name={ctx.categoryKey ?? 'x'}
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
      {themedTooltip(ctx)}
      <Legend wrapperStyle={{ fontSize: '12px' }} />
      {ctx.series.map((s, idx) => (
        <Scatter
          key={s.dataKey}
          name={s.name}
          data={scatterPoints(ctx.data, s.dataKey)}
          fill={resolveSeriesColor(idx, s.color)}
        />
      ))}
    </ScatterChart>
  );
}

/** Grouped bars (default) or gapless histogram bars (`distribution`). */
function renderBarPlot(ctx: CartesianContext, chartType: 'bar' | 'distribution') {
  const distribution = chartType === 'distribution';
  return (
    <BarChart
      width={560}
      height={ctx.height}
      data={ctx.data}
      // Histogram bins touch each other; grouped bars keep the default category gap.
      barCategoryGap={distribution ? 0 : undefined}
      margin={CHART_MARGIN}
    >
      {categoricalFurniture(ctx)}
      {ctx.series.map((s, idx) => {
        const fill = resolveSeriesColor(idx, s.color);
        return (
          <Bar
            key={s.dataKey}
            dataKey={s.dataKey}
            name={s.name}
            fill={fill}
            stroke={distribution ? GENERATIVE_CHART_TOKENS.tooltipBackground : undefined}
            radius={distribution ? 0 : [4, 4, 0, 0]}
          />
        );
      })}
    </BarChart>
  );
}

/** Picks the plot for the persisted chart type. */
function renderPlot(ctx: CartesianContext, chartType: GenerativeCartesianChartType) {
  if (chartType === 'line') return renderLinePlot(ctx);
  if (chartType === 'scatter') return renderScatterPlot(ctx);
  return renderBarPlot(ctx, chartType);
}

/** Visible formatted summary chips for non-color-only readability. */
function SummaryChips({ title, ctx }: { title: string; ctx: CartesianContext }) {
  const { data, series, currency, unit, locale } = ctx;
  return (
    <ul
      aria-label={`${title} summary`}
      className="mt-3 flex flex-wrap gap-2 border-t border-border pt-3 text-xs"
    >
      {data.map((row, idx) => {
        const primarySeries = series[0];
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
            <span className="tabular-nums font-semibold text-foreground">
              {formatChartValue(rawVal, {
                currency: primarySeries.currency ?? currency,
                unit: primarySeries.unit ?? unit,
                locale,
              })}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Card header: icon, title, description and the sample / currency badges. */
function BarChartHeader({ title, description, isSample, currency, unit, locale }: {
  title: string;
  description?: string;
  isSample?: boolean;
  currency?: string;
  unit?: string;
  locale: AppLocaleId;
}) {
  return (
    <CardHeader className="gap-1.5 border-b border-border pb-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <BarChart3Icon className="size-4" aria-hidden="true" />
          </span>
          <div>
            <CardTitle className="text-base font-semibold text-foreground">{title}</CardTitle>
            {description && (
              <CardDescription className="mt-0.5 text-xs text-muted-foreground">
                {description}
              </CardDescription>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {isSample && <SampleDataBadge locale={locale} />}
          {(currency || unit) && (
            <Badge variant="outline" className="tabular-nums text-xs">
              {currency ?? unit}
            </Badge>
          )}
        </div>
      </div>
    </CardHeader>
  );
}

/**
 * Standalone export (inline SVG + data table, no remote origins) for copy/download.
 *
 * @returns Memoized HTML document string for the current data and series.
 */
function useBarExportHtml(args: {
  title: string;
  description?: string;
  source?: string;
  sourceLabel: string;
  locale: AppLocaleId;
  chartType: GenerativeCartesianChartType;
  ctx: CartesianContext;
}): string {
  const { title, description, source, sourceLabel, locale, chartType, ctx } = args;
  const { series, data, currency, unit } = ctx;
  return React.useMemo(
    () =>
      chartToStandaloneHtml({
        title,
        description,
        source,
        sourceLabel,
        lang: locale === 'vi-vi' ? 'vi' : 'en',
        kind: chartType === 'bar' ? 'bar' : chartType,
        series: series.map((s) => ({ name: s.name, color: s.color ?? '' })),
        rows: data.map((row) => ({
          label: String(row.timestamp),
          values: series.map((s) => {
            const v = Number(row[s.dataKey] ?? row.value ?? 0);
            return Number.isFinite(v) ? v : 0;
          }),
        })),
        format: (v) => formatChartValue(v, { currency, unit, locale }),
      }),
    [title, description, source, sourceLabel, locale, chartType, series, data, currency, unit],
  );
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
  const catalog = genUiCopy(resolvedLocale);
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
  const ctx: CartesianContext = {
    data: normalizedData, series: resolvedSeries, height, currency, unit, locale: resolvedLocale, categoryKey: resolvedCategoryKey,
  };
  const exportHtml = useBarExportHtml({
    title, description: resolvedDescription, source, sourceLabel: catalog.genUiTableSource, locale: resolvedLocale, chartType, ctx,
  });

  return (
    <ExportOverlay title={title} html={exportHtml} ready={normalizedData.length > 0}>
    <Card
      data-testid="generative-bar-chart"
      className="my-2 w-full max-w-2xl border-border bg-surface text-foreground"
    >
      <BarChartHeader title={title} description={resolvedDescription} isSample={isSample} currency={currency} unit={unit} locale={resolvedLocale} />

      <CardContent className="pt-4">
        {normalizedData.length === 0 ? (
          <p
            role="status"
            data-testid="bar-chart-empty"
            className="py-8 text-center text-xs text-muted-foreground"
          >
            {catalog.genUiBarEmpty}
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
              {renderPlot(ctx, chartType)}
            </div>

            <SummaryChips title={title} ctx={ctx} />
          </NextIntlClientProvider>
        )}
      </CardContent>
      {source && (
        <CardFooter className="border-t border-border py-2.5 text-[11px] text-muted-foreground">
          <span data-testid="bar-chart-source">
            <strong className="font-medium text-foreground">{catalog.genUiTableSource}:</strong> {source}
          </span>
        </CardFooter>
      )}
    </Card>
    </ExportOverlay>
  );
}
