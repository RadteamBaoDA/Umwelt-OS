/**
 * Module-level memoisation of `Intl` formatters. Constructing a formatter is orders of magnitude slower than
 * calling `.format()`, so list rows and polling panels share one instance per (locale, options) pair.
 * The caches are unbounded but keyed only by locale/option combinations used in code, so they stay tiny.
 */
const numberFormats = new Map<string, Intl.NumberFormat>();
const dateTimeFormats = new Map<string, Intl.DateTimeFormat>();

/** Stable cache key: locale plus the option bag serialised with sorted keys. */
function cacheKey(locale: string, options: object | undefined): string {
  const entries = Object.entries(options ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `${locale}|${JSON.stringify(entries)}`;
}

/**
 * Returns a shared `Intl.NumberFormat` for the locale and options (created on first use).
 * @param locale BCP 47 formatting locale.
 * @param options Standard `Intl.NumberFormat` options; treated as immutable.
 */
export function getNumberFormat(locale: string, options?: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = cacheKey(locale, options);
  let format = numberFormats.get(key);
  if (!format) {
    format = new Intl.NumberFormat(locale, options);
    numberFormats.set(key, format);
  }
  return format;
}

/**
 * Returns a shared `Intl.DateTimeFormat` for the locale and options (created on first use).
 * @param locale BCP 47 formatting locale.
 * @param options Standard `Intl.DateTimeFormat` options (include `timeZone` explicitly); treated as immutable.
 */
export function getDateTimeFormat(locale: string, options?: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = cacheKey(locale, options);
  let format = dateTimeFormats.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat(locale, options);
    dateTimeFormats.set(key, format);
  }
  return format;
}
