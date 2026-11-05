/**
 * Widget frame shell contract shared by the `/genui-frame` route and the generative-UI frame builder. Kept in `core`
 * (pure strings, no React) so the route handler does not import module internals. Docs: docs/security.md.
 */

/** Same-origin path of the frame shell route (`app/genui-frame/route.ts`) that every widget iframe loads. */
export const FRAME_SHELL_PATH = '/genui-frame';

/**
 * Builds the CSP *header* value of the frame shell route. It carries the same directives as the in-document meta tag
 * (see `buildCspMetaTag`) plus `frame-ancestors 'self'`. The shell is a real URL response, so it gets its own policy
 * container instead of inheriting the host's nonce policy the way `srcdoc`/`data:`/`blob:` documents do; that is what
 * lets widget inline event handlers and the `unsafe-inline` frame scripts run while the app keeps no script `unsafe-inline`.
 *
 * `sandbox allow-scripts` (header-only; invalid in a meta tag) forces an opaque origin even when the URL is opened top-level,
 * so a `window.name` planted by an attacker page can never execute in the app origin.
 *
 * @param origin - App origin hosting `/vendor/genui/` (the sandboxed frame is opaque-origin, so `'self'` is not usable for it).
 * @returns The `Content-Security-Policy` header value.
 */
export function buildFrameShellCsp(origin: string): string {
  const vendor = origin ? ` ${origin}/vendor/genui/` : '';
  return `default-src 'none'; script-src 'unsafe-inline'${vendor}; style-src 'unsafe-inline'${vendor}; img-src data: blob:; font-src data:; connect-src 'none'; frame-ancestors 'self'; sandbox allow-scripts`;
}

/**
 * Inline script of the frame shell: replaces the shell document with the widget document the host handed over in the
 * iframe's `name` attribute (survives navigation, readable only by the frame itself) and clears the name afterwards.
 * Top-level (unframed) loads only clear the name and write nothing (defence in depth next to the CSP `sandbox`).
 * `document.write` runs the widget's inline scripts under the shell's header CSP; the `load` event fires once, after it.
 */
export const FRAME_SHELL_SCRIPT =
  "var d=window.name;window.name='';if(window.top!==window){document.open();document.write(d);document.close();}";
