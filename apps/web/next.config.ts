import type { NextConfig } from 'next';
import createNextIntlPlugin from 'next-intl/plugin';

/**
 * The Content-Security-Policy is deliberately NOT set here: src/proxy.ts emits it per request with a nonce
 * (src/core/csp.ts), and the Caddyfile must not add one (two CSP headers intersect). Docs: docs/security.md.
 */
const securityHeaders = [
  // HSTS is deliberately not set here: the TLS terminator (infrastructure/proxy/Caddyfile, or the tunnel) owns it so there is one value.
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=(self), payment=(), usb=()' },
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
