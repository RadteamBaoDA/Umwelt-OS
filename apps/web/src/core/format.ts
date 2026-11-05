import { normalizeFormattingLocale, type AppLocaleId } from '@/core/i18n';

/** Time units tried largest-first when picking a relative-time unit. */
const RELATIVE_UNITS: ReadonlyArray<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 31_536_000],
  ['month', 2_592_000],
  ['day', 86_400],
  ['hour', 3_600],
  ['minute', 60],
  ['second', 1],
];

const cache = new Map<string, Intl.DateTimeFormat | Intl.NumberFormat | Intl.RelativeTimeFormat>();

/**
 * Returns a memoised Intl formatter. Constructing Intl objects is expensive, so one instance is shared per
 * (kind, locale, options) key. Options must be JSON-serialisable.
 */
function memo<T extends Intl.DateTimeFormat | Intl.NumberFormat | Intl.RelativeTimeFormat>(
  kind: string,
  locale: AppLocaleId,
  options: object,
  create: (canonicalLocale: string) => T,
): T {
  const key = `${kind}|${locale}|${JSON.stringify(options)}`;
  let formatter = cache.get(key) as T | undefined;
  if (!formatter) {
    formatter = create(normalizeFormattingLocale(locale));
    cache.set(key, formatter);
  }
  return formatter;
}

/** Coerces an ISO string, epoch millis or Date to a Date. */
function toDate(value: string | number | Date): Date {
  return value instanceof Date ? value : new Date(value);
}

/** Formats a calendar date (medium style) in the given zone; the locale id is normalised (en-us -> en-US). */
export function formatDate(value: string | number | Date, locale: AppLocaleId, timezone?: string): string {
  const options = { dateStyle: 'medium', timeZone: timezone } as const;
  return memo('date', locale, options, (l) => new Intl.DateTimeFormat(l, options)).format(toDate(value));
}

/** Formats a date and time (medium date, short time) in the given zone. Output matches `formatDateTime` in i18n.ts. */
export function formatDateTime(value: string | number | Date, locale: AppLocaleId, timezone?: string): string {
  const options = { dateStyle: 'medium', timeStyle: 'short', timeZone: timezone } as const;
  return memo('datetime', locale, options, (l) => new Intl.DateTimeFormat(l, options)).format(toDate(value));
}

/**
 * Formats a date/time with arbitrary `Intl.DateTimeFormat` options (memoised per locale and options). Prefer `formatDate` /
 * `formatDateTime` when their presets fit; use this for axis labels and other custom field selections.
 */
export function formatDateWith(value: string | number | Date, locale: AppLocaleId, options: Intl.DateTimeFormatOptions): string {
  return memo('datewith', locale, options, (l) => new Intl.DateTimeFormat(l, options)).format(toDate(value));
}

/** Formats a plain number with optional Intl.NumberFormat options. */
export function formatNumber(value: number, locale: AppLocaleId, options: Intl.NumberFormatOptions = {}): string {
  return memo('number', locale, options, (l) => new Intl.NumberFormat(l, options)).format(value);
}

/** Formats a monetary amount for an ISO 4217 currency code. */
export function formatCurrency(value: number, locale: AppLocaleId, currency: string, options: Intl.NumberFormatOptions = {}): string {
  const merged: Intl.NumberFormatOptions = { ...options, style: 'currency', currency };
  return memo('currency', locale, merged, (l) => new Intl.NumberFormat(l, merged)).format(value);
}

/** Formats a ratio (0.25 -> "25%"); pass `fractionDigits` for decimals. */
export function formatPercent(ratio: number, locale: AppLocaleId, fractionDigits = 0): string {
  const options = { style: 'percent', maximumFractionDigits: fractionDigits, minimumFractionDigits: fractionDigits } as const;
  return memo('percent', locale, options, (l) => new Intl.NumberFormat(l, options)).format(ratio);
}

/**
 * Formats a moment relative to `now` ("3 hours ago", "in 2 days") choosing the largest fitting unit.
 * Uses `numeric: 'auto'` so "yesterday"/"tomorrow" read naturally.
 */
export function formatRelative(value: string | number | Date, locale: AppLocaleId, now: number = Date.now()): string {
  const diffSeconds = Math.round((toDate(value).getTime() - now) / 1000);
  const abs = Math.abs(diffSeconds);
  const [unit, size] = RELATIVE_UNITS.find(([, s]) => abs >= s) ?? ['second', 1];
  const options = { numeric: 'auto' } as const;
  return memo('relative', locale, options, (l) => new Intl.RelativeTimeFormat(l, options)).format(
    Math.trunc(diffSeconds / size),
    unit,
  );
}

/** Test helper: drops memoised formatters. */
export function resetFormatCache(): void {
  cache.clear();
}
