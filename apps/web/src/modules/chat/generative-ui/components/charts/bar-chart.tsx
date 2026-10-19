'use client';

import * as React from 'react';
import { NextIntlClientProvider } from 'next-intl';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
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
import { resolveWidgetLocale } from '../plan-card';
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
  /** Application locale ('en-us' | 'vi-vi') for number/currency formatting. */
  locale?: AppLocaleId;
  /** Chart viewport height in pixels (defaults to 240). */
  height?: number;
}

/** Bilingual empty state copy for GenerativeBarChart. */
const BAR_CHART_COPY: Record<AppLocaleId, { emptyText: string }> = {
  'en-us': { emptyText: 'No chart data available to display.' },
  'vi-vi': { emptyText: 'Không có dữ liệu biểu đồ để hiển thị.' },
};

/**
 * Normalizes flexible GenerativeBarDatum items into canonical ChartDataPoint records
 * required by Recharts and ChartAccessibleTable.
 *
 * @param data - Raw bar chart input array.
 * @param categoryKey - Optional property name holding the category label.
 * @returns Normalized ChartDataPoint array.
 */
function normalizeBarData(data: GenerativeBarDatum[], categoryKey?: string): ChartDataPoint[] {
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
  locale,
  height = 240,
}: GenerativeBarChartProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const canonicalLocale = normalizeFormattingLocale(resolvedLocale);
  const copy = BAR_CHART_COPY[resolvedLocale];
  const resolvedDescription = description ?? subtitle;
  const resolvedCategoryKey = categoryKey ?? x_key;

  const normalizedData = React.useMemo(
    () => normalizeBarData(data, resolvedCategoryKey),
    [data, resolvedCategoryKey],
  );
  const resolvedSeries = React.useMemo<ChartSeries[]>(() => {
    if (series && series.length > 0) {
      return series.map((s, idx) => {
        const raw = s as GenerativeChartSeriesInput;
        const dataKey = raw.dataKey || raw.key || 'value';
        const name = raw.name || raw.label || title || 'Value';
        const customColor = raw.color ?? raw.color_token;
        return {
          dataKey,
          name,
          color: resolveSeriesColor(idx, customColor),
          currency: raw.currency ?? currency,
          unit: raw.unit ?? unit,
        };
      });
    }
    return [
      {
        dataKey: 'value',
        name: title || 'Value',
        color: resolveSeriesColor(0),
        currency,
        unit,
      },
    ];
  }, [currency, series, title, unit]);

  return (
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
          {(currency || unit) && (
            <Badge variant="outline" className="font-mono text-xs">
              {currency ?? unit}
            </Badge>
          )}
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
            <div className="w-full overflow-x-auto" data-testid="bar-chart-canvas">
              <BarChart
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
                  const fill = resolveSeriesColor(idx, s.color);
                  return (
                    <Bar
                      key={s.dataKey}
                      dataKey={s.dataKey}
                      name={s.name}
                      fill={fill}
                      radius={[4, 4, 0, 0]}
                    />
                  );
                })}
              </BarChart>
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
    </Card>
  );
}
