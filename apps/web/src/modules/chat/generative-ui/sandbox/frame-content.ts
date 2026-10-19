/**
 * Sandboxed iframe document builder (`sandbox/frame-content.ts`).
 * Assembles the self-contained HTML document injected into `<iframe sandbox="allow-scripts">`,
 * combining strict Content-Security-Policy enforcement, the Umwelt-OS + OpenIntelligentUI
 * design system CSS, Idiomorph DOM morphing, `ResizeObserver` height reporting, and the
 * `window.sendPrompt` postMessage bridge.
 */

import {
  buildSandboxStylesheet,
  DESIGN_SYSTEM_CSS,
  IMPORTMAP_SCRIPT_TAG,
  type SandboxTheme,
} from '../design-system';
import { IDIOMORPH_INLINE_SCRIPT } from './idiomorph-inline';
import { processPartialHtml } from './process-partial-html';
import { TRIP_ANIMATOR_SCRIPT } from './trip-animator';

/** Message type emitted by the sandboxed iframe when its content height changes. */
export const RESIZE_MESSAGE_TYPE = 'umwelt:resize';

/** Legacy resize message type alias for OpenIntelligentUI compatibility. */
export const LEGACY_RESIZE_MESSAGE_TYPE = '__ogui_resize';

/** Minimal preview shell used when initializing an empty sandboxed frame. */
export const PREVIEW_FRAME_CONTENT = '<head></head><body><div id="umwelt-sandbox-root"></div></body>';

/**
 * Strict Content-Security-Policy meta tag enforced inside the sandboxed iframe.
 * Blocks all external network fetches (`default-src 'none'`), permits only inline styles
 * and scripts injected by the sandbox controller, and restricts images/fonts to `data:` and `blob:`.
 */
export const CSP_META_TAG =
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; img-src data: blob:; font-src data:;">';

/**
 * Escapes closing `</style` sequences inside CSS strings to prevent tag breakout.
 *
 * @param css - Raw CSS string.
 * @returns Escaped CSS string safe for inline `<style>` embedding.
 */
export function escapeStyleTagContent(css: string): string {
  return css.replace(/<\/style/gi, '<\\/style');
}

/**
 * Escapes closing `</script` sequences inside JS strings to prevent tag breakout.
 *
 * @param js - Raw JavaScript string.
 * @returns Escaped JavaScript string safe for inline `<script>` embedding.
 */
export function escapeScriptTagContent(js: string): string {
  return js.replace(/<\/script/gi, '<\\/script');
}

/**
 * Ensures an HTML fragment contains a `<head>` element so head styles/scripts can be inserted.
 *
 * @param html - Raw HTML string.
 * @returns HTML string guaranteed to contain a `<head>` tag.
 */
export function ensureHead(html: string): string {
  if (/<head[\s>]/i.test(html)) return html;
  return `<head></head>${html}`;
}

/**
 * Continuous autosize script injected into the sandboxed iframe.
 * Observes `document.body` via `ResizeObserver` and posts `{ type: 'umwelt:resize', height }`
 * to the host window whenever content dimensions change.
 */
export const MEASUREMENT_JS = `
(function() {
  if (window.__umweltResizeInstalled) return;
  window.__umweltResizeInstalled = true;
  function reportHeight() {
    if (!document.body) return;
    var root = document.getElementById('umwelt-sandbox-root') || document.body;
    var h = Math.max(root.scrollHeight || 0, document.body.scrollHeight || 0);
    var cs = window.getComputedStyle ? getComputedStyle(document.body) : null;
    if (cs) {
      h += (parseFloat(cs.marginTop) || 0) + (parseFloat(cs.marginBottom) || 0);
      h += (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    }
    var reported = Math.ceil(h);
    if (reported > 0 && window.parent && window.parent !== window) {
      window.parent.postMessage({ type: '${RESIZE_MESSAGE_TYPE}', height: reported }, '*');
    }
  }
  window.__umweltReportHeight = reportHeight;
  if (typeof ResizeObserver !== 'undefined' && document.body) {
    var ro = new ResizeObserver(reportHeight);
    ro.observe(document.body);
    var rootEl = document.getElementById('umwelt-sandbox-root');
    if (rootEl) ro.observe(rootEl);
  }
  window.addEventListener('resize', reportHeight);
  window.addEventListener('load', reportHeight);
  reportHeight();
})();
`.trim();

/**
 * Inline bridge script providing `window.sendPrompt(prompt)` and the `window.Websandbox`
 * compatibility object inside the sandboxed iframe, plus message listeners for live
 * DOM morphing (`umwelt:morph`), script execution (`umwelt:exec-js`), and theme synchronization (`umwelt:theme`).
 */
export const SANDBOX_BRIDGE_JS = `
(function() {
  if (window.__umweltBridgeInstalled) return;
  window.__umweltBridgeInstalled = true;

  function extractPromptText(input) {
    if (typeof input === 'string') return input.trim();
    if (input && typeof input === 'object') {
      if (typeof input.prompt === 'string') return input.prompt.trim();
      if (typeof input.text === 'string') return input.text.trim();
    }
    return '';
  }

  window.sendPrompt = function(input) {
    var text = extractPromptText(input);
    if (!text) return Promise.resolve({ ok: false });
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({
        type: 'umwelt:send-prompt',
        prompt: text,
        text: text
      }, '*');
    }
    return Promise.resolve({ ok: true });
  };

  window.Websandbox = {
    connection: {
      remote: {
        sendPrompt: function(args) {
          return window.sendPrompt(args);
        },
        openLink: function(args) {
          var url = args && typeof args === 'object' ? args.url : args;
          if (typeof url === 'string' && /^https:/i.test(url) && window.parent && window.parent !== window) {
            window.parent.postMessage({ type: 'umwelt:open-link', url: url }, '*');
          }
          return Promise.resolve({ ok: true });
        }
      }
    }
  };

  window.addEventListener('message', function(event) {
    var data = event.data;
    if (!data || typeof data !== 'object') return;

    if (data.type === 'umwelt:theme' && (data.theme === 'light' || data.theme === 'dark')) {
      document.documentElement.setAttribute('data-theme', data.theme);
      if (data.theme === 'dark') {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
      if (typeof data.stylesheet === 'string') {
        var dsStyle = document.getElementById('umwelt-design-system');
        if (dsStyle) dsStyle.textContent = data.stylesheet;
      }
      if (typeof window.__umweltReportHeight === 'function') {
        window.__umweltReportHeight();
      }
    } else if (data.type === 'umwelt:morph') {
      if (typeof data.css === 'string') {
        var customStyle = document.getElementById('umwelt-widget-css');
        if (!customStyle) {
          customStyle = document.createElement('style');
          customStyle.id = 'umwelt-widget-css';
          document.head.appendChild(customStyle);
        }
        customStyle.textContent = data.css;
      }
      var target = document.getElementById('umwelt-sandbox-root') || document.body;
      var nextHtml = typeof data.html === 'string' ? data.html : '';
      if (typeof Idiomorph !== 'undefined' && Idiomorph && typeof Idiomorph.morph === 'function') {
        try {
          Idiomorph.morph(target, nextHtml, {
            morphStyle: 'innerHTML',
            callbacks: {
              beforeNodeAdded: function(node) {
                if (node && node.nodeType === 1 && node.classList) {
                  node.classList.add('morph-enter');
                  node.addEventListener('animationend', function() {
                    node.classList.remove('morph-enter');
                  }, { once: true });
                }
              }
            }
          });
        } catch (_err) {
          target.innerHTML = nextHtml;
        }
      } else {
        target.innerHTML = nextHtml;
      }
      if (typeof window.__umweltReportHeight === 'function') {
        window.__umweltReportHeight();
      }
    } else if (data.type === 'umwelt:exec-js') {
      try {
        if (typeof data.jsFunctions === 'string' && data.jsFunctions.trim().length > 0) {
          (0, eval)(data.jsFunctions);
        }
        if (typeof data.jsExpressions === 'string' && data.jsExpressions.trim().length > 0) {
          (0, eval)('(async () => {\\n' + data.jsExpressions + '\\n})()');
        }
      } catch (err) {
        console.error('[UmweltSandbox] Script execution error:', err);
      }
      if (typeof window.__umweltReportHeight === 'function') {
        window.__umweltReportHeight();
      }
    }
  });
})();
`.trim();

/**
 * Generates an inline script expression that morphs `document.body` (or `#umwelt-sandbox-root`)
 * using `Idiomorph.morph` with `morphStyle: 'innerHTML'` and a safe `innerHTML` fallback.
 *
 * @param body - Sanitized HTML body content to morph into the frame.
 * @returns Executable JavaScript snippet string.
 */
export function buildPreviewBodyMorph(body: string): string {
  return `(function() {
  var html = ${JSON.stringify(body)};
  var target = document.getElementById('umwelt-sandbox-root') || document.body;
  if (typeof Idiomorph !== "undefined" && Idiomorph && Idiomorph.morph) {
    try {
      Idiomorph.morph(target, html, {
        morphStyle: 'innerHTML',
        callbacks: {
          beforeNodeAdded: function(node) {
            if (node.nodeType === 1) {
              node.classList.add('morph-enter');
              node.addEventListener('animationend', function() {
                node.classList.remove('morph-enter');
              }, { once: true });
            }
          }
        }
      });
    } catch (err) {
      target.innerHTML = html;
    }
  } else {
    target.innerHTML = html;
  }
})();`;
}

/**
 * Builds the `<head>` inner HTML for a streaming preview sandbox.
 *
 * @param css - Optional widget-specific CSS string.
 * @param previewStyles - Optional concatenated `<style>` blocks extracted from partial HTML.
 * @param theme - Active Umwelt-OS theme (`'light'` or `'dark'`).
 * @returns HTML string containing `<style>` blocks for the preview `<head>`.
 */
export function buildPreviewHeadContent(
  css?: string,
  previewStyles?: string,
  theme: SandboxTheme = 'light',
): string {
  const parts = [
    '<style>html, body { overflow: hidden !important; }</style>',
    `<style id="umwelt-design-system">${escapeStyleTagContent(buildSandboxStylesheet(theme))}</style>`,
  ];
  if (css) {
    parts.push(`<style id="umwelt-widget-css">${escapeStyleTagContent(css)}</style>`);
  }
  if (previewStyles) {
    parts.push(previewStyles);
  }
  return parts.join('');
}

export interface FrameDocumentOptions {
  /** Raw or partial HTML markup for the widget body. */
  html?: string;
  /** Widget-specific CSS rules. */
  css?: string;
  /** Top-level JavaScript function declarations to execute when `executeScripts` is true. */
  jsFunctions?: string;
  /** JavaScript expressions to execute inside an async IIFE when `executeScripts` is true. */
  jsExpressions?: string;
  /** Active Umwelt-OS theme (`'light'` or `'dark'`). Defaults to `'light'`. */
  theme?: SandboxTheme;
  /** Whether to include and execute `jsFunctions` and `jsExpressions` in the initial document. */
  executeScripts?: boolean;
  /** Optional title for the frame document. */
  title?: string;
}

/**
 * Assembles the complete sandboxed iframe HTML document (`srcdoc`), combining:
 * 1. Strict `CSP_META_TAG` (`default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data: blob:; font-src data:;`)
 * 2. Umwelt-OS + OpenIntelligentUI Design System CSS (`buildSandboxStylesheet(theme)`)
 * 3. Widget-specific CSS (`#umwelt-widget-css`)
 * 4. Idiomorph inline script (`IDIOMORPH_INLINE_SCRIPT`)
 * 5. TripAnimator helper (`TRIP_ANIMATOR_SCRIPT`)
 * 6. `window.sendPrompt` postMessage bridge & `ResizeObserver` height reporter
 * 7. Sanitized HTML body inside `#umwelt-sandbox-root`
 * 8. Optional deferred JS execution (`jsFunctions` and `jsExpressions`) once streaming completes.
 *
 * @param options - `FrameDocumentOptions` configuring HTML, CSS, scripts, and theme.
 * @returns Complete HTML document string for `<iframe srcdoc={...} sandbox="allow-scripts">`.
 */
export function buildFrameDocument(options: FrameDocumentOptions = {}): string {
  const {
    html = '',
    css = '',
    jsFunctions = '',
    jsExpressions = '',
    theme = 'light',
    executeScripts = false,
  } = options;

  const safeBody = processPartialHtml(html);
  const stylesheet = escapeStyleTagContent(buildSandboxStylesheet(theme));
  const customCss = escapeStyleTagContent(css);
  const themeClass = theme === 'dark' ? ' class="dark"' : '';

  const scriptParts: string[] = [];
  if (executeScripts) {
    if (jsFunctions.trim().length > 0) {
      scriptParts.push(escapeScriptTagContent(jsFunctions));
    }
    if (jsExpressions.trim().length > 0) {
      scriptParts.push(
        `(async () => {\n${escapeScriptTagContent(jsExpressions)}\n})();`,
      );
    }
  }

  const userScriptTag =
    scriptParts.length > 0
      ? `\n  <script id="umwelt-widget-js">\n${scriptParts.join('\n')}\n  </script>`
      : '';

  return `<!DOCTYPE html>
<html lang="en" data-theme="${theme}"${themeClass}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  ${CSP_META_TAG}
  <style>html, body { overflow: hidden !important; height: auto !important; min-height: 0 !important; }</style>
  <style id="umwelt-design-system">${stylesheet}</style>
  <style id="umwelt-widget-css">${customCss}</style>
  <script>${escapeScriptTagContent(IDIOMORPH_INLINE_SCRIPT)}</script>
  ${TRIP_ANIMATOR_SCRIPT}
  <script>${escapeScriptTagContent(SANDBOX_BRIDGE_JS)}</script>
</head>
<body>
  <div id="umwelt-sandbox-root" class="initial-render">${safeBody}</div>
  <script>${escapeScriptTagContent(MEASUREMENT_JS)}</script>${userScriptTag}
</body>
</html>`;
}

/**
 * Alias for `buildFrameDocument` matching upstream/contract naming.
 *
 * @param options - `FrameDocumentOptions` configuring HTML, CSS, scripts, and theme.
 * @returns Complete HTML document string for `<iframe srcdoc={...} sandbox="allow-scripts">`.
 */
export const assembleFrameHtml = buildFrameDocument;

/**
 * Builds the final frame content compatible with upstream `buildFinalFrameContent(html, css)`.
 *
 * @param html - Raw HTML content.
 * @param css - Optional widget CSS rules.
 * @returns HTML document string with CSP, importmap, trip animator, and design-system styles in `<head>`.
 */
export function buildFinalFrameContent(html: string, css?: string): string {
  const headContent =
    CSP_META_TAG +
    IMPORTMAP_SCRIPT_TAG +
    TRIP_ANIMATOR_SCRIPT +
    `<style>${escapeStyleTagContent(DESIGN_SYSTEM_CSS)}</style>` +
    (css ? `<style>${escapeStyleTagContent(css)}</style>` : '');
  const withHead = ensureHead(html);
  const openTag = withHead.match(/<head[^>]*>/i);
  if (!openTag || openTag.index === undefined) {
    return `<head>${headContent}</head>${withHead}`;
  }
  const insertAt = openTag.index + openTag[0].length;
  return withHead.slice(0, insertAt) + headContent + withHead.slice(insertAt);
}
