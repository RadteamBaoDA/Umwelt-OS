import { getDateTimeFormat, getNumberFormat } from '@/modules/shared';
import { formatCompactAgo } from './relative-time';

/** Longest window the observations API accepts, in days; the `ALL` chip reads exactly this. */
export const MAX_LOOKBACK_DAYS = 3660;

/** Chart range chips in display order; `days` is the lookback the existing observations query is asked for. */
export const CHART_RANGES = [
  { id: '24h', days: 1 },
  { id: '7D', days: 7 },
  { id: '30D', days: 30 },
  { id: '90D', days: 90 },
  { id: '1Y', days: 365 },
  { id: 'ALL', days: MAX_LOOKBACK_DAYS },
] as const;

/** Identifier of one range chip. */
export type ChartRangeId = (typeof CHART_RANGES)[number]['id'];

/**
 * Maps a lookback to the closest chip (by ratio, so 10 days is 7D and 2000 days is ALL).
 *
 * @param days Effective lookback in days; non-positive values map to the first chip.
 * @returns The nearest chip id.
 */
export function nearestRange(days: number): ChartRangeId {
  if (!(days > 0)) return CHART_RANGES[0].id;
  return CHART_RANGES.reduce((best, r) => (Math.abs(Math.log(days / r.days)) < Math.abs(Math.log(days / best.days)) ? r : best)).id;
}

/**
 * Lookback in days of a chip.
 *
 * @param id Chip id.
 * @returns Days (ALL is the API maximum).
 */
export function rangeLookbackDays(id: ChartRangeId): number {
  return CHART_RANGES.find((r) => r.id === id)?.days ?? CHART_RANGES[1].days;
}

/**
 * Builds a compact y-axis tick formatter (`72K`) whose fraction digits follow the tick spacing, so close ticks such as
 * 1.08 / 1.09 / 1.10 stay distinguishable while 72000 / 69000 / 66000 stay at zero digits.
 *
 * @param min Lowest plotted value.
 * @param max Highest plotted value.
 * @param locale Display locale.
 * @returns Formatter for one tick value.
 */
export function axisValueFormatter(min: number, max: number, locale: string): (value: number) => string {
  const top = Math.max(Math.abs(min), Math.abs(max));
  const scale = top >= 1e12 ? 1e12 : top >= 1e9 ? 1e9 : top >= 1e6 ? 1e6 : top >= 1e3 ? 1e3 : 1;
  const step = (max - min) / 2 / scale;
  const digits = step >= 1 || step <= 0 ? 0 : Math.min(6, Math.ceil(-Math.log10(step)));
  const format = getNumberFormat(locale, { notation: 'compact', maximumFractionDigits: digits });
  return (value) => format.format(value);
}

/**
 * Builds the x-axis tick formatter: clock time for spans up to two days, up to about ten months `Nov 5`, beyond that `Nov 2025`.
 *
 * @param spanMs Plotted time span in ms.
 * @param locale Display locale.
 * @param timeZone IANA zone of the display preference.
 * @returns Formatter for one epoch-ms tick.
 */
export function axisDateFormatter(spanMs: number, locale: string, timeZone: string): (ms: number) => string {
  const short = spanMs <= 2 * 86_400_000;
  // Spans near a year or more need the year, or both ends would read the same ("Nov 18" ... "Nov 18").
  const long = spanMs > 300 * 86_400_000;
  const format = getDateTimeFormat(
    locale,
    short
      ? { hour: '2-digit', minute: '2-digit', hour12: false, timeZone }
      : long
        ? { month: 'short', year: 'numeric', timeZone }
        : { month: 'short', day: 'numeric', timeZone },
  );
  return (ms) => format.format(new Date(ms));
}

/** Stale facts of a failing source: compact age of the last success and the next scheduled retry. */
export interface StaleInfo {
  /** Compact age (`2h`) of the last good collection, or null when it never succeeded. */
  ago: string | null;
  /** Epoch ms of the next scheduled collection, or null when unknown. */
  nextDueMs: number | null;
}

/**
 * Derives the "last good data / next retry" facts from a source row.
 *
 * @param source `last_success_at` / `next_due_at` of the bound source, if loaded.
 * @param now Epoch ms treated as now (the source list's fetch time keeps render pure).
 * @returns Facts, or null when neither is known (callers then show only the error line).
 */
export function staleInfo(
  source: { last_success_at?: string | null; next_due_at?: string | null } | undefined,
  now: number,
): StaleInfo | null {
  const ago = source?.last_success_at ? formatCompactAgo(source.last_success_at, now) || null : null;
  const due = source?.next_due_at ? Date.parse(source.next_due_at) : NaN;
  const nextDueMs = Number.isNaN(due) ? null : due;
  return ago === null && nextDueMs === null ? null : { ago, nextDueMs };
}
