import type { HistoryBucket, PgStats, PgTable } from './api';
import type { Tone } from './system-status-model';

/** Formats a byte count with binary units (B..TiB) in the locale; null stays null so callers render a dash. */
export function formatBytes(bytes: number | null | undefined, locale: string): string | null {
  if (bytes == null || !Number.isFinite(bytes)) return null;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = Math.max(0, bytes);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: i === 0 ? 0 : 1 }).format(value)} ${units[i]}`;
}

/** Formats a 0..1 ratio as a percentage with at most one decimal; null stays null. */
export function formatRatio(ratio: number | null | undefined, locale: string): string | null {
  if (ratio == null || !Number.isFinite(ratio)) return null;
  return new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 }).format(ratio);
}

/** Humanizes a duration in seconds as its two largest units (e.g. "3d 4h"), locale-aware; null stays null. */
export function formatUptime(seconds: number | null | undefined, locale: string): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  const unit = (value: number, name: string) => new Intl.NumberFormat(locale, { style: 'unit', unit: name, unitDisplay: 'narrow', maximumFractionDigits: 0 }).format(value);
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${unit(d, 'day')} ${unit(h, 'hour')}`;
  if (h > 0) return `${unit(h, 'hour')} ${unit(m, 'minute')}`;
  if (m > 0) return unit(m, 'minute');
  return unit(s, 'second');
}

/** Relative time ("3 days ago") from an ISO timestamp in the largest fitting unit; null when absent or invalid. */
export function formatAgo(iso: string | null | undefined, locale: string, now: number = Date.now()): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const diff = Math.round((then - now) / 1000);
  const abs = Math.abs(diff);
  const [value, unit] = abs >= 86400 ? ([Math.round(diff / 86400), 'day'] as const) : abs >= 3600 ? ([Math.round(diff / 3600), 'hour'] as const) : ([Math.round(diff / 60), 'minute'] as const);
  return new Intl.RelativeTimeFormat(locale, { numeric: 'auto' }).format(value, unit);
}

/** Fraction of connection slots in use; null when either figure is unknown or max is not positive. */
export function connectionRatio(conn: PgStats['connections'] | undefined): number | null {
  if (!conn || conn.total == null || conn.max == null || conn.max <= 0) return null;
  return conn.total / conn.max;
}

/** Connection usage tone: warning at 80%, danger at 95%. */
export function connectionTone(ratio: number | null): Tone {
  if (ratio == null) return 'muted';
  return ratio >= 0.95 ? 'danger' : ratio >= 0.8 ? 'warning' : 'success';
}

/** Dead-row tone for a table: warning at 10%, danger at 20%; muted when unknown. */
export function deadRatioTone(ratio: number | null): Tone {
  if (ratio == null) return 'muted';
  return ratio >= 0.2 ? 'danger' : ratio >= 0.1 ? 'warning' : 'muted';
}

/** Redis fragmentation is worth a warning above 1.5. */
export function fragmentationTone(ratio: number | null): Tone { return ratio != null && ratio > 1.5 ? 'warning' : 'muted'; }
/** Any evicted key is a danger (the cache is dropping data). */
export function evictedTone(evicted: number | null): Tone { return evicted != null && evicted > 0 ? 'danger' : 'muted'; }
/** A transaction open longer than 5 minutes is a warning. */
export function longestTxTone(seconds: number | null): Tone { return seconds != null && seconds > 300 ? 'warning' : 'muted'; }
/** A Redis background save is a danger unless it reports ok. */
export function saveTone(status: string | null): Tone { return status != null && status !== 'ok' ? 'danger' : 'muted'; }

/** The most recent non-null value of a history `db` field, or null when no bucket carries it. */
export function latestDbValue(buckets: HistoryBucket[], key: keyof NonNullable<HistoryBucket['db']>): number | null {
  for (let i = buckets.length - 1; i >= 0; i -= 1) {
    const value = buckets[i].db?.[key];
    if (value != null) return value;
  }
  return null;
}

export type PgFinding =
  | { kind: 'deadRows'; table: string; percent: number; ago: string | null }
  | { kind: 'connections'; total: number; max: number }
  | { kind: 'cacheHit'; percent: number };

/** Picks the single most pressing Postgres finding (dead rows, then connections, then cache hit), or null when nothing needs attention. */
export function pgFinding(pg: PgStats | null | undefined, locale: string, now: number = Date.now()): PgFinding | null {
  if (!pg) return null;
  const worst = (pg.tables ?? []).filter((tb: PgTable) => (tb.dead_ratio ?? 0) >= 0.1).sort((a, b) => (b.dead_ratio ?? 0) - (a.dead_ratio ?? 0))[0];
  if (worst) return { kind: 'deadRows', table: worst.name, percent: Math.round((worst.dead_ratio ?? 0) * 100), ago: formatAgo(worst.last_autovacuum, locale, now) };
  const ratio = connectionRatio(pg.connections);
  if (ratio != null && ratio >= 0.8 && pg.connections) return { kind: 'connections', total: pg.connections.total as number, max: pg.connections.max as number };
  if (pg.cache_hit_ratio != null && pg.cache_hit_ratio < 0.95) return { kind: 'cacheHit', percent: Math.round(pg.cache_hit_ratio * 1000) / 10 };
  return null;
}
