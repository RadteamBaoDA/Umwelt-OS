import { describe, expect, it } from 'vitest';
import type { HistoryBucket, ObservedRun, SystemHealth } from './api';
import { activityFinding, bucketRunsByHour, queueTotal, barTone, fillBuckets, isStaleRevision, statusTone, summarizeHealth, uptimePercent } from './system-status-model';

const bucket = (start: string, healthy: number, samples: number, components: HistoryBucket['components'] = {}): HistoryBucket => ({ start, samples, overall_healthy: healthy, queue_backlog_max: 0, components });
const run = (kind: ObservedRun['kind'], status: string): ObservedRun => ({ kind, id: kind + status, status, error_code: null, created_at: '2026-10-01T10:00:00Z', updated_at: '', finished_at: null, duration_ms: null, usage: null, trace: { request_id: null, ingestion_run_id: null, agent_run_id: null, tool_call_id: null } });

describe('system status model', () => {
  it('summarizes core health and unconfigured integrations', () => {
    const health: SystemHealth = { overall: 'degraded', components: {
      postgres: { status: 'healthy' }, redis: { status: 'healthy' }, worker: { status: 'healthy' }, chat_worker: { status: 'healthy' }, model_worker: { status: 'unavailable' },
      graph: { status: 'not_installed' }, n8n: { status: 'not_configured' }, browser: { status: 'configured' }, model_gateway: { status: 'configured', connectivity: 'not_tested' },
    } };
    expect(summarizeHealth(health)).toEqual({ healthy: 4, total: 5, unconfigured: 2, coreOk: false });
  });
  it('colours uptime bars including not-applicable buckets', () => {
    expect(barTone({ up: 3, total: 3 })).toBe('success');
    expect(barTone({ up: 1, total: 3 })).toBe('warning');
    expect(barTone({ up: 0, total: 3 })).toBe('danger');
    expect(barTone({ up: 0, total: 0 })).toBe('muted');
    expect(barTone(undefined)).toBe('muted');
  });
  it('keeps untested and unconfigured services muted', () => {
    expect(statusTone('configured', 'not_tested')).toBe('muted');
    expect(statusTone('not_installed')).toBe('muted');
    expect(statusTone('unavailable')).toBe('danger');
  });
  it('computes uptime and ignores total=0 components', () => {
    const buckets = [bucket('a', 1, 2, { n8n: { up: 0, total: 0, latency_ms_avg: null, latency_ms_max: null } }), bucket('b', 2, 2)];
    expect(uptimePercent(buckets)).toBe(75);
    expect(uptimePercent(buckets, 'n8n')).toBeNull();
    expect(uptimePercent([])).toBeNull();
  });
  it('fills missing bucket slots', () => {
    const out = fillBuckets([bucket('2026-10-01T00:00:00Z', 1, 1), bucket('2026-10-01T00:03:00Z', 1, 1)], 60);
    expect(out).toHaveLength(4);
    expect('missing' in out[1]).toBe(true);
  });
  it('finds where failures concentrate', () => {
    expect(activityFinding([])).toEqual({ kind: 'none' });
    expect(activityFinding([run('chat', 'succeeded')])).toEqual({ kind: 'clean', total: 1 });
    expect(activityFinding([run('agent', 'failed'), run('agent', 'error'), run('chat', 'failed'), run('chat', 'succeeded')])).toEqual({ kind: 'failures', failed: 3, total: 4, topKind: 'agent', topCount: 2 });
  });
  it('detects 409 conflicts', () => {
    expect(isStaleRevision({ status: 409 })).toBe(true);
    expect(isStaleRevision({ status: 500 })).toBe(false);
  });
  it('sums only active queue statuses', () => {
    expect(queueTotal({ ingestion_stages: { pending: 2, running: 1, succeeded: 50 }, event_delivery: { queued: 3, retrying: 1, failed: 9 }, connector_provisioning: {} })).toBe(7);
  });
  it('buckets runs by the local hour of the given zone', () => {
    const at = (iso: string): ObservedRun => ({ ...run('chat', 'succeeded'), created_at: iso });
    const runs = [at('2026-10-01T16:50:00Z'), at('2026-10-01T17:10:00Z')];
    expect(bucketRunsByHour(runs, 'UTC').map((b) => b.timestamp)).toEqual(['2026-10-01T16:00:00.000Z', '2026-10-01T17:00:00.000Z']);
    // India is UTC+5:30: both runs fall in local hours starting at :30 UTC.
    expect(bucketRunsByHour(runs, 'Asia/Kolkata').map((b) => b.timestamp)).toEqual(['2026-10-01T16:30:00.000Z']);
  });
});
