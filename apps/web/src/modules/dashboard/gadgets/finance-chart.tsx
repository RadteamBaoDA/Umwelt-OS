'use client';

import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import type { GadgetInstance } from '../api';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { listWorldObservationWindow } from '@/modules/observations/api';
import { MetricsChart, type MetricsChartProps } from './metrics-chart';

/** Props for the FinanceChart gadget component. */
export interface FinanceChartProps extends Omit<MetricsChartProps, 'instance'> {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/**
 * Standard Finance Chart gadget template.
 * Specializes MetricsChart for equities, crypto, commodities, and index series.
 *
 * @param props Gadget instance configuration and chart options.
 * @returns Accessible finance chart component.
 */
export function FinanceChart(props: FinanceChartProps) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const { instance } = props;
  const definition = instance.definition;
  const sourceId = definition.source_ids?.[0] ?? '';
  const symbol = definition.scope?.symbols?.[0] ?? '';
  const metric = definition.scope?.metrics?.[0] ?? 'close';
  const lookbackDays = definition.scope?.lookback_days ?? 90;
  const observations = useQuery({
    queryKey: ['world-observations', 'finance', instance.id, sourceId, symbol, metric, lookbackDays],
    queryFn: ({ signal }) => {
      const to = new Date();
      const from = new Date(to.getTime() - lookbackDays * 86_400_000);
      return listWorldObservationWindow({ sourceIds: [sourceId], symbols: [symbol], metrics: [metric], from, to }, signal);
    },
    enabled: Boolean(sourceId && symbol), staleTime: 60_000,
  });
  const rows = (observations.data?.items ?? [])
    .filter((item) => item.source_id === sourceId && item.symbol === symbol && item.metric === metric && item.value !== null)
    .sort((left, right) => left.observed_at.localeCompare(right.observed_at));
  const selected = rows.at(-1);
  const comparable = rows.every((item) => item.unit === selected?.unit && item.currency === selected?.currency);
  const points = comparable ? rows.flatMap((item) => item.value === null ? [] : [{ timestamp: item.observed_at, value: item.value }]) : [];
  if (!sourceId || !symbol) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('financeSourceSymbolRequired')}</p>;
  if (observations.isPending) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('observationLoading')}</p>;
  if (observations.isError) return <p role="alert" className="p-3 text-sm text-destructive">{t('observationLoadFailed')}</p>;
  if (!comparable) return <p role="status" className="p-3 text-sm text-muted-foreground">{t('observationMixedUnits')}</p>;
  return <>
    {observations.data?.truncated && <p role="status" className="px-3 pt-2 text-xs text-muted-foreground">{t('observationTruncated')}</p>}
    <MetricsChart {...props} initialData={points} currency={selected?.currency ?? undefined} unit={selected?.currency ? undefined : selected?.unit} providerDelay={selected ? `${selected.provider_delay_seconds == null ? t('observationProviderDelayUnknown') : `${selected.provider_delay_seconds}s`} · ${t('observationCollectedAt')} ${formatDateTime(selected.collected_at, display.locale, selected.timezone ?? display.timezone)} · ${selected.timezone ?? 'UTC'} · Alpha Vantage` : undefined} />
  </>;
}
