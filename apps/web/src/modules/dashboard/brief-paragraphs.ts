/** One inline piece of a brief paragraph: plain text or a `[n]` citation marker. */
export type BriefPart = { kind: 'text'; value: string } | { kind: 'cite'; ref: number };

/** One brief paragraph: an optional bold lead word(s) (`Markets.`) and the remaining inline parts. */
export interface BriefParagraph {
  /** Leading label including its period, or null when the paragraph does not start with one. */
  lead: string | null;
  /** Remaining text and citation markers in reading order. */
  parts: BriefPart[];
}

/** One or two capitalised words followed by a period and a space, e.g. `Markets.` or `Day plan.`. */
const LEAD = /^([A-Z][A-Za-z]*(?: [A-Za-z]+)?\.)\s+/;

/**
 * Splits a saved brief into paragraphs (blank-line separated), peeling a short lead label and turning `[n]` markers
 * into citation parts. Pure; the text is never altered other than the split, so concatenating `lead`, the text
 * values and the `[ref]` markers reproduces each paragraph.
 *
 * @param content Brief text as stored.
 * @returns One entry per non-empty paragraph.
 */
export function parseBriefParagraphs(content: string): BriefParagraph[] {
  return content
    .split(/\n{2,}/)
    .map((raw) => raw.trim())
    .filter(Boolean)
    .map((raw) => {
      const lead = LEAD.exec(raw);
      const rest = lead ? raw.slice(lead[0].length) : raw;
      const parts: BriefPart[] = [];
      let last = 0;
      for (const match of rest.matchAll(/\[(\d{1,3})\]/g)) {
        if (match.index > last) parts.push({ kind: 'text', value: rest.slice(last, match.index) });
        parts.push({ kind: 'cite', ref: Number(match[1]) });
        last = match.index + match[0].length;
      }
      if (last < rest.length) parts.push({ kind: 'text', value: rest.slice(last) });
      return { lead: lead ? lead[1] : null, parts };
    });
}
