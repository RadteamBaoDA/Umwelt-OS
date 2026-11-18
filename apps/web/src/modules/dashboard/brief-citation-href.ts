/**
 * In-app page a daily-brief citation opens, or null when no page shows that kind of fact.
 * Stories open the News story dialog, events the pinned Timeline event, source health the Sources list; market,
 * weather, hazard, task and goal facts have no page and stay plain text.
 *
 * @param kind - Citation fact kind (`stories`, `events`, ...).
 * @param id - Citation fact id (story / event id; ignored for source health). Trimmed, cut to 64 characters and
 * URL-encoded; an empty id opens the list page instead.
 * @returns App-relative href, or null.
 */
export function briefCitationHref(kind: string, id: string): string | null {
  const safeId = id.trim().slice(0, 64);
  if (kind === 'stories' || kind === 'story') return safeId ? `/news?story=${encodeURIComponent(safeId)}` : '/news';
  if (kind === 'events') return safeId ? `/timeline?event_id=${encodeURIComponent(safeId)}` : '/timeline';
  if (kind === 'source_health') return '/settings/sources';
  return null;
}
