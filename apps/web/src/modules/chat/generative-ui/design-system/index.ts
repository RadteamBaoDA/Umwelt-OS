/**
 * Unified Design System injector for Umwelt-OS Generative UI (`design-system/`).
 * Bridges Umwelt-OS semantic CSS tokens (`globals.css` & `docs/design-system/tokens.json`)
 * with OpenIntelligentUI's widget token vocabulary (`--color-background-primary`,
 * `--color-text-primary`, SVG classes, form controls, and stagger animations) so
 * sandboxed iframes and standalone HTML exports render natively in both light and dark themes.
 */

export type SandboxTheme = 'light' | 'dark';

/** Canonical Umwelt-OS semantic token names shared with `globals.css` and `tokens.json`. */
export const UMWELT_SEMANTIC_TOKEN_NAMES = [
  'bg',
  'surface',
  'text',
  'muted',
  'line',
  'accent',
  'on-accent',
  'primary-foreground',
  'danger',
  'status-success',
  'status-warning',
  'destructive-foreground',
  'chart-1',
  'chart-2',
  'chart-3',
  'chart-4',
  'chart-5',
] as const;

export type UmweltSemanticTokenName = (typeof UMWELT_SEMANTIC_TOKEN_NAMES)[number];

/**
 * Light-mode semantic and alias token values synchronized with `apps/web/src/app/globals.css` (`:root`)
 * and `docs/design-system/tokens.json`. Provides both bare token keys (`bg`) and CSS custom property
 * keys (`--bg`, `--color-background-primary`) for direct lookup and stylesheet generation.
 */
export const UMWELT_LIGHT_TOKENS = {
  bg: '#f4f5f2',
  surface: '#ffffff',
  text: '#17211c',
  muted: '#69756e',
  line: '#dfe4df',
  accent: '#176a4a',
  'on-accent': '#ffffff',
  'primary-foreground': '#ffffff',
  danger: '#a82e35',
  'status-success': '#176a4a',
  'status-warning': '#a65b00',
  'destructive-foreground': '#ffffff',
  'chart-1': '#176a4a',
  'chart-2': '#2563eb',
  'chart-3': '#d97706',
  'chart-4': '#7c3aed',
  'chart-5': '#0d9488',
  '--bg': '#f4f5f2',
  '--surface': '#ffffff',
  '--text': '#17211c',
  '--muted': '#69756e',
  '--line': '#dfe4df',
  '--accent': '#176a4a',
  '--on-accent': '#ffffff',
  '--primary-foreground': '#ffffff',
  '--danger': '#a82e35',
  '--status-success': '#176a4a',
  '--status-warning': '#a65b00',
  '--destructive-foreground': '#ffffff',
  '--chart-1': '#176a4a',
  '--chart-2': '#2563eb',
  '--chart-3': '#d97706',
  '--chart-4': '#7c3aed',
  '--chart-5': '#0d9488',
  '--color-background-primary': '#ffffff',
  '--color-background-secondary': '#f4f5f2',
  '--color-background-tertiary': '#e8ece8',
  '--color-background-info': '#e8f0fe',
  '--color-background-danger': '#fcebeb',
  '--color-background-success': '#e6f4ed',
  '--color-background-warning': '#faeeda',
  '--color-text-primary': '#17211c',
  '--color-text-secondary': '#69756e',
  '--color-text-tertiary': '#87928b',
  '--color-text-info': '#2563eb',
  '--color-text-danger': '#a82e35',
  '--color-text-success': '#176a4a',
  '--color-text-warning': '#a65b00',
  '--color-border-primary': '#b8c2b9',
  '--color-border-secondary': '#cdd5cd',
  '--color-border-tertiary': '#dfe4df',
  '--color-border-info': '#2563eb',
  '--color-border-danger': '#a82e35',
  '--color-border-success': '#176a4a',
  '--color-border-warning': '#a65b00',
  '--color-accent-primary': '#176a4a',
  '--color-on-accent': '#ffffff',
} as const;

/**
 * Dark-mode semantic and alias token values synchronized with `apps/web/src/app/globals.css` (`html.dark`)
 * and `docs/design-system/tokens.json`. Provides both bare token keys (`bg`) and CSS custom property
 * keys (`--bg`, `--color-background-primary`) for direct lookup and stylesheet generation.
 */
export const UMWELT_DARK_TOKENS = {
  bg: '#141a17',
  surface: '#1d2621',
  text: '#edf3ee',
  muted: '#a1afa6',
  line: '#35433a',
  accent: '#77c69c',
  'on-accent': '#102117',
  'primary-foreground': '#102117',
  danger: '#ff9b9f',
  'status-success': '#77c69c',
  'status-warning': '#fbbf24',
  'destructive-foreground': '#141a17',
  'chart-1': '#77c69c',
  'chart-2': '#60a5fa',
  'chart-3': '#f59e0b',
  'chart-4': '#a78bfa',
  'chart-5': '#2dd4bf',
  '--bg': '#141a17',
  '--surface': '#1d2621',
  '--text': '#edf3ee',
  '--muted': '#a1afa6',
  '--line': '#35433a',
  '--accent': '#77c69c',
  '--on-accent': '#102117',
  '--primary-foreground': '#102117',
  '--danger': '#ff9b9f',
  '--status-success': '#77c69c',
  '--status-warning': '#fbbf24',
  '--destructive-foreground': '#141a17',
  '--chart-1': '#77c69c',
  '--chart-2': '#60a5fa',
  '--chart-3': '#f59e0b',
  '--chart-4': '#a78bfa',
  '--chart-5': '#2dd4bf',
  '--color-background-primary': '#1d2621',
  '--color-background-secondary': '#141a17',
  '--color-background-tertiary': '#253029',
  '--color-background-info': '#172b46',
  '--color-background-danger': '#421c20',
  '--color-background-success': '#153324',
  '--color-background-warning': '#3d2a0d',
  '--color-text-primary': '#edf3ee',
  '--color-text-secondary': '#a1afa6',
  '--color-text-tertiary': '#76857c',
  '--color-text-info': '#60a5fa',
  '--color-text-danger': '#ff9b9f',
  '--color-text-success': '#77c69c',
  '--color-text-warning': '#fbbf24',
  '--color-border-primary': '#526659',
  '--color-border-secondary': '#435449',
  '--color-border-tertiary': '#35433a',
  '--color-border-info': '#60a5fa',
  '--color-border-danger': '#ff9b9f',
  '--color-border-success': '#77c69c',
  '--color-border-warning': '#fbbf24',
  '--color-accent-primary': '#77c69c',
  '--color-on-accent': '#102117',
} as const;

/**
 * Mapping of OpenIntelligentUI CSS variable names to Umwelt-OS semantic CSS custom properties.
 * Ensures any skill-generated widget referencing `--color-background-primary`, `--color-text-primary`,
 * or `--color-accent-primary` automatically inherits the active Umwelt-OS theme palette.
 */
export const OPEN_INTELLIGENT_UI_TOKEN_ALIASES: Readonly<Record<string, string>> = {
  '--color-background-primary': 'var(--surface)',
  '--color-background-secondary': 'var(--bg)',
  '--color-background-tertiary': 'color-mix(in srgb, var(--bg) 70%, var(--line))',
  '--color-background-info': 'color-mix(in srgb, var(--chart-2) 14%, var(--surface))',
  '--color-background-danger': 'color-mix(in srgb, var(--danger) 14%, var(--surface))',
  '--color-background-success': 'color-mix(in srgb, var(--status-success) 14%, var(--surface))',
  '--color-background-warning': 'color-mix(in srgb, var(--status-warning) 14%, var(--surface))',
  '--color-text-primary': 'var(--text)',
  '--color-text-secondary': 'var(--muted)',
  '--color-text-tertiary': 'color-mix(in srgb, var(--muted) 75%, var(--bg))',
  '--color-text-info': 'var(--chart-2)',
  '--color-text-danger': 'var(--danger)',
  '--color-text-success': 'var(--status-success)',
  '--color-text-warning': 'var(--status-warning)',
  '--color-border-primary': 'color-mix(in srgb, var(--text) 32%, var(--line))',
  '--color-border-secondary': 'color-mix(in srgb, var(--text) 18%, var(--line))',
  '--color-border-tertiary': 'var(--line)',
  '--color-border-info': 'var(--chart-2)',
  '--color-border-danger': 'var(--danger)',
  '--color-border-success': 'var(--status-success)',
  '--color-border-warning': 'var(--status-warning)',
  '--color-accent-primary': 'var(--accent)',
  '--color-on-accent': 'var(--primary-foreground)',
};

/**
 * Formats a token dictionary into CSS custom property declarations for a given selector.
 *
 * @param tokens - Token dictionary (`UMWELT_LIGHT_TOKENS` or `UMWELT_DARK_TOKENS`).
 * @returns Indented CSS custom property declarations.
 */
function formatTokenBlock(tokens: typeof UMWELT_LIGHT_TOKENS | typeof UMWELT_DARK_TOKENS): string {
  const cssEntries = Object.entries(tokens).filter(([key]) => key.startsWith('--'));
  return cssEntries.map(([key, value]) => `  ${key}: ${value};`).join('\n');
}

const SHARED_ALIAS_DECLARATIONS = `
  --color-background: var(--surface);
  --color-surface: var(--surface);
  --color-card: var(--surface);
  --color-card-foreground: var(--text);
  --color-foreground: var(--text);
  --color-muted: var(--bg);
  --color-muted-foreground: var(--muted);
  --color-border: var(--line);
  --color-input: var(--line);
  --color-ring: var(--accent);
  --color-primary: var(--accent);
  --color-primary-foreground: var(--primary-foreground);
  --color-secondary: var(--bg);
  --color-secondary-foreground: var(--text);
  --color-accent: var(--bg);
  --color-accent-foreground: var(--text);
  --color-destructive: var(--danger);
  --color-destructive-foreground: var(--destructive-foreground);
  --color-chart-1: var(--chart-1);
  --color-chart-2: var(--chart-2);
  --color-chart-3: var(--chart-3);
  --color-chart-4: var(--chart-4);
  --color-chart-5: var(--chart-5);

  --font-sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-serif: Georgia, "Times New Roman", serif;
  --font-mono: "SF Mono", "Fira Code", "Fira Mono", ui-monospace, monospace;

  --radius-sm: 6px;
  --radius-md: 9px;
  --radius-lg: 12px;
  --radius-card: 10px;
  --radius-dialog: 16px;
  --radius-panel: 18px;
  --border-radius-md: 9px;
  --border-radius-lg: 12px;
  --border-radius-xl: 18px;

  --p: var(--color-text-primary);
  --s: var(--color-text-secondary);
  --t: var(--color-text-tertiary);
  --bg2: var(--color-background-secondary);
  --b: var(--color-border-tertiary);
`.trim();

/**
 * Theme CSS defining Umwelt-OS light and dark semantic tokens alongside OpenIntelligentUI aliases.
 * Supports `:root`, `html.dark`, `[data-theme="dark"]`, and `@media (prefers-color-scheme: dark)`.
 */
export const THEME_CSS = `
:root, html[data-theme="light"] {
  color-scheme: light;
${formatTokenBlock(UMWELT_LIGHT_TOKENS)}
  ${SHARED_ALIAS_DECLARATIONS}
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
${formatTokenBlock(UMWELT_DARK_TOKENS)}
  }
}

html.dark, html[data-theme="dark"], body.dark {
  color-scheme: dark;
${formatTokenBlock(UMWELT_DARK_TOKENS)}
}
`.trim();

/** Pre-built SVG diagram and color-ramp classes for generative visualizations. */
export const SVG_CLASSES_CSS = `
svg text.t   { font: 400 14px var(--font-sans); fill: var(--p); }
svg text.ts  { font: 400 12px var(--font-sans); fill: var(--s); }
svg text.th  { font: 600 14px var(--font-sans); fill: var(--p); }

svg .box > rect, svg .box > circle, svg .box > ellipse { fill: var(--bg2); stroke: var(--b); }
svg .node { cursor: pointer; }
svg .node:hover { opacity: 0.85; }
svg .arr { stroke: var(--s); stroke-width: 1.5; fill: none; }
svg .leader { stroke: var(--t); stroke-width: 0.5; stroke-dasharray: 4 4; fill: none; }

/* Purple */
svg .c-purple > rect, svg .c-purple > circle, svg .c-purple > ellipse,
svg rect.c-purple, svg circle.c-purple, svg ellipse.c-purple { fill: #EEEDFE; stroke: #534AB7; }
svg .c-purple text.th, svg .c-purple text.t { fill: #3C3489; }
svg .c-purple text.ts { fill: #534AB7; }

/* Teal (Umwelt accent family) */
svg .c-teal > rect, svg .c-teal > circle, svg .c-teal > ellipse,
svg rect.c-teal, svg circle.c-teal, svg ellipse.c-teal { fill: #E1F5EE; stroke: #176a4a; }
svg .c-teal text.th, svg .c-teal text.t { fill: #085041; }
svg .c-teal text.ts { fill: #176a4a; }

/* Coral */
svg .c-coral > rect, svg .c-coral > circle, svg .c-coral > ellipse,
svg rect.c-coral, svg circle.c-coral, svg ellipse.c-coral { fill: #FAECE7; stroke: #993C1D; }
svg .c-coral text.th, svg .c-coral text.t { fill: #712B13; }
svg .c-coral text.ts { fill: #993C1D; }

/* Pink */
svg .c-pink > rect, svg .c-pink > circle, svg .c-pink > ellipse,
svg rect.c-pink, svg circle.c-pink, svg ellipse.c-pink { fill: #FBEAF0; stroke: #993556; }
svg .c-pink text.th, svg .c-pink text.t { fill: #72243E; }
svg .c-pink text.ts { fill: #993556; }

/* Gray */
svg .c-gray > rect, svg .c-gray > circle, svg .c-gray > ellipse,
svg rect.c-gray, svg circle.c-gray, svg ellipse.c-gray { fill: #f4f5f2; stroke: #69756e; }
svg .c-gray text.th, svg .c-gray text.t { fill: #17211c; }
svg .c-gray text.ts { fill: #69756e; }

/* Blue */
svg .c-blue > rect, svg .c-blue > circle, svg .c-blue > ellipse,
svg rect.c-blue, svg circle.c-blue, svg ellipse.c-blue { fill: #E6F1FB; stroke: #2563eb; }
svg .c-blue text.th, svg .c-blue text.t { fill: #0C447C; }
svg .c-blue text.ts { fill: #2563eb; }

/* Green */
svg .c-green > rect, svg .c-green > circle, svg .c-green > ellipse,
svg rect.c-green, svg circle.c-green, svg ellipse.c-green { fill: #EAF3DE; stroke: #176a4a; }
svg .c-green text.th, svg .c-green text.t { fill: #173404; }
svg .c-green text.ts { fill: #176a4a; }

/* Amber */
svg .c-amber > rect, svg .c-amber > circle, svg .c-amber > ellipse,
svg rect.c-amber, svg circle.c-amber, svg ellipse.c-amber { fill: #FAEEDA; stroke: #d97706; }
svg .c-amber text.th, svg .c-amber text.t { fill: #633806; }
svg .c-amber text.ts { fill: #a65b00; }

/* Red */
svg .c-red > rect, svg .c-red > circle, svg .c-red > ellipse,
svg rect.c-red, svg circle.c-red, svg ellipse.c-red { fill: #FCEBEB; stroke: #a82e35; }
svg .c-red text.th, svg .c-red text.t { fill: #791F1F; }
svg .c-red text.ts { fill: #a82e35; }

html.dark svg .c-purple > rect, html.dark svg .c-purple > circle, html.dark svg .c-purple > ellipse,
html.dark svg rect.c-purple, html.dark svg circle.c-purple, html.dark svg ellipse.c-purple { fill: #26215C; stroke: #a78bfa; }
html.dark svg .c-purple text.th, html.dark svg .c-purple text.t { fill: #CECBF6; }
html.dark svg .c-purple text.ts { fill: #a78bfa; }

html.dark svg .c-teal > rect, html.dark svg .c-teal > circle, html.dark svg .c-teal > ellipse,
html.dark svg rect.c-teal, html.dark svg circle.c-teal, html.dark svg ellipse.c-teal { fill: #153324; stroke: #77c69c; }
html.dark svg .c-teal text.th, html.dark svg .c-teal text.t { fill: #edf3ee; }
html.dark svg .c-teal text.ts { fill: #77c69c; }

html.dark svg .c-gray > rect, html.dark svg .c-gray > circle, html.dark svg .c-gray > ellipse,
html.dark svg rect.c-gray, html.dark svg circle.c-gray, html.dark svg ellipse.c-gray { fill: #1d2621; stroke: #a1afa6; }
html.dark svg .c-gray text.th, html.dark svg .c-gray text.t { fill: #edf3ee; }
html.dark svg .c-gray text.ts { fill: #a1afa6; }

html.dark svg .c-blue > rect, html.dark svg .c-blue > circle, html.dark svg .c-blue > ellipse,
html.dark svg rect.c-blue, html.dark svg circle.c-blue, html.dark svg ellipse.c-blue { fill: #172b46; stroke: #60a5fa; }
html.dark svg .c-blue text.th, html.dark svg .c-blue text.t { fill: #B5D4F4; }
html.dark svg .c-blue text.ts { fill: #60a5fa; }

html.dark svg .c-green > rect, html.dark svg .c-green > circle, html.dark svg .c-green > ellipse,
html.dark svg rect.c-green, html.dark svg circle.c-green, html.dark svg ellipse.c-green { fill: #153324; stroke: #77c69c; }
html.dark svg .c-green text.th, html.dark svg .c-green text.t { fill: #C0DD97; }
html.dark svg .c-green text.ts { fill: #77c69c; }

html.dark svg .c-amber > rect, html.dark svg .c-amber > circle, html.dark svg .c-amber > ellipse,
html.dark svg rect.c-amber, html.dark svg circle.c-amber, html.dark svg ellipse.c-amber { fill: #3d2a0d; stroke: #fbbf24; }
html.dark svg .c-amber text.th, html.dark svg .c-amber text.t { fill: #FAC775; }
html.dark svg .c-amber text.ts { fill: #fbbf24; }

html.dark svg .c-red > rect, html.dark svg .c-red > circle, html.dark svg .c-red > ellipse,
html.dark svg rect.c-red, html.dark svg circle.c-red, html.dark svg ellipse.c-red { fill: #421c20; stroke: #ff9b9f; }
html.dark svg .c-red text.th, html.dark svg .c-red text.t { fill: #F7C1C1; }
html.dark svg .c-red text.ts { fill: #ff9b9f; }
`.trim();

/** Initial-render stagger and morph-enter animation rules for streaming widgets. */
export const INITIAL_RENDER_STAGGER_CSS = `
/* First render: stagger all children */
#content.initial-render > * {
  animation: fadeSlideIn 0.4s ease-out both;
}
#content.initial-render > *:nth-child(1) { animation-delay: 0s; }
#content.initial-render > *:nth-child(2) { animation-delay: 0.06s; }
#content.initial-render > *:nth-child(3) { animation-delay: 0.12s; }
#content.initial-render > *:nth-child(4) { animation-delay: 0.18s; }
#content.initial-render > *:nth-child(5) { animation-delay: 0.24s; }
#content.initial-render > *:nth-child(n+6) { animation-delay: 0.3s; }

/* Subsequent morphs: only new elements animate in */
.morph-enter {
  animation: fadeSlideIn 0.4s ease-out both;
}
`.trim();

/** Form element baseline styles and stagger animations aligned with Umwelt-OS controls. */
export const FORM_STYLES_WITH_STAGGER_CSS = `
* { box-sizing: border-box; margin: 0; }

html { background: transparent; }

body {
  font-family: var(--font-sans);
  font-size: 15px;
  line-height: 1.6;
  color: var(--color-text-primary);
  background: transparent;
  -webkit-font-smoothing: antialiased;
}

button {
  font-family: inherit;
  font-size: 14px;
  font-weight: 600;
  min-height: 36px;
  padding: 6px 14px;
  border: 1px solid var(--color-border-tertiary);
  border-radius: var(--border-radius-md);
  background: var(--color-background-primary);
  color: var(--color-text-primary);
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, transform 0.1s;
}
button:hover {
  background: var(--color-background-secondary);
  border-color: var(--color-border-secondary);
}
button:focus-visible {
  outline: 3px solid color-mix(in srgb, var(--accent) 40%, transparent);
  outline-offset: 2px;
}
button:active { transform: scale(0.98); }

input[type="text"],
input[type="number"],
input[type="email"],
input[type="search"],
textarea,
select {
  font-family: inherit;
  font-size: 14px;
  padding: 6px 12px;
  min-height: 36px;
  border: 1px solid var(--color-border-tertiary);
  border-radius: var(--border-radius-md);
  background: var(--color-background-secondary);
  color: var(--color-text-primary);
  transition: border-color 0.15s;
}
input:hover, textarea:hover, select:hover { border-color: var(--color-border-secondary); }
input:focus, textarea:focus, select:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 25%, transparent);
}
textarea { height: auto; min-height: 80px; resize: vertical; }
input::placeholder, textarea::placeholder { color: var(--color-text-tertiary); }

input[type="range"] {
  -webkit-appearance: none;
  appearance: none;
  height: 4px;
  background: var(--color-border-tertiary);
  border-radius: 2px;
  border: none;
  outline: none;
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--color-background-primary);
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.16);
}
input[type="range"]::-moz-range-thumb {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--color-background-primary);
  cursor: pointer;
}

input[type="checkbox"], input[type="radio"] {
  width: 16px;
  height: 16px;
  accent-color: var(--accent);
}

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

${INITIAL_RENDER_STAGGER_CSS}

@keyframes fadeSlideIn {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
`.trim();

/** Form styles alias matching upstream `@repo/design-system` exports. */
export const FORM_STYLES_CSS = FORM_STYLES_WITH_STAGGER_CSS;

/** Combined Design System CSS string injected into sandboxed iframes and standalone HTML exports. */
export const DESIGN_SYSTEM_CSS = `${THEME_CSS}\n\n${SVG_CLASSES_CSS}\n\n${FORM_STYLES_WITH_STAGGER_CSS}`;

/** CDN import map for ES modules inside standalone exports. */
export const IMPORTMAP = {
  imports: {
    three: 'https://esm.sh/three',
    'three/': 'https://esm.sh/three/',
    gsap: 'https://esm.sh/gsap',
    'gsap/': 'https://esm.sh/gsap/',
    d3: 'https://esm.sh/d3',
    'd3/': 'https://esm.sh/d3/',
    'chart.js': 'https://esm.sh/chart.js',
    'chart.js/': 'https://esm.sh/chart.js/',
  },
} as const satisfies { imports: Record<string, string> };

/** Pre-rendered `<script type="importmap">` tag for HTML head injection. */
export const IMPORTMAP_SCRIPT_TAG = `<script type="importmap">
${JSON.stringify(IMPORTMAP, null, 2)
  .split('\n')
  .map((line) => '  ' + line)
  .join('\n')}
  </script>`;

/** Agent system prompt snippet describing the pre-loaded Umwelt-OS + OpenIntelligentUI design system. */
export const OPEN_GEN_UI_DESIGN_SKILL = `
The sandbox already includes the Umwelt-OS & OpenIntelligentUI unified design system. Build on it instead of restyling from scratch.

Colors and theming:
- Use semantic CSS variables for every color: --bg, --surface, --text, --muted, --line, --accent, --danger, --chart-1..5, or the OpenIntelligentUI aliases --color-background-primary / --color-background-secondary / --color-background-tertiary, --color-text-primary / --color-text-secondary / --color-text-tertiary, --color-border-primary / --color-border-secondary / --color-border-tertiary, --color-accent-primary, plus semantic info/danger/success/warning variants.
- Dark mode is automatic via these variables - NEVER hardcode hex values for text or backgrounds.
- Fonts: --font-sans, --font-serif, --font-mono. Radii: --border-radius-md (9px), --border-radius-lg (12px), --border-radius-xl (18px).

Form controls:
- button, input (text/number/email/search/range/checkbox/radio), textarea, and select are pre-styled by the design system.

SVG diagrams and charts:
- Pre-built color-ramp classes: .c-purple .c-teal .c-coral .c-pink .c-gray .c-blue .c-green .c-amber .c-red.
- Diagram helpers: .box .node .arr .leader for shapes/connectors, and .t .ts .th for text (body/small/heading).
`.trim();

/**
 * Builds the complete sandbox stylesheet for a specific active theme (`light` or `dark`),
 * pinning `:root` to the active theme's tokens so the sandboxed iframe matches the host's
 * explicit theme preference even when the OS `prefers-color-scheme` differs.
 *
 * @param theme - Active Umwelt-OS theme (`'light'` or `'dark'`). Defaults to `'light'`.
 * @returns Complete CSS stylesheet string ready for `<style>` injection.
 */
export function buildSandboxStylesheet(theme: SandboxTheme = 'light'): string {
  const activeTokens = theme === 'dark' ? UMWELT_DARK_TOKENS : UMWELT_LIGHT_TOKENS;
  const activeRootCss = `:root {\n  color-scheme: ${theme};\n${formatTokenBlock(activeTokens)}\n  ${SHARED_ALIAS_DECLARATIONS}\n}`;
  return `${activeRootCss}\n\n${THEME_CSS}\n\n${SVG_CLASSES_CSS}\n\n${FORM_STYLES_WITH_STAGGER_CSS}`;
}

/**
 * Extracts CSS custom property definitions (`--name: value`) from a specific selector block
 * (`:root` or `html.dark`) inside a CSS source file such as `apps/web/src/app/globals.css`.
 *
 * @param cssSource - Raw CSS file content.
 * @param selector - Target selector (`':root'` or `'html.dark'`).
 * @returns Dictionary mapping `--token` (and bare `token`) to its normalized value.
 */
export function extractCssVariables(
  cssSource: string,
  selector: ':root' | 'html.dark',
): Record<string, string> {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const blockRegex = new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`, 'm');
  const match = cssSource.match(blockRegex);
  if (!match || !match[1]) {
    return {};
  }
  const result: Record<string, string> = {};
  const declRegex = /--([a-zA-Z0-9-]+)\s*:\s*([^;]+);/g;
  let declMatch: RegExpExecArray | null;
  while ((declMatch = declRegex.exec(match[1])) !== null) {
    const name = declMatch[1]!.trim();
    const val = declMatch[2]!.trim().toLowerCase();
    result[name] = val;
    result[`--${name}`] = val;
  }
  return result;
}

/**
 * Verifies that `UMWELT_LIGHT_TOKENS` and `UMWELT_DARK_TOKENS` match the CSS variables
 * declared in `apps/web/src/app/globals.css` (`:root` and `html.dark`).
 *
 * @param globalsCssContent - Raw text of `apps/web/src/app/globals.css`.
 * @returns Object with `valid` boolean and any `mismatches` found.
 */
export function verifyGlobalsCssParity(globalsCssContent: string): {
  valid: boolean;
  mismatches: string[];
} {
  const lightVars = extractCssVariables(globalsCssContent, ':root');
  const darkVars = extractCssVariables(globalsCssContent, 'html.dark');
  const mismatches: string[] = [];

  for (const [key, value] of Object.entries(lightVars)) {
    if (!key.startsWith('--')) continue;
    const expected = (UMWELT_LIGHT_TOKENS as Record<string, string>)[key]?.toLowerCase();
    if (expected !== value) {
      mismatches.push(`Light token ${key}: globals.css has "${value}", design-system has "${expected ?? 'undefined'}"`);
    }
  }

  for (const [key, value] of Object.entries(darkVars)) {
    if (!key.startsWith('--')) continue;
    const expected = (UMWELT_DARK_TOKENS as Record<string, string>)[key]?.toLowerCase();
    if (expected !== value) {
      mismatches.push(`Dark token ${key}: globals.css has "${value}", design-system has "${expected ?? 'undefined'}"`);
    }
  }

  return { valid: mismatches.length === 0, mismatches };
}

/**
 * Verifies that `UMWELT_LIGHT_TOKENS` and `UMWELT_DARK_TOKENS` match the color token definitions
 * in `docs/design-system/tokens.json`.
 *
 * @param tokensJson - Parsed `docs/design-system/tokens.json` object.
 * @returns Object with `valid` boolean and any `mismatches` found.
 */
export function verifyTokensJsonParity(tokensJson: {
  color?: {
    tokens?: Array<{
      name: string;
      value: string | { light: string; dark: string };
    }>;
  };
}): {
  valid: boolean;
  mismatches: string[];
} {
  const mismatches: string[] = [];
  const tokenList = tokensJson.color?.tokens ?? [];

  for (const entry of tokenList) {
    if (typeof entry.value === 'object' && entry.value !== null) {
      const lightExpected = entry.value.light.toLowerCase();
      const darkExpected = entry.value.dark.toLowerCase();
      const lightActual = (UMWELT_LIGHT_TOKENS as Record<string, string>)[entry.name]?.toLowerCase();
      const darkActual = (UMWELT_DARK_TOKENS as Record<string, string>)[entry.name]?.toLowerCase();

      if (lightActual !== lightExpected) {
        mismatches.push(
          `tokens.json light "${entry.name}": expected "${lightExpected}", got "${lightActual ?? 'undefined'}"`,
        );
      }
      if (darkActual !== darkExpected) {
        mismatches.push(
          `tokens.json dark "${entry.name}": expected "${darkExpected}", got "${darkActual ?? 'undefined'}"`,
        );
      }
    }
  }

  return { valid: mismatches.length === 0, mismatches };
}
