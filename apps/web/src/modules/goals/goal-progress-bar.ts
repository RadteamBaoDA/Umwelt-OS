/**
 * Renders a goal's progress as a 10-cell ASCII bar for the terminal skin, e.g. `[████████░░] 83%`.
 * The percentage is clamped to 0-100 (non-finite input counts as 0) before both the cell count and the
 * printed value are derived, so the bar can never overflow its brackets.
 *
 * @param progress Goal progress percentage; any number is accepted and clamped.
 * @param cells Number of cells between the brackets (default 10).
 * @returns The bar text, including the rounded percentage. Pure: no side effects.
 */
export function asciiProgressBar(progress: number, cells = 10): string {
  const pct = Number.isFinite(progress) ? Math.min(100, Math.max(0, progress)) : 0;
  const filled = Math.round((pct / 100) * cells);
  return `[${'█'.repeat(filled)}${'░'.repeat(cells - filled)}] ${Math.round(pct)}%`;
}
