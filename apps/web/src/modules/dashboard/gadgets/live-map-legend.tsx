'use client';

import { useMemo } from 'react';
import { useTranslations } from 'next-intl';
import { LIVE_TAG_KEY, LIVE_TAGS } from '../live-feed/sources';

/** One live-feed category tag. */
type LiveTag = (typeof LIVE_TAGS)[number];

/**
 * Token colour of each category dot in the overview legend. Categories without an entry use the muted
 * token. Colours are semantic tokens only so both themes and the terminal skin resolve them.
 */
const LEGEND_TONE: Partial<Record<LiveTag, string>> = {
  disaster: 'var(--danger)',
  weather: 'var(--info, var(--accent))',
  geo: 'var(--status-warning)',
  space: 'var(--accent)',
};

/**
 * Token colour of one category tag (muted for unknown tags); shared by the legend dots and the terminal event rows.
 *
 * @param tag Category tag (lower-case).
 */
export function legendTone(tag: string): string {
  return LEGEND_TONE[tag as LiveTag] ?? 'var(--muted)';
}

/**
 * Counts how many events carry each category tag, in `LIVE_TAGS` order, keeping only tags with at least one event.
 * Tags are already lower-cased and de-duplicated per event by the feed store, so one event counts once per tag.
 *
 * @param events Events to count; not mutated.
 * @returns Non-empty-count entries in display order; an empty array when no event carries a known tag.
 */
export function countEventTags(events: { tags: string[] }[]): { tag: LiveTag; count: number }[] {
  return LIVE_TAGS.map((tag) => ({ tag, count: events.filter((event) => event.tags.includes(tag)).length })).filter(
    (entry) => entry.count > 0,
  );
}

/**
 * Non-interactive legend beside the overview event heading: one dot and count per category that has events.
 * Renders nothing when no event carries a known category. Counts reflect the events the list shows (after
 * the active filters), so the legend always agrees with the heading count.
 *
 * @param props.events Filtered events the overview list is showing.
 */
export function LiveEventLegend({ events }: { events: { tags: string[] }[] }) {
  const t = useTranslations('dashboard');
  const counts = useMemo(() => countEventTags(events), [events]);
  if (counts.length === 0) return null;
  return (
    <span className="dash-live-legend" data-testid="live-legend">
      {counts.map(({ tag, count }) => (
        <span key={tag} className="dash-live-legend-item">
          <span className="dash-live-legend-dot rounded-full" aria-hidden="true" style={{ background: legendTone(tag) }} />
          {/* Terminal skin keeps only dot + count visible (demo legend); the name stays for assistive tech. */}
          <span className="dash-live-legend-name">{t(LIVE_TAG_KEY[tag])} </span>
          {count}
        </span>
      ))}
    </span>
  );
}
