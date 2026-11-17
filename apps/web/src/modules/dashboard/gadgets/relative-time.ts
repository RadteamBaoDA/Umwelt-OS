import type { ChartDataPoint } from '@/core/charts/chart-format';

/**
 * Formats an ISO timestamp as a short localized relative phrase ("2 hr. ago", "yesterday").
 * Pure and clock-injectable so list rows stay deterministic in tests.
 *
 * @param iso ISO timestamp; unparseable values return an empty string.
 * @param locale BCP-47 display locale (any casing).
 * @param now Epoch ms treated as "now" (defaults to the current time).
 * @returns Relative phrase, or '' when the timestamp is invalid.
 */
export function formatRelativeTime(iso: string | null | undefined, locale: string, now: number = Date.now()): string {
  const ms = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(ms)) return '';
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto', style: 'short' });
  const minutes = Math.round((ms - now) / 60_000);
  if (Math.abs(minutes) < 60) return rtf.format(minutes, 'minute');
  if (Math.abs(minutes) < 1440) return rtf.format(Math.round(minutes / 60), 'hour');
  if (Math.abs(minutes) < 43_200) return rtf.format(Math.round(minutes / 1440), 'day');
  return rtf.format(Math.round(minutes / 43_200), 'month');
}

/**
 * Buckets timestamps into per-UTC-day counts as time-ordered chart points.
 * Days with no items between the first and last bucket are filled with zero so the line is honest.
 *
 * @param timestamps ISO timestamps (invalid ones are skipped).
 * @returns Ascending daily count points; empty when there are no valid timestamps.
 */
export function bucketByDay(timestamps: Array<string | null | undefined>): ChartDataPoint[] {
  const counts = new Map<number, number>();
  for (const iso of timestamps) {
    const ms = iso ? Date.parse(iso) : NaN;
    if (Number.isNaN(ms)) continue;
    const day = Math.floor(ms / 86_400_000);
    counts.set(day, (counts.get(day) ?? 0) + 1);
  }
  if (counts.size === 0) return [];
  const days = [...counts.keys()];
  const first = Math.min(...days);
  const last = Math.max(...days);
  const points: ChartDataPoint[] = [];
  // Bound the fill so a stray ancient timestamp cannot create thousands of points.
  for (let day = Math.max(first, last - 90); day <= last; day += 1) {
    points.push({ timestamp: day * 86_400_000, value: counts.get(day) ?? 0 });
  }
  return points;
}
