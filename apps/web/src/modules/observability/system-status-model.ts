import type { HistoryBucket, HistoryComponent, ObservedRun, SystemHealth } from './api';

/** Components whose health decides the overall verdict, in display order. */
export const CORE_SERVICES = ['postgres', 'redis', 'worker', 'chat_worker', 'model_worker'] as const;
/** Optional integrations that never affect the overall verdict, in display order. */
export const OPTIONAL_SERVICES = ['model_gateway', 'graph', 'n8n', 'browser'] as const;

export type Tone = 'success' | 'warning' | 'danger' | 'muted';

/** Maps a backend health status (and optional connectivity) to a semantic dot tone; unknown codes stay muted rather than red. */
export function statusTone(status: string | undefined, connectivity?: string): Tone {
  if (status === 'healthy' || status === 'ok') return 'success';
  if (status === 'configured') return connectivity === 'not_tested' ? 'muted' : 'success';
  if (status === 'degraded') return 'warning';
  if (status === 'unavailable' || status === 'down' || status === 'error') return 'danger';
  return 'muted';
}

/** True for statuses meaning "optional thing the owner has not set up", used to count unconfigured integrations. */
export function isUnconfigured(status: string | undefined): boolean {
  return status === 'unconfigured' || status === 'not_configured' || status === 'not_installed';
}

/** Facts for the one-line header summary, computed from the live health projection. */
export function summarizeHealth(health: SystemHealth) {
  const healthy = CORE_SERVICES.filter((key) => statusTone(health.components[key]?.status) === 'success').length;
  const unconfigured = OPTIONAL_SERVICES.filter((key) => isUnconfigured(health.components[key]?.status)).length;
  return { healthy, total: CORE_SERVICES.length, unconfigured, coreOk: healthy === CORE_SERVICES.length };
}

/** Colour tone of one uptime bar: all up = success, partial = warning, none up = danger, not applicable = muted. */
export function barTone(component: Pick<HistoryComponent, 'up' | 'total'> | undefined): Tone {
  if (!component || component.total <= 0) return 'muted';
  if (component.up >= component.total) return 'success';
  return component.up <= 0 ? 'danger' : 'warning';
}

/** Uptime percentage (0..100) over buckets for one component or the overall verdict; null when nothing applied. */
export function uptimePercent(buckets: HistoryBucket[], key?: string): number | null {
  let up = 0;
  let total = 0;
  for (const bucket of buckets) {
    if (key === undefined) { up += bucket.overall_healthy; total += bucket.samples; continue; }
    const component = bucket.components[key];
    if (component && component.total > 0) { up += component.up; total += component.total; }
  }
  return total > 0 ? (up / total) * 100 : null;
}

/** Inserts null placeholders for bucket slots without samples so charts show gaps instead of joining across them. */
export function fillBuckets(buckets: HistoryBucket[], bucketSeconds: number): (HistoryBucket | { start: string; missing: true })[] {
  if (buckets.length === 0 || bucketSeconds <= 0) return buckets;
  const step = bucketSeconds * 1000;
  const byTime = new Map(buckets.map((bucket) => [new Date(bucket.start).getTime(), bucket]));
  const first = new Date(buckets[0].start).getTime();
  const last = new Date(buckets[buckets.length - 1].start).getTime();
  const out: (HistoryBucket | { start: string; missing: true })[] = [];
  // Bounded: the server caps buckets per range, and we never emit more than 2000 slots.
  for (let t = first, n = 0; t <= last && n < 2000; t += step, n += 1) out.push(byTime.get(t) ?? { start: new Date(t).toISOString(), missing: true });
  return out;
}

const FAILED = ['failed', 'error', 'blocked'];
/** True when a run ended in a failing state. */
export function isFailedRun(run: ObservedRun): boolean { return FAILED.includes(run.status); }

/**
 * Buckets runs by the hour of `created_at` in `timeZone` (browser zone when omitted), ascending, counting succeeded
 * (non-failed) and failed. The bucket timestamp is the instant the local hour began, so fractional-offset zones stay correct.
 */
export function bucketRunsByHour(runs: ObservedRun[], timeZone?: string): { timestamp: string; ok: number; failed: number }[] {
  const fmt = new Intl.DateTimeFormat('en-US', { timeZone, minute: 'numeric', second: 'numeric', hourCycle: 'h23' });
  const map = new Map<number, { ok: number; failed: number }>();
  for (const run of runs) {
    const at = Math.floor(new Date(run.created_at).getTime() / 1000) * 1000;
    const parts = fmt.formatToParts(new Date(at));
    const part = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
    const hour = at - part('minute') * 60_000 - part('second') * 1000;
    const slot = map.get(hour) ?? { ok: 0, failed: 0 };
    if (isFailedRun(run)) slot.failed += 1; else slot.ok += 1;
    map.set(hour, slot);
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0]).map(([time, slot]) => ({ timestamp: new Date(time).toISOString(), ...slot }));
}

/** Finding about the runs window: no runs, no failures, or the kind that holds most failures. */
export function activityFinding(runs: ObservedRun[]):
  | { kind: 'none' } | { kind: 'clean'; total: number } | { kind: 'failures'; failed: number; total: number; topKind: string; topCount: number } {
  if (runs.length === 0) return { kind: 'none' };
  const failedRuns = runs.filter(isFailedRun);
  if (failedRuns.length === 0) return { kind: 'clean', total: runs.length };
  const counts = new Map<string, number>();
  for (const run of failedRuns) counts.set(run.kind, (counts.get(run.kind) ?? 0) + 1);
  const [topKind, topCount] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  return { kind: 'failures', failed: failedRuns.length, total: runs.length, topKind, topCount };
}

/** Statuses that mean work is still waiting or in flight; terminal states (succeeded, failed, ...) are excluded. */
export const ACTIVE_QUEUE_STATUSES = ['pending', 'queued', 'running', 'retrying'] as const;

/** Sums only the active-status counts across the durable queue summary maps. */
export function queueTotal(queue: { ingestion_stages: Record<string, number>; event_delivery: Record<string, number>; connector_provisioning: Record<string, number> }): number {
  return [queue.ingestion_stages, queue.event_delivery, queue.connector_provisioning]
    .reduce((sum, map) => sum + ACTIVE_QUEUE_STATUSES.reduce((a, key) => a + (map[key] ?? 0), 0), 0);
}

/** Maps a 409 ApiError-like failure to true; other failures are not stale-revision conflicts. */
export function isStaleRevision(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { status?: number }).status === 409;
}
