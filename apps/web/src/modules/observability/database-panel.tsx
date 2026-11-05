'use client';

import { TablePagination, usePagination } from '@/components/ui/pagination';
import { useQuery } from '@tanstack/react-query';
import { useLocale, useTranslations } from 'next-intl';
import { useMemo, type ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { BaseLineChart, type ChartDataPoint } from '@/core/charts/recharts-wrapper';
import { useDisplayPreferences } from '@/core/query-provider';
import { getDatabaseStats, type HistoryBucket, type PgStats, type RedisStats } from './api';
import {
  connectionRatio, connectionTone, deadRatioTone, evictedTone, formatAgo, formatBytes, formatRatio, formatUptime, fragmentationTone, latestDbValue, longestTxTone, pgFinding, saveTone,
} from './database-model';
import { StatusDot } from './status-row';
import { fillBuckets, statusTone, type Tone } from './system-status-model';
import { getDateTimeFormat, getNumberFormat } from '@/modules/shared';

const DASH = <span className="text-muted-foreground" aria-label="—">—</span>;
const TEXT: Record<Tone, string> = { success: '', muted: '', warning: 'text-[var(--status-warning)]', danger: 'text-destructive' };
const FILL: Record<Tone, string> = { success: 'bg-[var(--status-success)]', muted: 'bg-[var(--chart-4)]', warning: 'bg-[var(--status-warning)]', danger: 'bg-[var(--danger)]' };
const NEUTRAL = 'var(--chart-4)';

/** One label/value row of the definition grid; null renders a grey dash and the tone colours only the value. */
function Fact({ label, value, tone = 'muted', children }: { label: string; value: ReactNode | null; tone?: Tone; children?: ReactNode }) {
  return <div className="flex flex-col gap-0.5 py-1.5">
    <dt className="text-xs text-muted-foreground">{label}</dt>
    <dd className={`m-0 text-sm font-medium tabular-nums ${TEXT[tone]}`}>{value ?? DASH}</dd>
    {children}
  </div>;
}

/** Thin usage bar at the true ratio (clamped to the track only for drawing). */
function UsageBar({ ratio, tone, label }: { ratio: number; tone: Tone; label: string }) {
  return <div role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(ratio * 100)} className="h-1 w-full rounded-sm bg-rule">
    <div className={`h-1 rounded-sm ${FILL[tone]}`} style={{ width: `${Math.min(100, ratio * 100)}%` }} />
  </div>;
}

/** Per-database column frame: status dot, name, version, uptime. Children are the figures, skeleton or error. */
function Column({ title, tone, status, meta, children }: { title: string; tone: Tone; status?: string; meta: string[]; children: ReactNode }) {
  return <section className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4" aria-label={title}>
    <h3 className="m-0 flex flex-wrap items-center gap-x-2 text-base font-semibold"><StatusDot tone={tone} />{title}
      {status && <span className="text-sm font-normal text-muted-foreground">{status}</span>}
      {meta.length > 0 && <span className="text-sm font-normal tabular-nums text-muted-foreground">· {meta.join(' · ')}</span>}
    </h3>
    {children}
  </section>;
}

/** Builds chart rows (gaps for missing buckets) from history `db` fields, scaled for display. */
function series(buckets: HistoryBucket[], bucketSeconds: number, pick: (db: NonNullable<HistoryBucket['db']>) => Record<string, number | null | undefined>): ChartDataPoint[] {
  return fillBuckets(buckets, bucketSeconds).map((bucket) => {
    const db = 'db' in bucket ? bucket.db : null;
    const values = db ? pick(db) : {};
    const row: ChartDataPoint = { timestamp: bucket.start, value: null as unknown as number };
    for (const [key, v] of Object.entries(values)) row[key] = v ?? null;
    return row;
  });
}

/** A titled mini trend chart; shows a muted note instead when no bucket carries data. */
function Trend({ title, data, keys, unit, empty }: { title: string; data: ChartDataPoint[]; keys: { dataKey: string; name: string; neutral?: boolean }[]; unit?: string; empty: string }) {
  const hasData = data.some((row) => keys.some((k) => row[k.dataKey] != null));
  return <div className="flex flex-col gap-1">
    <h4 className="m-0 text-xs font-semibold text-muted-foreground">{title}</h4>
    {hasData ? <BaseLineChart height={110} unit={unit} showGrid={false} connectNulls data={data}
      series={keys.map((k) => ({ dataKey: k.dataKey, name: k.name, ...(k.neutral ? { color: NEUTRAL } : {}) }))} /> : <p className="m-0 text-xs text-muted-foreground">{empty}</p>}
  </div>;
}

/** Postgres column: finding sentence, live figures, trends and largest tables. */
// eslint-disable-next-line complexity -- JSX presentational markup: the score counts every conditional-render branch (&&, ?:, ??) of this panel; it holds no branching business logic, so splitting would only relocate the same render conditions
function PostgresColumn({ pg, buckets, bucketSeconds, locale }: { pg: PgStats | null | undefined; buckets: HistoryBucket[]; bucketSeconds: number; locale: string }) {
  const t = useTranslations('observability');
  const num = getNumberFormat(locale, { maximumFractionDigits: 1 });
  const int = getNumberFormat(locale, { maximumFractionDigits: 0 });
  const n = (v: number | null | undefined, f = int) => (v == null ? null : f.format(v));
  const tablePager = usePagination(pg?.tables ?? []);
  const title = t('dbPostgres');
  if (!pg) return <Column title={title} tone="muted" meta={[]}><p className="m-0 text-sm text-muted-foreground">{t('dbUnavailable')}</p></Column>;
  const conn = pg.connections;
  const ratio = connectionRatio(conn);
  const tone = connectionTone(ratio);
  const finding = pgFinding(pg, locale);
  const tps = latestDbValue(buckets, 'pg_tps_avg');
  const meta = [pg.version, formatUptime(pg.uptime_seconds, locale)].filter((v): v is string => !!v);
  const sentence = !finding ? null : finding.kind === 'deadRows'
    ? (finding.ago ? t('dbFindDeadAgo', { table: finding.table, percent: finding.percent, ago: finding.ago }) : t('dbFindDead', { table: finding.table, percent: finding.percent }))
    : finding.kind === 'connections' ? t('dbFindConnections', { total: finding.total, max: finding.max }) : t('dbFindCache', { percent: finding.percent });
  const connData = series(buckets, bucketSeconds, (d) => ({ value: d.pg_connections_avg, max: d.pg_connections_max }));
  const tpsData = series(buckets, bucketSeconds, (d) => ({ value: d.pg_tps_avg }));
  const cacheData = series(buckets, bucketSeconds, (d) => ({ value: d.pg_cache_hit_ratio_avg == null ? null : d.pg_cache_hit_ratio_avg * 100 }));
  const tables = tablePager.pageItems;
  return <Column title={title} tone={statusTone(pg.status)} meta={meta}>
    {sentence && <p className="m-0 text-sm" role="status">{sentence}</p>}
    <dl className="m-0 grid grid-cols-2 gap-x-4">
      <Fact label={t('dbSize')} value={formatBytes(pg.database_size_bytes, locale)} />
      <Fact label={t('dbConnections')} value={conn && conn.total != null ? `${int.format(conn.total)} / ${conn.max == null ? '—' : int.format(conn.max)}` : null} tone={tone === 'success' ? 'muted' : tone}>
        {ratio != null && <UsageBar ratio={ratio} tone={tone} label={t('dbConnections')} />}
        <span className="text-xs text-muted-foreground">{t('dbConnectionsNote')}</span>
      </Fact>
      <Fact label={t('dbCacheHit')} value={formatRatio(pg.cache_hit_ratio, locale)} />
      <Fact label={t('dbIndexHit')} value={formatRatio(pg.index_hit_ratio, locale)} />
      <Fact label={t('dbTps')} value={n(tps, num)} />
      <Fact label={t('dbDeadlocks')} value={n(pg.deadlocks)} />
      <Fact label={t('dbLocksWaiting')} value={n(pg.locks_waiting)} />
      <Fact label={t('dbLongestTx')} value={pg.longest_transaction_seconds == null ? null : formatUptime(pg.longest_transaction_seconds, locale)} tone={longestTxTone(pg.longest_transaction_seconds)} />
      <Fact label={t('dbTempBytes')} value={formatBytes(pg.temp_bytes, locale)} />
    </dl>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 lg:grid-cols-1 xl:grid-cols-3">
      <Trend title={t('dbTrendConnections')} data={connData} empty={t('sysCollecting')} keys={[{ dataKey: 'value', name: t('dbAvg') }, { dataKey: 'max', name: t('dbPeak'), neutral: true }]} />
      <Trend title={t('dbTps')} data={tpsData} empty={t('sysCollecting')} keys={[{ dataKey: 'value', name: t('dbTps') }]} />
      <Trend title={t('dbCacheHit')} data={cacheData} unit="%" empty={t('sysCollecting')} keys={[{ dataKey: 'value', name: t('dbCacheHit') }]} />
    </div>
    <h4 className="m-0 mt-1 text-sm font-semibold">{t('dbTables')}</h4>
    {tablePager.total === 0 ? <p className="m-0 text-sm text-muted-foreground">{t('dbTablesEmpty')}</p> : <><Table>
      <TableHeader><TableRow>
        <TableHead>{t('dbColTable')}</TableHead><TableHead className="text-right">{t('dbColSize')}</TableHead><TableHead className="text-right">{t('dbColLive')}</TableHead>
        <TableHead className="text-right">{t('dbColDead')}</TableHead><TableHead className="text-right">{t('dbColScans')}</TableHead><TableHead className="text-right">{t('dbColVacuum')}</TableHead>
      </TableRow></TableHeader>
      <TableBody>{tables.map((tb) => {
        const dead = deadRatioTone(tb.dead_ratio);
        const ago = formatAgo(tb.last_autovacuum, locale);
        return <TableRow key={tb.name}>
          <TableCell className="font-medium">{tb.name}</TableCell>
          <TableCell className="text-right tabular-nums">{formatBytes(tb.total_bytes, locale) ?? DASH}</TableCell>
          <TableCell className="text-right tabular-nums">{n(tb.live_rows) ?? DASH}</TableCell>
          <TableCell className="text-right tabular-nums">{tb.dead_rows == null ? DASH : <>{int.format(tb.dead_rows)}{tb.dead_ratio != null && <span className={`ml-1 ${TEXT[dead] || 'text-muted-foreground'}`}>({formatRatio(tb.dead_ratio, locale)})</span>}</>}</TableCell>
          <TableCell className="text-right tabular-nums">{tb.seq_scans == null && tb.index_scans == null ? DASH : `${n(tb.seq_scans) ?? '—'} / ${n(tb.index_scans) ?? '—'}`}</TableCell>
          <TableCell className="text-right">{ago ?? DASH}</TableCell>
        </TableRow>;
      })}</TableBody>
    </Table><TablePagination {...tablePager} /></>}
  </Column>;
}

/** Redis column: live figures and memory/ops trends. */
// eslint-disable-next-line complexity -- JSX presentational markup: the score counts every conditional-render branch (&&, ?:, ??) of this panel; it holds no branching business logic, so splitting would only relocate the same render conditions
function RedisColumn({ redis, buckets, bucketSeconds, locale }: { redis: RedisStats | null | undefined; buckets: HistoryBucket[]; bucketSeconds: number; locale: string }) {
  const t = useTranslations('observability');
  const num = getNumberFormat(locale, { maximumFractionDigits: 1 });
  const int = getNumberFormat(locale, { maximumFractionDigits: 0 });
  const title = t('dbRedis');
  if (!redis) return <Column title={title} tone="muted" meta={[]}><p className="m-0 text-sm text-muted-foreground">{t('dbUnavailable')}</p></Column>;
  const meta = [redis.version, formatUptime(redis.uptime_seconds, locale)].filter((v): v is string => !!v);
  const limited = redis.max_memory_bytes != null && redis.max_memory_bytes > 0;
  const memRatio = limited && redis.used_memory_bytes != null ? redis.used_memory_bytes / (redis.max_memory_bytes as number) : null;
  const memTone = connectionTone(memRatio);
  const used = formatBytes(redis.used_memory_bytes, locale);
  const memory = used == null ? null : limited ? `${used} / ${formatBytes(redis.max_memory_bytes, locale)}` : used;
  const memData = series(buckets, bucketSeconds, (d) => ({ value: d.redis_memory_bytes_max == null ? null : d.redis_memory_bytes_max / 1_048_576 }));
  const opsData = series(buckets, bucketSeconds, (d) => ({ value: d.redis_ops_per_sec_avg }));
  return <Column title={title} tone={statusTone(redis.status)} meta={meta}>
    <dl className="m-0 grid grid-cols-2 gap-x-4">
      <Fact label={t('dbMemory')} value={memory} tone={memTone === 'success' ? 'muted' : memTone}>
        {memRatio != null ? <UsageBar ratio={memRatio} tone={memTone} label={t('dbMemory')} /> : used != null && !limited && <span className="text-xs text-muted-foreground">{t('dbUnlimited')}</span>}
      </Fact>
      <Fact label={t('dbFragmentation')} value={redis.fragmentation_ratio == null ? null : num.format(redis.fragmentation_ratio)} tone={fragmentationTone(redis.fragmentation_ratio)} />
      <Fact label={t('dbClients')} value={redis.connected_clients == null ? null : int.format(redis.connected_clients)} />
      <Fact label={t('dbOps')} value={redis.ops_per_sec == null ? null : num.format(redis.ops_per_sec)} />
      <Fact label={t('dbHitRatio')} value={formatRatio(redis.keyspace_hit_ratio, locale)} />
      <Fact label={t('dbEvicted')} value={redis.evicted_keys == null ? null : int.format(redis.evicted_keys)} tone={evictedTone(redis.evicted_keys)} />
      <Fact label={t('dbKeys')} value={redis.keys_total == null ? null : int.format(redis.keys_total)} />
      <Fact label={t('dbLastSave')} value={redis.last_save_status} tone={saveTone(redis.last_save_status)} />
    </dl>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <Trend title={t('dbMemory')} data={memData} unit="MiB" empty={t('sysCollecting')} keys={[{ dataKey: 'value', name: t('dbMemory') }]} />
      <Trend title={t('dbOps')} data={opsData} empty={t('sysCollecting')} keys={[{ dataKey: 'value', name: t('dbOps') }]} />
    </div>
  </Column>;
}

/**
 * Database section: live Postgres and Redis figures refreshed every 15s, plus trend charts from the page's history range.
 * Errors stay inside this section with a Retry; each database degrades independently when the server returns null for it.
 */
export function DatabasePanel({ buckets, bucketSeconds }: { buckets: HistoryBucket[]; bucketSeconds: number }) {
  const t = useTranslations('observability');
  const locale = useLocale() === 'vi-vi' ? 'vi-VN' : 'en-US';
  const display = useDisplayPreferences();
  const stats = useQuery({ queryKey: ['system-database'], queryFn: getDatabaseStats, refetchInterval: 15_000 });
  const updated = useMemo(() => stats.dataUpdatedAt
    ? getDateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: display.timezone }).format(new Date(stats.dataUpdatedAt)) : null, [stats.dataUpdatedAt, locale, display.timezone]);
  return <section className="flex min-w-0 flex-col gap-3" aria-label={t('dbTitle')}>
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="m-0 text-base font-semibold">{t('dbTitle')}</h2>
      {updated && <span className="text-sm tabular-nums text-muted-foreground">{t('dbUpdated', { time: updated })}</span>}
    </div>
    {stats.isPending ? <div className="h-80 w-full animate-pulse rounded-md bg-rule" role="status" aria-label={t('loadingStatus')} />
      : stats.isError ? <div role="alert" className="flex flex-col items-start gap-2 text-sm"><p className="m-0 text-destructive">{t('sysBlockFailed')}</p><Button type="button" variant="outline" onClick={() => void stats.refetch()}>{t('retry')}</Button></div>
        : <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          <PostgresColumn pg={stats.data.postgres} buckets={buckets} bucketSeconds={bucketSeconds} locale={locale} />
          <RedisColumn redis={stats.data.redis} buckets={buckets} bucketSeconds={bucketSeconds} locale={locale} />
        </div>}
  </section>;
}
