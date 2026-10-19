/**
 * Standalone HTML export for the native chart widgets (`sandbox/chart-export.ts`).
 *
 * Design choice: upstream OpenIntelligentUI exports load chart.js from a CDN. Umwelt-OS must not
 * embed remote origins in files users share, so the export is fully self-contained: a hand-rolled
 * inline SVG (bars, lines, scatter points or donut slices) plus an accessible static `<table>` that
 * carries the exact data. No script, no remote URL; every interpolated string is HTML-escaped.
 */

import { THEME_CSS, type SandboxTheme } from '../design-system';
import { escapeHtml, escapeStyleClose } from './export-utils';

/** Plot kinds the exporter can draw (mirrors the chart widgets' `chartType` plus `pie`). */
export type ChartExportKind = 'pie' | 'bar' | 'line' | 'scatter' | 'distribution';

/** Input describing one chart; values are pre-normalized numbers, formatting is injected. */
export interface ChartExportInput {
  title: string;
  description?: string;
  source?: string;
  kind: ChartExportKind;
  /** Document language attribute (`en` / `vi`). */
  lang?: string;
  /** Localized label for the source footer. */
  sourceLabel?: string;
  /** Series display names and semantic color tokens (`var(--chart-N)`); pie uses a single series. */
  series: Array<{ name: string; color: string }>;
  /** One entry per category/slice; `values[i]` belongs to `series[i]`. */
  rows: Array<{ label: string; values: number[] }>;
  /** Locale-aware value formatter (usually `formatChartValue` bound to currency/unit/locale). */
  format: (value: number) => string;
  theme?: SandboxTheme;
}

const W = 480;
const H = 240;
const PAD = { l: 8, r: 8, t: 12, b: 12 };

/**
 * Restricts emitted colors to semantic chart tokens; anything else falls back by index.
 *
 * @param color - Candidate CSS color.
 * @param index - Palette fallback index.
 * @returns `var(--chart-N)` reference.
 */
function safeColor(color: string, index: number): string {
  return /^var\(--chart-[1-5]\)$/.test(color) ? color : `var(--chart-${(index % 5) + 1})`;
}

/** Rounds to 2 decimals so SVG attributes stay short and deterministic. */
const n = (v: number): string => String(Math.round(v * 100) / 100);

/**
 * Builds a donut as stroked arc paths (one per slice).
 *
 * @param input - Chart input; only `series[0]` values are used.
 * @returns SVG inner markup.
 */
function pieSvg(input: ChartExportInput): string {
  const vals = input.rows.map((r) => Math.max(0, r.values[0] ?? 0));
  const total = vals.reduce((a, b) => a + b, 0);
  if (total <= 0) return '';
  const cx = W / 2;
  const cy = H / 2;
  const r = 80;
  let angle = -Math.PI / 2;
  return vals
    .map((v, i) => {
      if (v <= 0) return '';
      const color = safeColor(input.series[0]?.color ?? '', i);
      const sweep = (v / total) * Math.PI * 2;
      // A full circle cannot be a single arc path; draw it as a circle element.
      if (sweep >= Math.PI * 2 - 1e-6) {
        return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="32"/>`;
      }
      const x1 = cx + r * Math.cos(angle);
      const y1 = cy + r * Math.sin(angle);
      angle += sweep;
      const x2 = cx + r * Math.cos(angle);
      const y2 = cy + r * Math.sin(angle);
      return `<path d="M${n(x1)} ${n(y1)}A${r} ${r} 0 ${sweep > Math.PI ? 1 : 0} 1 ${n(x2)} ${n(y2)}" fill="none" stroke="${color}" stroke-width="32"/>`;
    })
    .join('');
}

/**
 * Builds bars, lines or scatter points on a shared linear value axis that always includes zero.
 *
 * @param input - Chart input.
 * @returns SVG inner markup.
 */
function cartesianSvg(input: ChartExportInput): string {
  const all = input.rows.flatMap((r) => r.values).filter(Number.isFinite);
  const max = Math.max(0, ...all);
  const min = Math.min(0, ...all);
  const span = max - min || 1;
  const ih = H - PAD.t - PAD.b;
  const iw = W - PAD.l - PAD.r;
  const y = (v: number) => PAD.t + ih - ((v - min) / span) * ih;
  const slot = iw / Math.max(input.rows.length, 1);
  const x = (i: number) => PAD.l + slot * i + slot / 2;
  const base = y(0);
  const axis = `<line x1="${PAD.l}" y1="${n(base)}" x2="${W - PAD.r}" y2="${n(base)}" stroke="var(--line)"/>`;
  const gap = input.kind === 'distribution' ? 0 : slot * 0.2;
  const groupW = slot - gap;
  const barW = groupW / Math.max(input.series.length, 1);

  const parts = input.series.map((s, si) => {
    const color = safeColor(s.color, si);
    const pts = input.rows.map((r, i) => ({ x: x(i), v: r.values[si] ?? 0 }));
    if (input.kind === 'line') {
      const d = pts.map((p, i) => `${i ? 'L' : 'M'}${n(p.x)} ${n(y(p.v))}`).join('');
      return `<path d="${d}" fill="none" stroke="${color}" stroke-width="2"/>`;
    }
    if (input.kind === 'scatter') {
      return pts.map((p) => `<circle cx="${n(p.x)}" cy="${n(y(p.v))}" r="4" fill="${color}"/>`).join('');
    }
    return pts
      .map((p) => {
        const bx = p.x - groupW / 2 + barW * si;
        const h = Math.abs(y(p.v) - base);
        return `<rect x="${n(bx)}" y="${n(Math.min(y(p.v), base))}" width="${n(barW)}" height="${n(h)}" fill="${color}"/>`;
      })
      .join('');
  });
  return axis + parts.join('');
}

const EXPORT_CSS = `body{margin:0;padding:24px;background:var(--surface);color:var(--text);font-family:var(--font-sans)}
main{max-width:640px;margin:0 auto}h1{font-size:18px;margin:0 0 4px}p{margin:0 0 12px;color:var(--muted);font-size:13px}
svg{width:100%;height:auto;display:block;border:1px solid var(--line);border-radius:10px}
ul{list-style:none;display:flex;flex-wrap:wrap;gap:12px;padding:0;margin:12px 0;font-size:12px}
.sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
footer{margin-top:12px;font-size:11px;color:var(--muted)}`;

/**
 * Renders a chart as a self-contained, script-free HTML document (inline SVG + data table).
 *
 * @param input - Chart description (see `ChartExportInput`).
 * @returns Complete `<!DOCTYPE html>` string with no remote URLs; all text is HTML-escaped.
 */
export function chartToStandaloneHtml(input: ChartExportInput): string {
  const theme = input.theme ?? 'light';
  const isPie = input.kind === 'pie';
  const svg = isPie ? pieSvg(input) : cartesianSvg(input);
  const head = input.series.map((s) => `<th scope="col">${escapeHtml(s.name)}</th>`).join('');
  const body = input.rows
    .map((r) => {
      const cells = input.series
        .map((_, i) => `<td>${escapeHtml(input.format(r.values[i] ?? 0))}</td>`)
        .join('');
      return `<tr><th scope="row">${escapeHtml(r.label)}</th>${cells}</tr>`;
    })
    .join('');
  const legendLabels = isPie ? input.rows.map((r) => r.label) : input.series.map((s) => s.name);
  const legend = legendLabels
    .map((label, i) => {
      const color = safeColor(input.series[isPie ? 0 : i]?.color ?? '', i);
      return `<li><span class="sw" style="background:${color}"></span>${escapeHtml(label)}</li>`;
    })
    .join('');
  return `<!DOCTYPE html>
<html lang="${escapeHtml(input.lang ?? 'en')}" data-theme="${theme}"${theme === 'dark' ? ' class="dark"' : ''}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(input.title)}</title>
<style>
${escapeStyleClose(THEME_CSS)}
${escapeStyleClose(EXPORT_CSS)}
</style>
</head>
<body>
<main>
<h1>${escapeHtml(input.title)}</h1>${input.description ? `\n<p>${escapeHtml(input.description)}</p>` : ''}
<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(input.title)}">${svg}</svg>
<ul>${legend}</ul>
<table><thead><tr><th scope="col"></th>${head}</tr></thead><tbody>${body}</tbody></table>${
    input.source ? `\n<footer>${escapeHtml(input.sourceLabel ?? 'Source')}: ${escapeHtml(input.source)}</footer>` : ''
  }
</main>
</body>
</html>`;
}
