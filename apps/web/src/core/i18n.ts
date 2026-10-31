export const appLocaleIds = ['en-us', 'vi-vi'] as const;
export type AppLocaleId = (typeof appLocaleIds)[number];
export type ThemePreference = 'light' | 'dark' | 'system';

const formattingLocales: Record<AppLocaleId, string> = {
  'en-us': 'en-US',
  'vi-vi': 'vi-VN',
};

/** Maps the application locale identifier to the canonical locale used by Intl formatters. */
export function normalizeFormattingLocale(locale: AppLocaleId): string {
  return formattingLocales[locale];
}

/** Formats a timestamp with an explicit application locale and time zone. */
export function formatDateTime(value: string | Date, locale: AppLocaleId, timezone: string): string {
  return new Intl.DateTimeFormat(normalizeFormattingLocale(locale), {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: timezone,
  }).format(value instanceof Date ? value : new Date(value));
}

/**
 * Renders an IANA zone id as a short offset label (e.g. "GMT+7") for user-facing copy.
 * Falls back to the raw zone id when the zone or locale is rejected by Intl.
 */
export function formatTimeZoneLabel(timezone: string, locale: AppLocaleId): string {
  try {
    const part = new Intl.DateTimeFormat(normalizeFormattingLocale(locale), { timeZone: timezone, timeZoneName: 'short' })
      .formatToParts(new Date()).find((p) => p.type === 'timeZoneName');
    return part?.value ?? timezone;
  } catch {
    return timezone;
  }
}
