'use client';

import { TablePagination, usePagination } from '@/components/ui/pagination';
import { Table, TableBody, TableCaption, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { Button } from '@/components/ui/button';
import { getMetrics } from './api';

/** Presents process-local telemetry while labeling absent usage and process data as unknown. */
export function UsagePanel() {
  const t = useTranslations('observability');
  const display = useDisplayPreferences();
  const metrics = useQuery({ queryKey: ['observability', 'metrics'], queryFn: getMetrics, refetchInterval: 15_000 });
  const callPager = usePagination(metrics.data?.counters.filter((row) => row.name === 'model_calls_total') ?? []);
  if (metrics.isPending) return <section aria-labelledby="usage-heading"><h3 id="usage-heading" className="text-lg font-semibold">{t('usage')}</h3><p className="text-muted-foreground" role="status">{t('loading')}</p></section>;
  if (metrics.isError) return <section aria-labelledby="usage-heading"><h3 id="usage-heading" className="text-lg font-semibold">{t('usage')}</h3><div role="alert"><p>{t('metricsUnavailable')}</p><Button type="button" variant="outline" className="mt-2" onClick={() => void metrics.refetch()}>{t('retry')}</Button></div></section>;
  const modelCalls = metrics.data.counters.filter((row) => row.name === 'model_calls_total');
  const usageUnknown = metrics.data.processes.length
    ? metrics.data.counters.filter((row) => row.name === 'model_usage_unknown_total').reduce((sum, row) => sum + row.value, 0)
    : null;
  /** Sum one named counter across process snapshots, showing unknown when no series was reported. */
  const tokenTotal = (name: string) => {
    const values = metrics.data.counters.filter((row) => row.name === name);
    return values.length ? values.reduce((sum, row) => sum + row.value, 0).toLocaleString(normalizeFormattingLocale(display.locale)) : t('unknown');
  };
  const latency = metrics.data.histograms.filter((row) => row.name === 'model_call_ms');
  return <section className="space-y-3" aria-labelledby="usage-heading">
    <h3 id="usage-heading" className="text-lg font-semibold">{t('usage')}</h3>
    <p className="text-muted-foreground">{t('processSnapshots', { count: metrics.data.processes.length })} · {usageUnknown === null ? t('unknown') : t('unknownUsageCalls', { count: usageUnknown })}</p>
    <dl className="grid gap-3 sm:grid-cols-3"><Metric label={t('inputTokens')} value={tokenTotal('model_tokens_in_total')} /><Metric label={t('outputTokens')} value={tokenTotal('model_tokens_out_total')} /><Metric label={t('estimatedCost')} value={t('unknown')} /></dl>
    {modelCalls.length ? <div><Table><TableCaption className="sr-only">{t('modelUsage')}</TableCaption><TableHeader><TableRow><TableHead scope="col" className="text-left">{t('model')}</TableHead><TableHead scope="col" className="text-right">{t('calls')}</TableHead><TableHead scope="col" className="text-right">{t('latency')}</TableHead></TableRow></TableHeader><TableBody>
      {callPager.pageItems.map((row) => {
        const capability = row.labels.capability ?? t('unknown');
        const role = row.labels.role ?? t('unknown');
        const matching = latency.find((item) => item.labels.capability === row.labels.capability && item.labels.role === row.labels.role && item.labels.outcome === row.labels.outcome);
        return <tr key={`${capability}:${role}:${row.labels.outcome ?? 'all'}`} className="border-t border-border"><th scope="row" className="py-2 text-left font-normal">{capability} · {role} · {row.labels.outcome ?? t('all')}</th><td className="text-right tabular-nums">{row.value}</td><td className="text-right tabular-nums">{matching?.p95_ms == null ? t('unknown') : `${matching.p95_ms} ms`}</td></tr>;
      })}
    </TableBody></Table><TablePagination {...callPager} /></div> : <p className="text-muted-foreground">{t('noUsage')}</p>}
    <p className="text-muted-foreground">{t('usageEstimateCaveat')}</p>
  </section>;
}

/** Render a formatted usage label and value without inferring or estimating missing data. */
function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-md border border-border p-3"><dt className="text-muted-foreground text-sm">{label}</dt><dd className="mt-1 text-xl font-semibold">{value}</dd></div>;
}
