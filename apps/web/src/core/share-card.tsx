/** Static copy shared by the share-card image and the root layout metadata (default locale, no user data). */
export const SHARE_TITLE = 'Umwelt-OS';
export const SHARE_TAGLINE = 'Your single-user, self-hosted personal intelligence OS.';
export const SHARE_ALT = `${SHARE_TITLE} - ${SHARE_TAGLINE}`;
export const SHARE_SIZE = { width: 1200, height: 630 } as const;

// Hex values mirror the design system light theme (apps/web/src/app/globals.css, docs/design-system/tokens.json):
// --bg #f4f5f2, --text #17211c, --accent #176a4a. next/og cannot resolve CSS variables.
const BG = '#f4f5f2';
const TEXT = '#17211c';
const ACCENT = '#176a4a';

/**
 * Builds the JSX tree rendered by next/og for the Open Graph and Twitter share image.
 * Pure and static: contains no per-user data because the route is served without authentication.
 * The brand mark reproduces the app icon geometry from src/app/icon.svg.
 * @returns A satori-compatible element at SHARE_SIZE.
 */
export function ShareCard() {
  return (
    <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'center', padding: 96, background: BG, color: TEXT }}>
      <svg width="120" height="120" viewBox="0 0 24 24" fill="none">
        <g transform="rotate(-45 12 12)" stroke={ACCENT} strokeWidth="2.8" strokeLinecap="round">
          <path d="M3.54 8.92A9 9 0 0 1 20.46 8.92" />
          <path d="M20.46 15.08A9 9 0 0 1 3.54 15.08" />
        </g>
        <circle cx="12" cy="12" r="3.2" fill={ACCENT} />
      </svg>
      <div style={{ display: 'flex', fontSize: 96, fontWeight: 700, marginTop: 40, color: ACCENT }}>{SHARE_TITLE}</div>
      <div style={{ display: 'flex', fontSize: 40, marginTop: 16 }}>{SHARE_TAGLINE}</div>
    </div>
  );
}
