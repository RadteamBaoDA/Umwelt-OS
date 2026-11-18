'use client';

import type { ReactNode } from 'react';
import { normalizeFormattingLocale, type AppLocaleId } from '@/core/i18n';
import type { BetterDirection } from './metric-direction';

/**
 * Full-precision plain number for the terminal watchlist table (`71,340.20`): two fraction digits, four for
 * magnitudes under 1 so small prices stay readable. No currency symbol or compaction; the column header carries the meaning.
 *
 * @param value Finite value to format; non-finite input yields an em dash.
 * @param locale Display locale (normalised to its BCP 47 formatting tag).
 * @param whole When true, values of 1,000 and above drop the fraction (the chart figure `71,340`).
 */
export function formatTerminalValue(value: number | null, locale: AppLocaleId, whole = false): string {
  if (value === null || !Number.isFinite(value)) return '—';
  const noFraction = whole && Math.abs(value) >= 1000;
  return new Intl.NumberFormat(normalizeFormattingLocale(locale), {
    minimumFractionDigits: noFraction ? 0 : 2,
    maximumFractionDigits: noFraction ? 0 : Math.abs(value) < 1 && value !== 0 ? 4 : 2,
  }).format(value);
}

/**
 * Signed change cell `▲ +3.12%` / `▼ −0.62%` with a colour tone: good moves positive, bad negative; metrics with no
 * good direction colour by sign (up positive, down negative); flat or unknown changes are muted. The arrow and sign
 * carry the direction, so colour is never the only cue.
 *
 * @param percent Percent change; null/non-finite renders an em dash.
 * @param better Which direction is good for this metric (`neutral` colours by sign).
 * @returns Text and tone name for `data-tone`.
 */
export function formatTerminalChange(
  percent: number | null | undefined,
  better: BetterDirection,
): { text: string; tone: 'positive' | 'negative' | 'neutral' } {
  if (percent === null || percent === undefined || !Number.isFinite(percent)) return { text: '—', tone: 'neutral' };
  if (percent === 0) return { text: '0.00%', tone: 'neutral' };
  const up = percent > 0;
  const text = `${up ? '▲ +' : '▼ −'}${Math.abs(percent).toFixed(2)}%`;
  // Neutral-direction metrics (prices) colour by sign as in the demo table: the arrow already carries the direction.
  if (better === 'neutral') return { text, tone: up ? 'positive' : 'negative' };
  return { text, tone: (up ? better === 'up' : better === 'down') ? 'positive' : 'negative' };
}

/**
 * Terminal watchlist row: `NAME  LAST  ▲ CHG` as three cells of the `.obs-term-row` grid (header and rows share it).
 *
 * @param props.label Series label; `props.last` the formatted value; `props.change` the output of {@link formatTerminalChange}.
 */
export function TerminalObservationCells({
  label,
  last,
  change,
}: {
  label: string;
  last: string;
  change: ReturnType<typeof formatTerminalChange>;
}) {
  return (
    <div className="obs-term-row">
      <span className="obs-term-name">{label}</span>
      <span className="obs-term-last">{last}</span>
      <span className="obs-term-chg" data-tone={change.tone}>
        {change.text}
      </span>
    </div>
  );
}

/**
 * Terminal single-series figure (demo chart pane): `71,340 ▲ +3.12%` at 24px with the history line below. The series
 * label stays as screen-reader text because the gadget title already names the series visually.
 *
 * @param props.label Series label; `props.figure` the formatted value; `props.change` the formatted change;
 * `props.spark` the history chart element (or null with fewer than two points).
 */
export function TerminalObservationFigure({
  label,
  figure,
  change,
  spark,
}: {
  label: string;
  figure: string;
  change: ReturnType<typeof formatTerminalChange>;
  spark: ReactNode;
}) {
  return (
    <div className="obs-term-figure-box">
      <p className="obs-term-figure">
        <span className="sr-only">{label}</span>
        {figure}
        <small data-tone={change.tone}>{change.text}</small>
      </p>
      {spark}
    </div>
  );
}
