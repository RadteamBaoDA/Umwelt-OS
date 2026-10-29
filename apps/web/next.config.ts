import type { NextConfig } from 'next';
import createNextIntlPlugin from 'next-intl/plugin';

const isDev = process.env.NODE_ENV !== 'production';

/**
 * Content-Security-Policy. Audit of map/globe/video gadgets found no external origins: the globe uses bundled
 * geometry, video items are outbound links only, and realtime uses same-origin EventSource, so everything is 'self'.
 * Relaxations: 'unsafe-inline' for scripts because Next injects inline bootstrap/hydration scripts and a per-request
 * nonce would force every page dynamic; 'unsafe-inline' for styles (React style props, Recharts, Radix); 'unsafe-eval'
 * only in dev (React refresh); data:/blob: images (globe canvas, inline icons); blob: workers (three.js/globe.gl).
 */
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ''}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self'${isDev ? ' ws: wss:' : ''}`,
  "worker-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join('; ');

const securityHeaders = [
  { key: 'Content-Security-Policy', value: csp },
  // HSTS is deliberately not set here: the TLS terminator (infrastructure/proxy/Caddyfile, or the tunnel) owns it so there is one value.
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=(), payment=(), usb=()' },
];

const nextConfig: NextConfig = {
  output: 'standalone',
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- config is CommonJS and needs __dirname
  outputFileTracingRoot: require('node:path').join(__dirname, '../..'),
  outputFileTracingIncludes: { '/*': ['../../docs/legal/**/*.md', '../../docs/third-party-services.md'] },
  // Default proxy timeout is 30 s; brief generate (lease wait + up to 180 s model time) goes through the /api rewrite.
  experimental: { proxyTimeout: 210_000 },
  /** Applies the security headers to every route, including API rewrites' HTML error pages. */
  async headers() {
    return [
      { source: '/:path*', headers: securityHeaders },
      // Sandboxed (opaque-origin) generative-UI iframes load these modules cross-origin, so they need CORS + CORP.
      {
        source: '/vendor/genui/:path*',
        headers: [
          { key: 'Access-Control-Allow-Origin', value: '*' },
          { key: 'Cross-Origin-Resource-Policy', value: 'cross-origin' },
          { key: 'Cache-Control', value: 'public, max-age=31536000, immutable' },
        ],
      },
    ];
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${process.env.API_INTERNAL_URL ?? 'http://localhost:8000'}/api/:path*`,
      },
      { source: '/health', destination: `${process.env.API_INTERNAL_URL ?? 'http://localhost:8000'}/health` },
    ];
  },
};

const withNextIntl = createNextIntlPlugin('./src/core/i18n-request.ts');

export default withNextIntl(nextConfig);
