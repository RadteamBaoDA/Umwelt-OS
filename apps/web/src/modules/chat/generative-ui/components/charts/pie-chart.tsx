'use client';

import * as React from 'react';
import { NextIntlClientProvider } from 'next-intl';
import { Cell, Legend, Pie, PieChart, Tooltip } from 'recharts';
import { PieChartIcon } from 'lucide-react';
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
import { genUiCopy, resolveWidgetLocale } from '../../copy';
import { GENERATIVE_CHART_TOKENS, resolveSeriesColor } from './config';

/** Single slice datum accepted by GenerativePieChart. */
export interface GenerativePieDatum {
  /** Slice category label. */
  label?: string;
  /** Alternative slice name field. */
  name?: string;
  /** Numeric value of the slice. */
  value: number;
  /** Optional semantic CSS color variable override (e.g. 'var(--chart-2)'). */
  color?: string;
  /** Backend `color_token` alias. */
  color_token?: string;
}

/** Props accepted by the native structured GenerativePieChart component. */
export interface GenerativePieChartProps {
  /** Chart headline title. */
  title: string;
  /** Optional subtitle or context description. */
  description?: string;
  /** Backend `ChartPayload.subtitle` alias. */
  subtitle?: string;
  /** Slice data points. */
  data: GenerativePieDatum[];
  /** Optional ISO currency code for slice values. */
  currency?: string;
  /** Optional measurement unit for slice values. */
  unit?: string;
  /** Optional provenance source label. */
  source?: string;
  /** Application locale ('en-us' | 'vi-vi') for number/currency formatting. */
  locale?: AppLocaleId;
  /** Chart viewport height in pixels (defaults to 240). */
  height?: number;
}

/**
 * Native structured GenerativePieChart widget using Recharts (`PieChart`, `Pie`, `Cell`,
 * `Tooltip`, `Legend`) with semantic `--chart-1..5` colors and a screen-reader accessible
 * summary table (`ChartAccessibleTable`).
 *
 * @param props - GenerativePieChartProps configuration.
 * @returns Accessible, theme-aware pie chart card.
 */
export function GenerativePieChart({
  title,
  description,
  subtitle,
  data,
  currency,
  unit,
  locale,
  height = 240,
}: GenerativePieChartProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const canonicalLocale = normalizeFormattingLocale(resolvedLocale);
  const copy = { emptyText: genUiCopy(resolvedLocale).genUiPieEmpty, totalLabel: genUiCopy(resolvedLocale).genUiPieTotal };
  const resolvedDescription = description ?? subtitle;

  const normalizedSlices = React.useMemo(
    () =>
      (data ?? []).map((item, idx) => ({
        name: String(item.label ?? item.name ?? `#${idx + 1}`),
        value: typeof item.value === 'number' && !Number.isNaN(item.value) ? item.value : 0,
        color: resolveSeriesColor(idx, item.color ?? item.color_token),
      })),
    [data],
  );

  const totalValue = React.useMemo(
    () => normalizedSlices.reduce((sum, slice) => sum + slice.value, 0),
    [normalizedSlices],
  );

  const accessibleData = React.useMemo<ChartDataPoint[]>(
    () =>
      normalizedSlices.map((slice) => ({
        timestamp: slice.name,
        value: slice.value,
      })),
    [normalizedSlices],
  );

  const accessibleSeries = React.useMemo<ChartSeries[]>(
    () => [
      {
        dataKey: 'value',
        name: title || 'Share',
        currency,
        unit,
      },
    ],
    [currency, title, unit],
  );

  return (
    <Card
      data-testid="generative-pie-chart"
      className="my-2 w-full max-w-2xl border-border bg-surface text-foreground shadow-sm"
    >
      <CardHeader className="gap-1.5 border-b border-border pb-4">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <PieChartIcon className="size-4" aria-hidden="true" />
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
          {normalizedSlices.length > 0 && (
            <Badge variant="secondary" className="font-mono text-xs">
              {copy.totalLabel}:{' '}
              {formatChartValue(totalValue, {
                currency,
                unit,
                locale: resolvedLocale,
              })}
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="pt-4">
        {normalizedSlices.length === 0 ? (
          <p
            role="status"
            data-testid="pie-chart-empty"
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
              data={accessibleData}
              series={accessibleSeries}
              title={title}
            />
            <div className="flex flex-col items-center justify-center overflow-x-auto" data-testid="pie-chart-canvas">
              <PieChart width={420} height={height}>
                <Pie
                  data={normalizedSlices}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  innerRadius={48}
                  outerRadius={82}
                  paddingAngle={2}
                  isAnimationActive={false}
                >
                  {normalizedSlices.map((slice, idx) => (
                    <Cell
                      key={`${slice.name}-${idx}`}
                      fill={slice.color}
                      stroke="var(--surface)"
                      strokeWidth={2}
                    />
                  ))}
                </Pie>
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
              </PieChart>
            </div>

            {/* Accessible non-color-only breakdown list with semantic color indicators */}
            <ul
              aria-label={`${title} breakdown`}
              className="mt-3 grid grid-cols-1 gap-2 border-t border-border pt-3 text-xs sm:grid-cols-2"
            >
              {normalizedSlices.map((slice, idx) => {
                const sharePercent =
                  totalValue > 0 ? Math.round((slice.value / totalValue) * 1000) / 10 : 0;
                return (
                  <li
                    key={`${slice.name}-${idx}`}
                    data-testid={`pie-slice-item-${idx}`}
                    className="flex items-center justify-between gap-2 rounded-md border border-border bg-background px-3 py-1.5"
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      <span
                        className="size-2.5 rounded-full shrink-0"
                        style={{ backgroundColor: slice.color }}
                        data-color={slice.color}
                        aria-hidden="true"
                      />
                      <span className="truncate font-medium text-foreground">{slice.name}</span>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5 font-mono">
                      <span className="font-semibold text-foreground">
                        {formatChartValue(slice.value, {
                          currency,
                          unit,
                          locale: resolvedLocale,
                        })}
                      </span>
                      <span className="text-muted-foreground">({sharePercent}%)</span>
                    </div>
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

