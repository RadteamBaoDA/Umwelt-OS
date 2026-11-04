/** Which direction of change is an improvement for a metric. */
export type BetterDirection = 'up' | 'down' | 'neutral';

/** Key fragments whose increase is bad news (pollution, unemployment, hazards, vulnerabilities). */
const LOWER_IS_BETTER = ['aqi', 'pm25', 'pm10', 'unemployment', 'fires', 'fire_count', 'cve', 'earthquake', 'outage', 'inflation'];

/**
 * Maps a metric or provider key to the direction that counts as "better" for delta colouring.
 * Prices and indices stay neutral because a rise is not inherently good for a reader; unknown keys
 * default to neutral so nothing is coloured as good or bad without evidence.
 *
 * @param key Metric id or provider id (case-insensitive substring match); nullish yields 'neutral'.
 * @returns 'down' for lower-is-better metrics, otherwise 'neutral'.
 */
export function metricBetterDirection(key: string | null | undefined): BetterDirection {
  const k = (key ?? '').toLowerCase();
  return LOWER_IS_BETTER.some((fragment) => k.includes(fragment)) ? 'down' : 'neutral';
}
