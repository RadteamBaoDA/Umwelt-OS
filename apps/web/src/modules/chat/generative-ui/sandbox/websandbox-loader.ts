/**
 * Sandboxed iframe lifecycle controller (`sandbox/websandbox-loader.ts`).
 * Manages `<iframe sandbox="allow-scripts">` initialization, Idiomorph HTML/CSS delta updates,
 * deferred JavaScript execution upon stream completion, and live theme synchronization.
 *
 * Security invariant: The sandbox attribute is strictly locked to `"allow-scripts"` and
 * NEVER includes `"allow-same-origin"`, preventing untrusted widget scripts from accessing
 * parent cookies, storage, or CSRF tokens.
 */

import { buildSandboxStylesheet, type SandboxTheme } from '../design-system';
import { buildFrameDocument, createFrameNonce, type FrameDocumentOptions } from './frame-content';
import { processPartialHtml } from './process-partial-html';

export interface SandboxController {
  /** Managed `<iframe sandbox="allow-scripts">` element. */
  iframe: HTMLIFrameElement;
  /** Sends a sanitized HTML/CSS delta to the iframe for Idiomorph DOM morphing. */
  updateContent: (html: string, css?: string) => void;
  /** Triggers deferred execution of `jsFunctions` and `jsExpressions` inside the iframe. */
  executeScripts: (jsFunctions?: string, jsExpressions?: string) => void;
  /** Synchronizes the active Umwelt-OS theme (`'light'` or `'dark'`) into the iframe. */
  syncTheme: (theme: SandboxTheme) => void;
  /** Cleans up the iframe document and detaches its content. */
  destroy: () => void;
}

/**
 * Enforces the strict `sandbox="allow-scripts"` security policy on an iframe element,
 * stripping `allow-same-origin` if ever present.
 *
 * @param iframe - Target `HTMLIFrameElement`.
 */
export function enforceStrictSandboxAttribute(iframe: HTMLIFrameElement): void {
  iframe.setAttribute('sandbox', 'allow-scripts');
}

/**
 * Reads the per-frame nonce stamped on the iframe (`data-umwelt-nonce`). Every host->frame
 * message must carry it or the in-frame listener drops the message.
 *
 * @param iframe - Target sandbox iframe.
 * @returns The nonce, or `undefined` when none was stamped (messages are then ignored by the frame).
 */
function frameNonce(iframe: HTMLIFrameElement): string | undefined {
  return iframe.dataset.umweltNonce;
}

/**
 * Dispatches a progressive HTML and CSS update to a sandboxed iframe via `postMessage`
 * so the in-frame Idiomorph engine morphs the DOM tree without full-frame reloads.
 *
 * @param iframe - Target `HTMLIFrameElement` (no-op if null).
 * @param html - Raw or partial HTML markup to sanitize and morph.
 * @param css - Optional widget-specific CSS stylesheet.
 */
export function updateSandboxContent(
  iframe: HTMLIFrameElement | null,
  html: string,
  css?: string,
): void {
  if (!iframe) return;
  enforceStrictSandboxAttribute(iframe);

  const sanitizedHtml = processPartialHtml(html);
  iframe.dataset.latestHtml = sanitizedHtml;
  if (typeof css === 'string') {
    iframe.dataset.latestCss = css;
  }

  try {
    iframe.contentWindow?.postMessage(
      {
        type: 'umwelt:morph',
        html: sanitizedHtml,
        css,
        nonce: frameNonce(iframe),
      },
      '*',
    );
  } catch {
    // Ignore transient postMessage errors before iframe contentWindow initializes.
  }
}

/**
 * Executes `jsFunctions` and `jsExpressions` inside the sandboxed iframe once streaming completes.
 * Never executes while `status === 'streaming'` to avoid running truncated JavaScript.
 *
 * @param iframe - Target `HTMLIFrameElement` (no-op if null).
 * @param jsFunctions - Optional top-level function declarations.
 * @param jsExpressions - Optional statement/expression code to run in an async IIFE.
 */
export function executeSandboxScripts(
  iframe: HTMLIFrameElement | null,
  jsFunctions?: string,
  jsExpressions?: string,
): void {
  if (!iframe) return;
  const hasFunctions = typeof jsFunctions === 'string' && jsFunctions.trim().length > 0;
  const hasExpressions = typeof jsExpressions === 'string' && jsExpressions.trim().length > 0;
  if (!hasFunctions && !hasExpressions) return;

  enforceStrictSandboxAttribute(iframe);
  iframe.dataset.scriptsExecuted = 'true';

  try {
    iframe.contentWindow?.postMessage(
      {
        type: 'umwelt:exec-js',
        jsFunctions: hasFunctions ? jsFunctions : undefined,
        jsExpressions: hasExpressions ? jsExpressions : undefined,
        nonce: frameNonce(iframe),
      },
      '*',
    );
  } catch {
    // Ignore transient postMessage errors before iframe contentWindow initializes.
  }
}

/**
 * Synchronizes the host Umwelt-OS theme (`'light'` or `'dark'`) with the sandboxed iframe.
 *
 * @param iframe - Target `HTMLIFrameElement` (no-op if null).
 * @param theme - Active theme (`'light'` or `'dark'`).
 */
export function syncSandboxTheme(
  iframe: HTMLIFrameElement | null,
  theme: SandboxTheme,
): void {
  if (!iframe) return;
  enforceStrictSandboxAttribute(iframe);
  iframe.dataset.theme = theme;

  try {
    iframe.contentWindow?.postMessage(
      {
        type: 'umwelt:theme',
        theme,
        stylesheet: buildSandboxStylesheet(theme),
        nonce: frameNonce(iframe),
      },
      '*',
    );
  } catch {
    // Ignore transient postMessage errors before iframe contentWindow initializes.
  }
}

/**
 * Creates a `SandboxController` bound to an `HTMLIFrameElement`, enforcing `sandbox="allow-scripts"`
 * and optionally writing the initial `srcdoc` frame document.
 *
 * @param iframe - Target `HTMLIFrameElement`.
 * @param initialOptions - Optional `FrameDocumentOptions` to populate `iframe.srcdoc` immediately.
 * @returns `SandboxController` with `updateContent`, `executeScripts`, `syncTheme`, and `destroy`.
 */
export function createSandboxController(
  iframe: HTMLIFrameElement,
  initialOptions?: FrameDocumentOptions,
): SandboxController {
  enforceStrictSandboxAttribute(iframe);

  if (initialOptions) {
    const nonce = initialOptions.nonce ?? createFrameNonce();
    iframe.dataset.umweltNonce = nonce;
    iframe.srcdoc = buildFrameDocument({ ...initialOptions, nonce });
    iframe.dataset.theme = initialOptions.theme ?? 'light';
    if (initialOptions.html) {
      iframe.dataset.latestHtml = processPartialHtml(initialOptions.html);
    }
    if (initialOptions.executeScripts && (initialOptions.jsFunctions || initialOptions.jsExpressions)) {
      iframe.dataset.scriptsExecuted = 'true';
    }
  }

  return {
    iframe,
    updateContent: (html: string, css?: string) => updateSandboxContent(iframe, html, css),
    executeScripts: (jsFunctions?: string, jsExpressions?: string) =>
      executeSandboxScripts(iframe, jsFunctions, jsExpressions),
    syncTheme: (theme: SandboxTheme) => syncSandboxTheme(iframe, theme),
    destroy: () => {
      iframe.srcdoc = '';
      delete iframe.dataset.latestHtml;
      delete iframe.dataset.latestCss;
      delete iframe.dataset.scriptsExecuted;
    },
  };
}
