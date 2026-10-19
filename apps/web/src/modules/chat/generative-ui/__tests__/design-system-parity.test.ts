import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  buildSandboxStylesheet,
  DESIGN_SYSTEM_CSS,
  FORM_STYLES_WITH_STAGGER_CSS,
  IMPORTMAP,
  IMPORTMAP_SCRIPT_TAG,
  OPEN_GEN_UI_DESIGN_SKILL,
  OPEN_INTELLIGENT_UI_TOKEN_ALIASES,
  SVG_CLASSES_CSS,
  THEME_CSS,
  UMWELT_DARK_TOKENS,
  UMWELT_LIGHT_TOKENS,
  UMWELT_SEMANTIC_TOKEN_NAMES,
  verifyGlobalsCssParity,
  verifyTokensJsonParity,
} from '../design-system';

const CURRENT_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(CURRENT_DIR, '../../../../../../..');
const GLOBALS_CSS_PATH = resolve(REPO_ROOT, 'apps/web/src/app/globals.css');
const TOKENS_JSON_PATH = resolve(REPO_ROOT, 'docs/design-system/tokens.json');

describe('Design System Token Parity & Stylesheet Generation', () => {
  it('matches every :root and html.dark token in apps/web/src/app/globals.css', () => {
    const globalsCss = readFileSync(GLOBALS_CSS_PATH, 'utf-8');
    const parity = verifyGlobalsCssParity(globalsCss);
    expect(parity.mismatches).toEqual([]);
    expect(parity.valid).toBe(true);
  });

  it('matches every light/dark token in docs/design-system/tokens.json', () => {
    const tokensJson = JSON.parse(readFileSync(TOKENS_JSON_PATH, 'utf-8'));
    const parity = verifyTokensJsonParity(tokensJson);
    expect(parity.mismatches).toEqual([]);
    expect(parity.valid).toBe(true);
  });

  it('exports all canonical Umwelt-OS semantic tokens in both light and dark maps', () => {
    for (const tokenName of UMWELT_SEMANTIC_TOKEN_NAMES) {
      expect(UMWELT_LIGHT_TOKENS[tokenName]).toMatch(/^#[0-9a-f]{6}$/i);
      expect(UMWELT_DARK_TOKENS[tokenName]).toMatch(/^#[0-9a-f]{6}$/i);
      expect(UMWELT_LIGHT_TOKENS[`--${tokenName}`]).toBe(UMWELT_LIGHT_TOKENS[tokenName]);
      expect(UMWELT_DARK_TOKENS[`--${tokenName}`]).toBe(UMWELT_DARK_TOKENS[tokenName]);
    }
  });

  it('maps OpenIntelligentUI CSS variables to Umwelt-OS tokens in light and dark themes', () => {
    expect(UMWELT_LIGHT_TOKENS['--color-background-primary']).toBe('#ffffff');
    expect(UMWELT_LIGHT_TOKENS['--color-background-secondary']).toBe('#f4f5f2');
    expect(UMWELT_LIGHT_TOKENS['--color-text-primary']).toBe('#17211c');
    expect(UMWELT_LIGHT_TOKENS['--color-text-secondary']).toBe('#69756e');
    expect(UMWELT_LIGHT_TOKENS['--color-accent-primary']).toBe('#176a4a');

    expect(UMWELT_DARK_TOKENS['--color-background-primary']).toBe('#1d2621');
    expect(UMWELT_DARK_TOKENS['--color-background-secondary']).toBe('#141a17');
    expect(UMWELT_DARK_TOKENS['--color-text-primary']).toBe('#edf3ee');
    expect(UMWELT_DARK_TOKENS['--color-text-secondary']).toBe('#a1afa6');
    expect(UMWELT_DARK_TOKENS['--color-accent-primary']).toBe('#77c69c');

    expect(OPEN_INTELLIGENT_UI_TOKEN_ALIASES['--color-background-primary']).toBe('var(--surface)');
    expect(OPEN_INTELLIGENT_UI_TOKEN_ALIASES['--color-text-primary']).toBe('var(--text)');
  });

  it('generates light and dark sandbox stylesheets with SVG classes and stagger animations', () => {
    const lightCss = buildSandboxStylesheet('light');
    const darkCss = buildSandboxStylesheet('dark');

    expect(lightCss).toContain('color-scheme: light;');
    expect(lightCss).toContain('--bg: #f4f5f2;');
    expect(lightCss).toContain('--color-background-primary: #ffffff;');
    expect(lightCss).toContain('svg text.t');
    expect(lightCss).toContain('@keyframes fadeSlideIn');

    expect(darkCss).toContain('color-scheme: dark;');
    expect(darkCss).toContain('--bg: #141a17;');
    expect(darkCss).toContain('--color-background-primary: #1d2621;');

    expect(THEME_CSS).toContain('@media (prefers-color-scheme: dark)');
    expect(SVG_CLASSES_CSS).toContain('.c-teal');
    expect(FORM_STYLES_WITH_STAGGER_CSS).toContain('.morph-enter');
    expect(DESIGN_SYSTEM_CSS).toContain(THEME_CSS);
  });

  it('exports CDN importmap and design skill prompt documentation', () => {
    expect(IMPORTMAP.imports.three).toContain('https://esm.sh/three');
    expect(IMPORTMAP_SCRIPT_TAG).toContain('<script type="importmap">');
    expect(OPEN_GEN_UI_DESIGN_SKILL).toContain('--color-background-primary');
    expect(OPEN_GEN_UI_DESIGN_SKILL).toContain('.c-teal');
  });
});
