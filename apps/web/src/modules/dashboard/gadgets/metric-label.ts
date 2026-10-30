/** Minimal translator surface used by the label helpers (the `dashboard` namespace translator). */
type LabelTranslator = ((key: string) => string) & { has: (key: string) => boolean };

/** "temperature_2m" -> "Temperature 2m": replace underscores and capitalise the first letter. */
function sentenceCase(id: string): string {
  const spaced = id.replace(/_/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Human label for a provider metric id (e.g. `fear_greed_index`).
 * Known ids resolve through the `metric_<id>` messages; anything else falls back to sentence case
 * so a raw snake_case identifier is never shown. Pure; no side effects.
 */
export function formatMetricLabel(id: string, t: LabelTranslator): string {
  const key = `metric_${id}`;
  return t.has(key) ? t(key) : sentenceCase(id);
}

/**
 * Human sentence for a backend reason/availability code (e.g. `method_data_license_unverified`).
 * Known codes resolve through `reason_<code>`; unknown codes use the generic `reason_unknown` sentence.
 */
export function formatReasonLabel(code: string, t: LabelTranslator): string {
  const key = `reason_${code}`;
  return t.has(key) ? t(key) : t('reason_unknown');
}
