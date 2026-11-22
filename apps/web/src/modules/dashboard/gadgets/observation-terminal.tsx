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
 * `props.spark` the history chart element (or null with fewer than two points); optional `props.chips` a control row between figure and chart.
 */
export function TerminalObservationFigure({
  label,
  figure,
  change,
  spark,
  chips,
}: {
  label: string;
  figure: string;
  change: ReturnType<typeof formatTerminalChange>;
  spark: ReactNode;
  chips?: ReactNode;
}) {
  return (
    <div className="obs-term-figure-box">
      <p className="obs-term-figure">
        <span className="sr-only">{label}</span>
        {figure}
        <small data-tone={change.tone}>{change.text}</small>
      </p>
      {chips}
      {spark}
    </div>
  );
}

/** Terminal sparkline-row layout is used up to this many series; more series read better as the `SYMBOL LAST CHG` table. */
export const TERMINAL_SPARK_ROWS_MAX = 3;

/**
 * Chooses the terminal layout of an observation gadget from its series count: one series is the chart figure, two to
 * {@link TERMINAL_SPARK_ROWS_MAX} are sparkline rows (small metric panes, demo `metric`), more are the watchlist table
 * (the Overview watchlist). Pure.
 *
 * @param count Number of series shown.
 * @returns `figure`, `sparkrows` or `table`.
 */
export function terminalObservationLayout(count: number): 'figure' | 'sparkrows' | 'table' {
  if (count <= 1) return 'figure';
  return count <= TERMINAL_SPARK_ROWS_MAX ? 'sparkrows' : 'table';
}

/**
 * Compact reading for a sparkline row (`71.3K`, `20.5°`): thousands abbreviated, one fraction digit, a degree sign for
 * temperature units and no other unit text.
 *
 * @param value Reading; non-finite yields an em dash.
 * @param unit Provider unit (only a leading degree sign is kept).
 * @param locale Display locale.
 */
export function formatTerminalCompact(value: number | null, unit: string | null | undefined, locale: AppLocaleId): string {
  if (value === null || !Number.isFinite(value)) return '—';
  const text = new Intl.NumberFormat(normalizeFormattingLocale(locale), {
    notation: Math.abs(value) >= 1000 ? 'compact' : 'standard',
    maximumFractionDigits: 1,
  }).format(value);
  return unit?.startsWith('°') ? `${text}°` : text;
}

/**
 * Signed percent without an arrow (`+3.1%` / `−1.4%`) for sparkline rows; tone as {@link formatTerminalChange}.
 *
 * @param percent Percent change; null/non-finite renders an em dash.
 * @param better Which direction is good for this metric.
 */
export function formatTerminalPercent(
  percent: number | null | undefined,
  better: BetterDirection,
): { text: string; tone: 'positive' | 'negative' | 'neutral' } {
  const full = formatTerminalChange(percent, better);
  return { ...full, text: full.text.replace(/^[▲▼] /, '').replace(/(\d+\.\d)\d%$/, '$1%') };
}

/**
 * Currency symbol for a chart figure (`$` for USD); empty for a missing or unknown code.
 *
 * @param currency ISO 4217 code or null.
 * @param locale Display locale.
 */
export function currencyPrefix(currency: string | null | undefined, locale: AppLocaleId): string {
  if (!currency || !/^[A-Z]{3}$/.test(currency)) return '';
  try {
    const parts = new Intl.NumberFormat(normalizeFormattingLocale(locale), {
      style: 'currency',
      currency,
      currencyDisplay: 'narrowSymbol',
    }).formatToParts(0);
    return parts.find((part) => part.type === 'currency')?.value ?? '';
  } catch {
    return '';
  }
}

/**
 * One sparkline row (demo `metric` pane): `name  ~line~  71.3K  +3.1%`.
 *
 * @param props.label Series label; `props.spark` the sparkline element; `props.last` the compact value;
 * `props.change` the output of {@link formatTerminalPercent}.
 */
export function TerminalSparkRow({
  label,
  spark,
  last,
  change,
}: {
  label: string;
  spark: ReactNode;
  last: string;
  change: ReturnType<typeof formatTerminalPercent>;
}) {
  return (
    <div className="obs-spark-row">
      <span className="obs-term-name">{label}</span>
      <span className="obs-spark-line">{spark}</span>
      <span className="obs-term-last">{last}</span>
      <span className="obs-term-chg" data-tone={change.tone}>
        {change.text}
      </span>
    </div>
  );
}
