import { buildFrameShellCsp, FRAME_SHELL_SCRIPT } from '@/core/genui-frame';

/** Static for the lifetime of a deployment apart from the origin-dependent CSP header, so never cached by shared caches. */
export const dynamic = 'force-dynamic';

/**
 * Serves the generative-UI frame shell: a head-less/body-less document (an explicit head or body makes Chromium treat the written `<meta>` CSP as outside `<head>` and warn) whose only job is to replace itself with the widget document
 * passed in `window.name` (see `FRAME_SHELL_SCRIPT`). Because it is a real response it carries its OWN
 * `Content-Security-Policy` (widget policy, `buildFrameShellCsp`) rather than inheriting the app's nonce policy, so the
 * app `script-src` needs no `'unsafe-inline'` and no host nonce ever enters a frame.
 *
 * Unauthenticated by design: it is excluded from `proxy.ts` because a sandboxed (opaque-origin) frame sends no cookies,
 * and the response contains no user data. Only the embedding app origin may frame it (`frame-ancestors 'self'`).
 *
 * @param request - Incoming request; its origin names the `/vendor/genui/` source allowed by the CSP.
 * @returns The shell HTML response.
 */
export function GET(request: Request): Response {
  return new Response(`<!doctype html><script>${FRAME_SHELL_SCRIPT}</script>`, {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Security-Policy': buildFrameShellCsp(new URL(request.url).origin),
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer',
      'Cache-Control': 'no-store',
    },
  });
}
