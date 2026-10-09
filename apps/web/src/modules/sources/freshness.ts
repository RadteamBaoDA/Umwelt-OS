/** Provider freshness rules: data is stale after two missed polling intervals. */
export const STALE_AFTER_INTERVALS = 2;

/** True when the last successful collection is older than two polling intervals; a source that never succeeded is not "stale". */
export function isStale(lastSuccessAt: string | null | undefined, intervalMinutes: number | null | undefined, now: Date = new Date()): boolean {
  if (!lastSuccessAt || !intervalMinutes || intervalMinutes <= 0) return false;
  const last = Date.parse(lastSuccessAt);
  if (Number.isNaN(last)) return false;
  return now.getTime() - last > STALE_AFTER_INTERVALS * intervalMinutes * 60_000;
}

/** Which dataset time a provider's value is anchored to: FX shows a reference date, annual macro data its period. */
export type PeriodKind = 'referenceDate' | 'annualPeriod' | null;

const PERIOD_KIND: Record<string, PeriodKind> = { frankfurter: 'referenceDate', ecb: 'referenceDate', world_bank: 'annualPeriod' };

export function periodKind(provider: string | null | undefined): PeriodKind {
  return (provider && PERIOD_KIND[provider]) || null;
}
