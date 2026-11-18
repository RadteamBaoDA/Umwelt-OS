/** Translator subset the summary needs: a key plus optional ICU values. */
type Translate = (
  key: 'summaryUpdated' | 'summarySources' | 'summaryLiveEvents' | 'summaryGadgets' | 'staleSources.summary',
  values?: Record<string, string | number>,
) => string;

/** Counts shown after the freshness fragment in the terminal statusline. */
export interface TerminalSummaryCounts {
  /** Active sources bound to the dashboard's gadgets. */
  sources: number;
  /** Events currently shown by the Overview live map; null while no live map is mounted. */
  liveEvents: number | null;
  /** Gadgets on the active non-Overview group; null/omitted on Overview. */
  gadgets?: number | null;
}

/**
 * Joins the header summary fragments with " · ": "Updated {age}", then (terminal skin only, when `counts` is given)
 * "{n} sources", "{n} live events" and "{n} gadgets", then the failing-sources fragment. A fragment whose data is unknown or zero
 * (no freshness time, no bound sources, no live map) is omitted.
 *
 * Pure: formatting only, no I/O.
 *
 * @param t Translator for the `dashboard` catalog.
 * @param updatedAge Pre-formatted relative age of the newest successful collection, or null when unknown.
 * @param staleCount Number of failing bound sources.
 * @param counts Terminal-only source and live-event counts; omit for the default skin.
 * @returns The joined text, or null when every fragment is absent.
 */
export function buildHeaderSummary(
  t: Translate,
  updatedAge: string | null,
  staleCount: number,
  counts?: TerminalSummaryCounts,
): string | null {
  const parts = [
    updatedAge !== null ? t('summaryUpdated', { time: updatedAge }) : null,
    counts && counts.sources > 0 ? t('summarySources', { count: counts.sources }) : null,
    counts && counts.liveEvents !== null ? t('summaryLiveEvents', { count: counts.liveEvents }) : null,
    counts && counts.gadgets != null ? t('summaryGadgets', { count: counts.gadgets }) : null,
    staleCount > 0 ? t('staleSources.summary', { count: staleCount }) : null,
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}
