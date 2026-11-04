import DOMPurify from 'dompurify';

let cachedPurifier: ReturnType<typeof DOMPurify> | null = null;

/** Returns the browser-bound DOMPurify singleton (created lazily so SSR never touches `window`). */
function getPurifier(): ReturnType<typeof DOMPurify> {
  if (!cachedPurifier) cachedPurifier = DOMPurify(window);
  return cachedPurifier;
}

/**
 * Defence-in-depth sanitizer for the policy HTML that `loadPolicy` renders from repository Markdown (html disabled).
 * Keeps the standard HTML profile (headings, lists, tables, links, spans, code) and, deliberately, heading `id`s as
 * written: DOM-clobbering protection is off because the ids are generated anchors such as `#location` that the
 * documents link to, while scripts, event handlers and `javascript:` URLs are still removed.
 *
 * On the server (no `window`, no DOM implementation shipped) the markup is returned unchanged: it is the output of the
 * `html: false` markdown-it renderer, which already escapes raw HTML and rejects unsafe link schemes. The browser
 * sanitizes again whenever the component renders there (hydration and client navigation). The DOMPurify guarantee is
 * therefore client-side only; server-rendered HTML relies on the `html: false` escaping of the Markdown renderer.
 *
 * @param html Rendered policy HTML.
 * @returns Sanitized HTML string.
 */
export function sanitizePolicyHtml(html: string): string {
  if (typeof window === 'undefined') return html;
  return getPurifier().sanitize(html, {
    USE_PROFILES: { html: true },
    SANITIZE_DOM: false,
    SANITIZE_NAMED_PROPS: false,
  });
}
