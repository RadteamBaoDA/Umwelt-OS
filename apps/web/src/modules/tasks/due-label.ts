/** Localized fragments the compact due label needs. */
export interface DueLabelText {
  /** "overdue 2d" style text for a positive number of days past due. */
  overdue: (days: number) => string;
  /** Text for a task due today. */
  today: string;
  /** Text for a task without a due day. */
  none: string;
}

const DAY_MS = 86_400_000;

/** Whole-day distance `a - b` between two YYYY-MM-DD calendar days (UTC arithmetic, so DST-free). */
function dayDiff(a: string, b: string): number {
  return Math.round((Date.parse(`${a}T00:00:00Z`) - Date.parse(`${b}T00:00:00Z`)) / DAY_MS);
}

/**
 * Compact relative due label for the terminal task rows: `overdue 2d`, `today`, a short weekday within the next
 * six days (`Fri`), otherwise a short month and day (`Dec 3`). Pure: both inputs are calendar days already resolved in
 * the owner's timezone, and formatting uses `locale` with the UTC zone so the day never shifts.
 *
 * @param due Due calendar day (YYYY-MM-DD) or null when undated.
 * @param today Today's calendar day (YYYY-MM-DD) in the owner's timezone.
 * @param locale BCP 47 formatting locale.
 * @param text Localized fragments.
 * @returns The label; `text.none` when `due` is null.
 */
export function dueLabel(due: string | null, today: string, locale: string, text: DueLabelText): string {
  if (!due) return text.none;
  const diff = dayDiff(due, today);
  if (diff < 0) return text.overdue(-diff);
  if (diff === 0) return text.today;
  const at = new Date(`${due}T00:00:00Z`);
  const options: Intl.DateTimeFormatOptions =
    diff < 7 ? { weekday: 'short', timeZone: 'UTC' } : { month: 'short', day: 'numeric', timeZone: 'UTC' };
  return new Intl.DateTimeFormat(locale, options).format(at);
}
