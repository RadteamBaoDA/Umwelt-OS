'use client';

import { useTranslations } from 'next-intl';
import type { GadgetDocumentProjection } from '@/modules/knowledge/public-api';
import type { GadgetInstance } from '../api';
import { MediaCollection, releaseTimeZone } from './media-parts';

/** Literal calendar section labels, ordered chronologically with recently released records first. */
const CALENDAR_BUCKET_KEY = {
  recent: 'media.recent',
  today: 'media.today',
  tomorrow: 'media.tomorrow',
  week: 'media.thisWeek',
  upcoming: 'media.upcoming',
} as const;

/** Converts an instant to a day number in the display timezone, avoiding DST-length assumptions. */
function calendarDay(value: string | number, timezone: string): number {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: timezone, year: 'numeric', month: 'numeric', day: 'numeric' }).formatToParts(
    new Date(value),
  );
  /** Reads the numeric calendar field emitted by Intl in the requested timezone. */
  const part = (type: string) => Number(parts.find((value) => value.type === type)?.value);
  return Date.UTC(part('year'), part('month') - 1, part('day')) / 86_400_000;
}

/** Groups typed release instants in chronological buckets using the owner's display timezone; unknown dates are omitted. */
export function calendarGroups(items: GadgetDocumentProjection[], timezone: string, now = Date.now()) {
  const today = calendarDay(now, timezone);
  // Monday is the first day of the schedule week; tomorrow takes precedence at the boundary.
  const weekday = new Date(today * 86_400_000).getUTCDay();
  const weekEnd = today + (7 - ((weekday + 6) % 7));
  const groups: Record<keyof typeof CALENDAR_BUCKET_KEY, GadgetDocumentProjection[]> = {
    recent: [],
    today: [],
    tomorrow: [],
    week: [],
    upcoming: [],
  };
  const datedItems = items.filter((item) => item.media?.release_date && Number.isFinite(Date.parse(item.media.release_date)));
  for (const item of datedItems.sort(
    (a, b) =>
      Date.parse(a.media?.release_date ?? '') - Date.parse(b.media?.release_date ?? '') || a.document_id.localeCompare(b.document_id),
  )) {
    const day = calendarDay(item.media!.release_date!, releaseTimeZone(item.media!.release_date!, timezone));
    const bucket = day < today ? 'recent' : day === today ? 'today' : day === today + 1 ? 'tomorrow' : day < weekEnd ? 'week' : 'upcoming';
    groups[bucket].push(item);
  }
  return (Object.keys(groups) as (keyof typeof CALENDAR_BUCKET_KEY)[])
    .filter((key) => groups[key].length)
    .map((key) => ({ key, items: groups[key] }));
}

/** Multi-source release schedule shares media filters/actions and requests the dedicated ascending cursor contract. */
export function MediaCalendarGadget({ instance }: { instance: GadgetInstance }) {
  const t = useTranslations('dashboard');
  return (
    <MediaCollection
      instance={instance}
      calendar
      groupItems={(items, timezone) =>
        calendarGroups(items, timezone).map((group) => ({ ...group, label: t(CALENDAR_BUCKET_KEY[group.key]) }))
      }
    />
  );
}
