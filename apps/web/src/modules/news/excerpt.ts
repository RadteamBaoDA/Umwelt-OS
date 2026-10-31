/** Collapse text to lowercase alphanumerics so title matching ignores case, spacing and punctuation. */
const fold = (value: string): string => value.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');

/**
 * Turn a stored excerpt into plain display text: strips Markdown (headings anywhere, emphasis,
 * backticks, links, images, bullets), collapses whitespace, drops a leading release label
 * ("Tag:", "Release:", "Version:") and then a leading copy of the title (or just its identifier-like first token, e.g. a CVE id).
 * Pure; never throws.
 * @param title Headline already rendered above the excerpt.
 * @param excerpt Stored excerpt, possibly Markdown.
 * @returns Cleaned text, or null when nothing meaningful remains.
 */
export function cleanExcerpt(title: string, excerpt: string | null | undefined): string | null {
  let text = (excerpt ?? '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^[ \t]*(?:[-*+][ \t]+|>[ \t]?)/gm, '')
    .replace(/[*_`]+/g, '')
    .replace(/(^|\s)#{1,6}\s+/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^(?:tag|release|version)\s*:\s*/i, '');
  const head = fold(title);
  if (head && fold(text).startsWith(head)) {
    // Walk the original text until the title's folded characters are consumed.
    let seen = 0;
    let i = 0;
    while (i < text.length && seen < head.length) {
      if (fold(text[i])) seen += 1;
      i += 1;
    }
    text = text.slice(i).replace(/^[\s.:!?\-–—…]+/, '');
  }
  else {
    // Identifier-like lead token (e.g. "CVE-2026-91920") repeated from a longer title.
    const token = title.trim().split(/\s+/)[0] ?? '';
    if (token.length >= 6 && /\d/.test(token) && text.startsWith(token)) {
      text = text.slice(token.length).replace(/^[\s.:!?\-–—…]+/, '');
    }
  }
  return text.trim() || null;
}
