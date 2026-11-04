import { describe, expect, it } from 'vitest';
import type { HistoryBucket, PgStats } from './api';
import { connectionRatio, connectionTone, deadRatioTone, evictedTone, formatAgo, formatBytes, formatRatio, formatUptime, fragmentationTone, latestDbValue, longestTxTone, pgFinding, saveTone } from './database-model';

const pg = (over: Partial<PgStats> = {}): PgStats => ({
  status: 'healthy', version: '16', uptime_seconds: 10, database_size_bytes: 1, connections: { active: 1, idle: 1, idle_in_transaction: 0, waiting: 0, total: 8, max: 100 },
  cache_hit_ratio: 0.99, index_hit_ratio: 0.99, transactions: null, deadlocks: 0, temp_bytes: 0, locks_waiting: 0, longest_transaction_seconds: 0, tables: [], ...over,
});
const table = (name: string, dead_ratio: number | null, last_autovacuum: string | null = null) => ({ name, total_bytes: 1, live_rows: 1, dead_rows: 1, dead_ratio, seq_scans: 0, index_scans: 0, last_autovacuum, last_autoanalyze: null });

describe('database model', () => {
  it('formats bytes, ratios and uptime with null handling', () => {
    expect(formatBytes(null, 'en-US')).toBeNull();
    expect(formatBytes(512, 'en-US')).toBe('512 B');
    expect(formatBytes(1536, 'en-US')).toBe('1.5 KiB');
    expect(formatBytes(5 * 1024 ** 3, 'en-US')).toBe('5 GiB');
    expect(formatRatio(null, 'en-US')).toBeNull();
    expect(formatRatio(0.9876, 'en-US')).toBe('98.8%');
    expect(formatUptime(null, 'en-US')).toBeNull();
    expect(formatUptime(3 * 86400 + 4 * 3600, 'en-US')).toBe('3d 4h');
    expect(formatAgo(null, 'en-US')).toBeNull();
    expect(formatAgo('2026-10-01T00:00:00Z', 'en-US', Date.parse('2026-10-04T00:00:00Z'))).toBe('3 days ago');
  });
  it('applies connection thresholds at the true ratio', () => {
    expect(connectionRatio({ active: 0, idle: 0, idle_in_transaction: 0, waiting: 0, total: 8, max: 100 })).toBe(0.08);
    expect(connectionRatio({ active: 0, idle: 0, idle_in_transaction: 0, waiting: 0, total: 8, max: 0 })).toBeNull();
    expect(connectionRatio(null)).toBeNull();
    expect([0.79, 0.8, 0.95].map(connectionTone)).toEqual(['success', 'warning', 'danger']);
    expect(connectionTone(null)).toBe('muted');
  });
  it('applies dead ratio, fragmentation, evicted, transaction and save thresholds', () => {
    expect([0.09, 0.1, 0.2].map(deadRatioTone)).toEqual(['muted', 'warning', 'danger']);
    expect(deadRatioTone(null)).toBe('muted');
    expect([1.5, 1.51].map(fragmentationTone)).toEqual(['muted', 'warning']);
    expect([0, 1, null].map(evictedTone)).toEqual(['muted', 'danger', 'muted']);
    expect([300, 301].map(longestTxTone)).toEqual(['muted', 'warning']);
    expect([['ok'], ['err'], [null]].map(([s]) => saveTone(s))).toEqual(['muted', 'danger', 'muted']);
  });
  it('picks the most pressing finding', () => {
    const now = Date.parse('2026-10-04T00:00:00Z');
    expect(pgFinding(pg(), 'en-US', now)).toBeNull();
    expect(pgFinding(null, 'en-US', now)).toBeNull();
    expect(pgFinding(pg({ cache_hit_ratio: 0.9 }), 'en-US', now)).toEqual({ kind: 'cacheHit', percent: 90 });
    expect(pgFinding(pg({ cache_hit_ratio: 0.9, connections: { active: 0, idle: 0, idle_in_transaction: 0, waiting: 0, total: 85, max: 100 } }), 'en-US', now)).toEqual({ kind: 'connections', total: 85, max: 100 });
    expect(pgFinding(pg({ tables: [table('a', 0.12), table('documents', 0.23, '2026-10-01T00:00:00Z')], cache_hit_ratio: 0.5 }), 'en-US', now))
      .toEqual({ kind: 'deadRows', table: 'documents', percent: 23, ago: '3 days ago' });
  });
  it('reads the latest non-null history value', () => {
    const b = (tps: number | null | undefined): HistoryBucket => ({ start: 'x', samples: 1, overall_healthy: 1, queue_backlog_max: 0, components: {}, db: tps === undefined ? undefined : { pg_tps_avg: tps } as never });
    expect(latestDbValue([b(5), b(null), b(undefined)], 'pg_tps_avg')).toBe(5);
    expect(latestDbValue([], 'pg_tps_avg')).toBeNull();
  });
});
