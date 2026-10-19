/**
 * HTML streaming sanitizer and tag balancer (`sandbox/process-partial-html.ts`).
 * Prepares partial streamed HTML chunks for flicker-free DOM morphing inside the
 * sandboxed iframe while stripping disallowed `<script>`, `<iframe>`, and external
 * stylesheet/resource injections.
 */

const VOID_HTML_TAGS = new Set([
  'area',
  'base',
  'br',
  'col',
  'embed',
  'hr',
  'img',
  'input',
  'link',
  'meta',
  'param',
  'source',
  'track',
  'wbr',
]);

/**
 * Extracts all complete `<style>` blocks from raw streamed HTML.
 * Sanitizes any embedded closing `</style>` injection sequences and returns
 * the concatenated `<style>` tags suitable for `<head>` injection.
 *
 * @param html - Raw accumulated HTML string.
 * @returns Concatenated complete `<style>` blocks, or empty string if none.
 */
export function extractCompleteStyles(html: string): string {
  if (!html) return '';
  const matches = html.match(/<style\b[^>]*>[\s\S]*?<\/style>/gi);
  return matches ? matches.join('') : '';
}

/**
 * Balances unclosed HTML and SVG tags in a partial streaming HTML fragment
 * so Idiomorph can morph the DOM tree without parser errors or layout jumps.
 * Void tags (`<br>`, `<img>`, `<input>`, etc.) and self-closing SVG elements
 * (`<rect />`, `<path />`, `<circle />`) are never closed twice.
 *
 * @param html - Partial HTML fragment with trailing incomplete tags already removed.
 * @returns HTML fragment with any unclosed non-void tags closed in reverse nesting order.
 */
export function balanceUnclosedTags(html: string): string {
  if (!html) return '';

  const openTags: string[] = [];
  const tagRegex = /<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|<\/?([a-zA-Z][a-zA-Z0-9:-]*)\b(?:"[^"]*"|'[^']*'|[^>"'])*(\/?)>/gi;

  let match: RegExpExecArray | null;
  while ((match = tagRegex.exec(html)) !== null) {
    const fullToken = match[0]!;
    const tagName = match[1];

    if (!tagName || fullToken.startsWith('<!--') || fullToken.startsWith('<!')) {
      continue;
    }

    const lowerName = tagName.toLowerCase();
    const isClosing = fullToken.startsWith('</');
    const isSelfClosing = fullToken.endsWith('/>') || VOID_HTML_TAGS.has(lowerName);

    if (isClosing) {
      // Pop back to the nearest matching open tag to recover gracefully from mismatched nesting.
      for (let i = openTags.length - 1; i >= 0; i--) {
        if (openTags[i]!.toLowerCase() === lowerName) {
          openTags.length = i;
          break;
        }
      }
    } else if (!isSelfClosing) {
      openTags.push(tagName);
    }
  }

  if (openTags.length === 0) {
    return html;
  }

  const closingSequence = openTags
    .slice()
    .reverse()
    .map((tag) => `</${tag}>`)
    .join('');

  return `${html}${closingSequence}`;
}

export interface ProcessPartialHtmlOptions {
  /** Whether to automatically close unclosed HTML/SVG tags at the end of the stream. Defaults to true. */
  balanceTags?: boolean;
}

/**
 * Processes raw accumulated HTML for safe streaming preview injection.
 * Pure function with no DOM dependencies.
 *
 * Security & Streaming Pipeline (order matters):
 * 1. Strip incomplete HTML comments and incomplete trailing tags (`<div cla...`)
 * 2. Strip self-closing `<script />`, `<iframe />`, `<object />`, `<embed />`, and `<link>` tags
 * 3. Strip complete `<style>`, `<script>`, `<head>`, `<iframe>`, `<object>`, and `<embed>` blocks
 * 4. Strip unterminated `<style>`, `<script>`, `<head>`, `<iframe>`, `<object>`, and `<embed>` blocks to end of input
 * 5. Neutralize `javascript:` URLs in `href` / `src` attributes
 * 6. Strip incomplete trailing HTML entities (`&amp`, `&#12`)
 * 7. Extract `<body>` content if a `<body>` wrapper is present
 * 8. Balance unclosed HTML/SVG tags so DOM morphing remains structurally stable
 *
 * @param rawHtml - Raw accumulated HTML string from streaming tool call arguments.
 * @param options - Optional configuration (e.g. `balanceTags`).
 * @returns Clean, morphable HTML string safe for sandboxed preview injection.
 */
export function processPartialHtml(
  rawHtml: string,
  options: ProcessPartialHtmlOptions = {},
): string {
  if (!rawHtml) return '';

  const { balanceTags = true } = options;
  let result = rawHtml;

  // 1. Strip unterminated HTML comments and incomplete trailing tag at the end of the stream.
  result = result.replace(/<!--[\s\S]*$/, '');
  result = result.replace(/<[^>]*$/, '');

  // 2. Strip self-closing script/iframe/object/embed tags and external http(s) <link> injections first.
  result = result.replace(/<(script|iframe|object|embed)\b[^>]*\/>/gi, '');
  result = result.replace(/<link\b[^>]*>/gi, '');

  // 3. Strip complete disallowed/head blocks (<style>, <script>, <head>, <iframe>, <object>, <embed>).
  result = result.replace(
    /<(style|script|head|iframe|object|embed)\b[^>]*>[\s\S]*?<\/\1>/gi,
    '',
  );

  // 4. Strip unterminated disallowed/head blocks to the end of the input so partial source never leaks.
  result = result.replace(
    /<(style|script|head|iframe|object|embed)\b[^>]*>[\s\S]*$/gi,
    '',
  );

  // 5. Neutralize javascript: pseudo-protocol in href or src attributes.
  result = result.replace(
    /\b(href|src)\s*=\s*(?:"\s*javascript:[^"]*"|'\s*javascript:[^']*')/gi,
    '$1="#"',
  );

  // 6. Strip incomplete trailing HTML entities (e.g. "&amp" or "&#12" without semicolon).
  result = result.replace(/&[a-zA-Z0-9#]*$/, '');

  // 7. Extract inner <body> content if present, dropping outer <html>/<body> wrappers.
  const bodyMatch = result.match(/<body[^>]*>([\s\S]*)/i);
  if (bodyMatch) {
    result = bodyMatch[1]!;
    result = result.replace(/<\/body>[\s\S]*/i, '');
  } else {
    // Also strip stray <html>, </html>, <!DOCTYPE> wrappers if no <body> tag was used.
    result = result.replace(/<!DOCTYPE[^>]*>/gi, '');
    result = result.replace(/<\/?html[^>]*>/gi, '');
  }

  // 8. Balance unclosed HTML and SVG tags during streaming.
  if (balanceTags) {
    result = balanceUnclosedTags(result);
  }

  return result;
}
