/**
 * Standalone HTML document builder and download utilities (`sandbox/export-utils.ts`).
 * Packages a generative UI widget (`css`, `html`, `jsFunctions`, `jsExpressions`) together
 * with the Umwelt-OS + OpenIntelligentUI design system, importmap, TripAnimator, and
 * graceful bridge stubs so exported `.html` files work offline in any browser.
 */

import {
  buildSandboxStylesheet,
  IMPORTMAP_SCRIPT_TAG,
  type SandboxTheme,
} from '../design-system';
import { TRIP_ANIMATOR_SCRIPT } from './trip-animator';

export interface StandaloneActivityContent {
  css?: string;
  html?: string | string[];
  jsFunctions?: string;
  jsExpressions?: string | string[];
  title?: string;
}

const STANDALONE_BRIDGE_STUB = `window.sendPrompt = async () => ({ ok: true }); window.Websandbox = { connection: { remote: { sendPrompt: async () => ({ ok: true }), openLink: async ({ url } = {}) => { if (typeof url === "string" && /^https:/i.test(url)) window.open(url, "_blank", "noopener,noreferrer"); } } } };`;

/**
 * Escapes closing `</script` tags inside embedded JavaScript code.
 *
 * @param js - Raw JavaScript source string.
 * @returns Escaped JavaScript string safe for inline `<script>` tags.
 */
export function escapeScriptClose(js: string): string {
  return js.replace(/<\/script/gi, '<\\/script');
}

/**
 * Escapes closing `</style` tags inside embedded CSS rules.
 *
 * @param css - Raw CSS source string.
 * @returns Escaped CSS string safe for inline `<style>` tags.
 */
export function escapeStyleClose(css: string): string {
  return css.replace(/<\/style/gi, '<\\/style');
}

/**
 * Escapes special HTML characters (`&`, `<`, `>`, `"`) for safe text/title interpolation.
 *
 * @param text - Raw text string.
 * @returns HTML-escaped string.
 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Converts a widget title into a URL/filename-safe lowercase slug.
 *
 * @param text - Raw widget title.
 * @returns Hyphenated lowercase slug (or empty string if no alphanumeric characters exist).
 */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Assembles a self-contained HTML5 document from sandboxed widget arguments (`SandboxedUiArgs`
 * or `StandaloneActivityContent`).
 *
 * Ordering invariants:
 * 1. `<script type="importmap">` first in `<head>` so ES module specifiers resolve
 * 2. `TRIP_ANIMATOR_SCRIPT` (`window.createTripAnimator`)
 * 3. Umwelt-OS + OpenIntelligentUI Design System CSS (`buildSandboxStylesheet(theme)`)
 * 4. Widget-specific `css` (with `</style` neutralized)
 * 5. Graceful `window.sendPrompt` & `window.Websandbox` bridge stubs
 * 6. HTML body markup after `</head>`
 * 7. Classic `<script>` with `jsFunctions` at top level (so inline `onclick="fn()"` handlers work)
 *    followed by `jsExpressions` inside an `async` IIFE.
 *
 * @param content - Widget content (`css`, `html`, `jsFunctions`, `jsExpressions`).
 * @param title - Document `<title>` (defaults to `content.title` or `'generated-widget'`).
 * @param theme - Active Umwelt-OS theme (`'light'` or `'dark'`). Defaults to `'light'`.
 * @returns Complete standalone `<!DOCTYPE html>` document string.
 */
export function buildStandaloneHtmlDocument(
  content: StandaloneActivityContent,
  title?: string,
  theme: SandboxTheme = 'light',
): string {
  const resolvedTitle = title ?? content.title ?? 'generated-widget';
  const body = Array.isArray(content.html)
    ? content.html.join('')
    : (content.html ?? '');

  const expressionLines = Array.isArray(content.jsExpressions)
    ? content.jsExpressions.filter((line) => line.trim().length > 0)
    : typeof content.jsExpressions === 'string' && content.jsExpressions.trim().length > 0
      ? [content.jsExpressions]
      : [];

  const scriptParts = [
    ...(content.jsFunctions && content.jsFunctions.trim().length > 0
      ? [escapeScriptClose(content.jsFunctions)]
      : []),
    ...(expressionLines.length > 0
      ? [
          `(async () => {\n${expressionLines.map(escapeScriptClose).join('\n')}\n})();`,
        ]
      : []),
  ];

  const generatedScript =
    scriptParts.length > 0
      ? `\n  <script>\n${scriptParts.join('\n')}\n  </script>`
      : '';

  const themeClass = theme === 'dark' ? ' class="dark"' : '';

  return `<!DOCTYPE html>
<html lang="en" data-theme="${theme}"${themeClass}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(resolvedTitle)}</title>
  ${IMPORTMAP_SCRIPT_TAG}
  ${TRIP_ANIMATOR_SCRIPT}
  <style>
${escapeStyleClose(buildSandboxStylesheet(theme))}
  </style>${
    content.css
      ? `\n  <style>\n${escapeStyleClose(content.css)}\n  </style>`
      : ''
  }
  <script>${STANDALONE_BRIDGE_STUB}</script>
</head>
<body>
${body}${generatedScript}
</body>
</html>`;
}

/** Alias for `buildStandaloneHtmlDocument` matching OpenIntelligentUI naming. */
export const assembleStandaloneHtmlFromActivity = buildStandaloneHtmlDocument;

/**
 * Triggers a browser file download for a standalone HTML document string.
 * Creates an ephemeral `Blob` (`text/html;charset=utf-8`), clicks a temporary `<a download>`,
 * and immediately revokes the object URL.
 *
 * @param htmlString - Complete HTML document string to download.
 * @param filename - Target filename or title (automatically suffixed with `.html`).
 */
export function downloadHtmlDocument(
  htmlString: string,
  filename = 'generated-widget.html',
): void {
  const baseSlug = filename.endsWith('.html')
    ? slugify(filename.slice(0, -5)) || 'generated-widget'
    : slugify(filename) || 'generated-widget';
  const normalizedFilename = `${baseSlug}.html`;

  const blob = new Blob([htmlString], { type: 'text/html;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = normalizedFilename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

/** Alias for `downloadHtmlDocument` matching OpenIntelligentUI naming. */
export const triggerDownload = downloadHtmlDocument;
