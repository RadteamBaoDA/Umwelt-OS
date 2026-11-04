'use client';

import { TablePagination, usePagination } from '@/components/ui/pagination';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { RefreshCw } from 'lucide-react';
import { useLocale, useTranslations } from 'next-intl';
import { useMemo, useState, type ReactNode } from 'react';
import { Bar, BarChart, CartesianGrid, Tooltip, XAxis, YAxis } from 'recharts';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { BaseLineChart, RechartsContainer, type ChartDataPoint } from '@/core/charts/recharts-wrapper';
import { useErrorCodeLabel } from '@/core/error-codes';
import { useDisplayPreferences } from '@/core/query-provider';
import { serviceLabelKeys, statusLabel } from '@/modules/settings/status-label';
import { humanizeCode } from '@/modules/sources/source-list';
import { getHistorySettings, getQuality, getQueueSummary, getSystemHealth, getSystemHistory, listRuns, type HistoryBucket, type HistoryRange, type SystemHealth } from './api';
import { DatabasePanel } from './database-panel';
import { HistoryRetention } from './history-retention';
import { StatusRow } from './status-row';
import {
  CORE_SERVICES, OPTIONAL_SERVICES, activityFinding, barTone, bucketRunsByHour, fillBuckets, isFailedRun, queueTotal, statusTone, summarizeHealth, uptimePercent, type Tone,
} from './system-status-model';

const RANGES: HistoryRange[] = ['1h', '24h', '7d', '30d', '90d'];
const BAR: Record<Tone, string> = { success: 'bg-[var(--status-success)]', warning: 'bg-[var(--status-warning)]', danger: 'bg-[var(--danger)]', muted: 'bg-[var(--chart-4)]' };
const DASH = <span className="text-muted-foreground" aria-label="—">—</span>;
const TOOLTIP_STYLE = { background: 'var(--surface)', border: '1px solid var(--line)', borderRadius: 6, fontSize: 12 } as const;

/** Block frame with a heading; children render the data, loading skeleton or in-block error. */
function Block({ title, children }: { title: ReactNode; children: ReactNode }) {
  return <section className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4"><h2 className="m-0 text-base font-semibold">{title}</h2>{children}</section>;
}

/** Error state kept inside the failing block, with a Retry action. */
function BlockError({ message, retry }: { message: string; retry: () => void }) {
  const t = useTranslations('observability');
  return <div role="alert" className="flex flex-col items-start gap-2 text-sm"><p className="m-0 text-destructive">{message}</p><Button type="button" variant="outline" onClick={retry}>{t('retry')}</Button></div>;
}

/** Skeleton at the final size of the content it stands in for. */
function Skeleton({ height }: { height: number }) {
  return <div className="w-full animate-pulse rounded-md bg-rule" style={{ height }} role="status" aria-label="loading" />;
}

/** One uptime bar strip: a thin bar per bucket coloured by that bucket's up/total for the component. */
export function UptimeStrip({ buckets, name }: { buckets: HistoryBucket[]; name: string }) {
  return <div className="flex h-4 w-28 items-stretch gap-px" aria-hidden="true" data-testid={`strip-${name}`}>
    {buckets.map((bucket) => { const tone = barTone(bucket.components[name]); return <span key={bucket.start} data-tone={tone} className={`min-w-px flex-1 rounded-[1px] ${BAR[tone]}`} />; })}
  </div>;
}

/** Operations dashboard: summary, KPIs, latency, service list, activity, queue, recent failures and history retention. */
export function SystemStatusDashboard() {
  const t = useTranslations('observability');
  const ts = useTranslations('uiSweep');
  const locale = useLocale() === 'vi-vi' ? 'vi-VN' : 'en-US';
  const display = useDisplayPreferences();
  const errorLabel = useErrorCodeLabel();
  const [range, setRange] = useState<HistoryRange>('1h');
  const health = useQuery({ queryKey: ['system-health'], queryFn: getSystemHealth, refetchInterval: 30_000 });
  const history = useQuery({ queryKey: ['system-history', range], queryFn: () => getSystemHistory(range) });
  // Shared cache key with the settings row; drives the "collection is off" note.
  const historySettings = useQuery({ queryKey: ['system-history-settings'], queryFn: getHistorySettings });
  const collectionOff = historySettings.data?.sample_interval_minutes === 0;
  const runs = useQuery({ queryKey: ['system-runs-100'], queryFn: () => listRuns() });
  const queue = useQuery({ queryKey: ['system-queue'], queryFn: getQueueSummary });
  const quality = useQuery({ queryKey: ['system-quality'], queryFn: getQuality });
  const buckets = useMemo(() => history.data?.buckets ?? [], [history.data]);
  const bucketSeconds = history.data?.bucket_seconds ?? 0;
  const noSamples = history.isSuccess && buckets.length === 0;
  const num = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  const time = (iso: string) => new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short', timeZone: display.timezone }).format(new Date(iso));
  const clock = (value: string | number) => new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', timeZone: display.timezone }).format(new Date(value));
  const checkedAt = health.dataUpdatedAt ? clock(health.dataUpdatedAt) : null;

  const summary = health.data ? summarizeHealth(health.data) : null;
  const runItems = useMemo(() => runs.data?.items ?? [], [runs.data]);
  const failedRuns = useMemo(() => runItems.filter(isFailedRun), [runItems]);
  const failedPager = usePagination(failedRuns);
  const uptime = uptimePercent(buckets);
  const filled = useMemo(() => fillBuckets(buckets, bucketSeconds), [buckets, bucketSeconds]);
  // Missing slots carry null so Recharts leaves a gap instead of joining across a collection outage.
  const latencyData: ChartDataPoint[] = useMemo(() => filled.map((bucket) => {
    const components = 'components' in bucket ? bucket.components : undefined;
    return { timestamp: bucket.start, value: (components?.postgres?.latency_ms_avg ?? null) as number, redis: components?.redis?.latency_ms_avg ?? null };
  }), [filled]);
  const backlog = useMemo(() => filled.map((bucket) => ({ timestamp: bucket.start, backlog: 'queue_backlog_max' in bucket ? bucket.queue_backlog_max : null })), [filled]);
  const activity = useMemo(() => bucketRunsByHour(runItems, display.timezone), [runItems, display.timezone]);
  const finding = activityFinding(runItems);
  const collecting = <p className="m-0 text-sm text-muted-foreground" role="status">{collectionOff ? t('collectionOff') : t('sysCollecting')}</p>;
  const historyError = <BlockError message={t('sysBlockFailed')} retry={() => void history.refetch()} />;
  const axis = { fontSize: 11, stroke: 'var(--muted)', tickLine: false } as const;

  return <div className="flex flex-col gap-5">
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="m-0 text-2xl font-semibold">{ts('systemTitle')}</h1>
        {summary && <p className="mt-2 mb-0 text-muted-foreground" role="status">
          {summary.coreOk ? t('sysSummaryOk', { healthy: summary.healthy, total: summary.total }) : t('sysSummaryBad', { healthy: summary.healthy, total: summary.total })}
          {summary.unconfigured > 0 && <> · {t('sysUnconfigured', { count: summary.unconfigured })}</>}
        </p>}
      </div>
      <div className="flex items-center gap-3 text-sm text-muted-foreground">
        {checkedAt && <span className="tabular-nums">{t('sysCheckedAt', { time: checkedAt })}</span>}
        <Button type="button" variant="outline" aria-label={t('sysRefresh')} onClick={() => { void health.refetch(); void history.refetch(); void runs.refetch(); void queue.refetch(); void quality.refetch(); }}><RefreshCw aria-hidden="true" className="size-4" /></Button>
      </div>
    </header>

    <div className="flex flex-wrap items-center gap-3">
      <Select value={range} onValueChange={(value) => setRange(value as HistoryRange)}>
        <SelectTrigger className="w-40" aria-label={t('sysRange')}><SelectValue /></SelectTrigger>
        <SelectContent>{RANGES.map((r) => <SelectItem key={r} value={r}>{t(`r${r}`)}</SelectItem>)}</SelectContent>
      </Select>
      {collectionOff && <p className="m-0 text-sm text-muted-foreground" role="status">{t('collectionOff')}</p>}
    </div>

    <dl className="m-0 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border lg:grid-cols-4">
      <Kpi label={t('kpiUptime')} value={uptime === null ? null : `${num.format(uptime)}%`} />
      <Kpi label={t('kpiFailedRuns')} note={t('kpiFailedRunsNote')} value={runs.isSuccess ? `${failedRuns.length}/${runItems.length}` : null} danger={failedRuns.length > 0} />
      <Kpi label={t('kpiQueue')} value={queue.data ? num.format(queueTotal(queue.data)) : null} />
      <Kpi label={t('kpiStale')} value={quality.data ? num.format(quality.data.stale_sources) : null} />
      {quality.data?.graph_sync_lag_seconds != null && <Kpi label={t('kpiGraphLag')} value={t('seconds', { value: num.format(quality.data.graph_sync_lag_seconds) })} />}
    </dl>

    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <Block title={t('latencyTitle')}>
        {history.isPending ? <Skeleton height={220} /> : history.isError ? historyError : noSamples ? collecting :
          <BaseLineChart height={220} connectNulls unit={t('latencyUnit')} data={latencyData} series={[{ dataKey: 'value', name: t('latencyDb') }, { dataKey: 'redis', name: t('latencyRedis'), color: 'var(--chart-3)' }]} />}
      </Block>
      <Block title={t('servicesTitle')}>
        {health.isPending ? <Skeleton height={320} /> : health.isError ? <BlockError message={t('sysHealthFailed')} retry={() => void health.refetch()} /> : <>
          <ServiceGroup title={t('coreGroup')} keys={CORE_SERVICES} health={health.data} buckets={buckets} />
          <ServiceGroup title={t('optionalGroup')} keys={OPTIONAL_SERVICES} health={health.data} buckets={buckets} />
        </>}
      </Block>
    </div>

    <DatabasePanel buckets={buckets} bucketSeconds={bucketSeconds} />

    <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
      <Block title={t('activityTitle')}>
        {runs.isPending ? <Skeleton height={200} /> : runs.isError ? <BlockError message={t('sysBlockFailed')} retry={() => void runs.refetch()} /> : <>
          <p className="m-0 text-sm">
            {finding.kind === 'none' ? t('activityNone') : finding.kind === 'clean' ? t('activityClean', { total: finding.total })
              : t('activityFailures', { failed: finding.failed, total: finding.total, kind: finding.topKind, count: finding.topCount })}
          </p>
          <RechartsContainer height={200}>
            <BarChart data={activity} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--line)" vertical={false} />
              <XAxis dataKey="timestamp" {...axis} tickFormatter={(v: string) => clock(v)} />
              <YAxis {...axis} width={32} allowDecimals={false} axisLine={false} />
              <Tooltip labelFormatter={(v) => time(String(v))} contentStyle={TOOLTIP_STYLE} />
              <Bar dataKey="ok" name={t('runsOk')} stackId="runs" fill="var(--chart-3)" isAnimationActive={false} />
              <Bar dataKey="failed" name={t('runsFailed')} stackId="runs" fill="var(--danger)" isAnimationActive={false} />
            </BarChart>
          </RechartsContainer>
        </>}
      </Block>
      <Block title={t('queueTitle')}>
        {history.isPending ? <Skeleton height={100} /> : history.isError ? historyError : noSamples ? collecting : <>
          <p className="m-0 text-sm text-muted-foreground">{t('queueBacklog')}</p>
          <RechartsContainer height={100} minHeight={100}>
            <BarChart data={backlog} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <XAxis dataKey="timestamp" hide />
              <YAxis {...axis} width={32} allowDecimals={false} axisLine={false} />
              <Tooltip labelFormatter={(v) => time(String(v))} contentStyle={TOOLTIP_STYLE} />
              <Bar dataKey="backlog" name={t('kpiQueue')} fill="var(--chart-1)" isAnimationActive={false} />
            </BarChart>
          </RechartsContainer>
        </>}
        <QueueStages data={queue.data?.ingestion_stages} pending={queue.isPending} failed={queue.isError} retry={() => void queue.refetch()} />
      </Block>
    </div>

    <Block title={t('failuresTitle')}>
      {runs.isPending ? <Skeleton height={120} /> : runs.isError ? <BlockError message={t('sysBlockFailed')} retry={() => void runs.refetch()} /> :
        failedRuns.length === 0 ? <p className="m-0 text-sm text-muted-foreground">{t('failuresEmpty')}</p> :
          <><Table>
            <TableHeader><TableRow><TableHead>{t('colTime')}</TableHead><TableHead>{t('colKind')}</TableHead><TableHead>{t('colError')}</TableHead><TableHead className="text-right">{t('colDuration')}</TableHead></TableRow></TableHeader>
            <TableBody>{failedPager.pageItems.map((run) => <TableRow key={`${run.kind}:${run.id}`}>
              <TableCell className="tabular-nums">{time(run.created_at)}</TableCell>
              <TableCell>{t.has(run.kind) ? t(run.kind) : humanizeCode(run.kind)}</TableCell>
              <TableCell className="text-destructive">{run.error_code ? errorLabel(run.error_code) : DASH}</TableCell>
              <TableCell className="text-right tabular-nums">{run.duration_ms == null ? DASH : `${Math.round(run.duration_ms)} ms`}</TableCell>
            </TableRow>)}</TableBody>
          </Table><TablePagination {...failedPager} /></>}
    </Block>

    <HistoryRetention oldestSampleAt={history.data?.oldest_sample_at ?? null} />
  </div>;
}

/** Plain-type KPI cell: label, tabular figure (grey dash when unknown) and an optional scope note. */
function Kpi({ label, value, note, danger }: { label: string; value: string | null; note?: string; danger?: boolean }) {
  return <div className="flex flex-col gap-1 bg-background p-4">
    <dt className="text-sm text-muted-foreground">{label}</dt>
    <dd className={`m-0 text-2xl font-semibold tabular-nums ${danger ? 'text-destructive' : ''}`}>{value ?? DASH}</dd>
    {note && <dd className="m-0 text-xs text-muted-foreground">{note}</dd>}
  </div>;
}

/** A titled group of service rows with status dot, label, uptime strip and the one relevant action. */
function ServiceGroup({ title, keys, health, buckets }: { title: string; keys: readonly string[]; health: SystemHealth; buckets: HistoryBucket[] }) {
  const t = useTranslations('observability');
  const tg = useTranslations('globalSettings');
  const ts = useTranslations('uiSweep');
  const locale = useLocale() === 'vi-vi' ? 'vi-VN' : 'en-US';
  return <div>
    <h3 className="m-0 mb-1 text-sm font-semibold text-muted-foreground">{title}</h3>
    <ul className="m-0 list-none p-0">{keys.filter((key) => health.components[key]).map((key) => {
      const component = health.components[key];
      const pct = uptimePercent(buckets, key);
      const notTested = component.status === 'configured' && component.connectivity === 'not_tested';
      const action = key === 'n8n' && component.status !== 'configured' ? { href: '/settings/n8n', label: t('actionConfigure') }
        : key === 'model_gateway' && notTested ? { href: '/settings/models', label: t('actionTest') } : null;
      return <StatusRow key={key} tone={statusTone(component.status, component.connectivity)} name={ts(serviceLabelKeys[key] as never)}
        status={notTested ? `${statusLabel(tg, component.status)} · ${ts('connectivityNotTested')}` : statusLabel(tg, component.status)}>
        {buckets.length > 0 && <span className="ml-auto flex items-center gap-2"><UptimeStrip buckets={buckets} name={key} />
          <span className="w-12 text-right text-xs tabular-nums text-muted-foreground">{pct === null ? DASH : `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(pct)}%`}</span></span>}
        {action && <Link href={action.href} className="text-sm underline underline-offset-4">{action.label}</Link>}
      </StatusRow>;
    })}</ul>
  </div>;
}

/** Current per-stage waiting counts as horizontal bars scaled to the largest stage. */
function QueueStages({ data, pending, failed, retry }: { data?: Record<string, number>; pending: boolean; failed: boolean; retry: () => void }) {
  const t = useTranslations('observability');
  if (pending) return <Skeleton height={60} />;
  if (failed) return <BlockError message={t('sysBlockFailed')} retry={retry} />;
  const entries = Object.entries(data ?? {}).filter(([, count]) => count > 0);
  const max = Math.max(1, ...entries.map(([, count]) => count));
  return <div className="flex flex-col gap-2 border-t border-border pt-3">
    <h3 className="m-0 text-sm font-semibold text-muted-foreground">{t('queueNow')}</h3>
    {entries.length === 0 ? <p className="m-0 text-sm text-muted-foreground">{t('queueEmpty')}</p> : entries.map(([stage, count]) => <div key={stage} className="flex items-center gap-3 text-sm">
      <span className="w-28 shrink-0 truncate">{humanizeCode(stage)}</span>
      <span className="h-2 flex-1 rounded-sm bg-rule"><span className="block h-2 rounded-sm bg-[var(--chart-1)]" style={{ width: `${(count / max) * 100}%` }} /></span>
      <span className="w-10 text-right tabular-nums">{count}</span>
    </div>)}
  </div>;
}
