import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { buildSandboxStylesheet } from '../design-system';
import { resolveTerminalPalette, TERMINAL_MONO_FONT_STACK } from '../sandbox/terminal-palette';

describe('resolveTerminalPalette', () => {
  beforeEach(() => {
    document.documentElement.setAttribute('data-skin', 'terminal');
    document.documentElement.style.setProperty('--surface', '#0f141a');
    document.documentElement.style.setProperty('--text', '#c9d1d9');
    document.documentElement.style.setProperty('--accent', '#3fb950');
  });
  afterEach(() => {
    document.documentElement.removeAttribute('data-skin');
    document.documentElement.removeAttribute('style');
  });

  it('returns an empty palette outside the terminal skin so the default skin is unchanged', () => {
    document.documentElement.removeAttribute('data-skin');
    expect(resolveTerminalPalette(document.documentElement)).toEqual({});
    expect(resolveTerminalPalette(null)).toEqual({});
  });

  it('reads the host semantic tokens as computed custom properties under the terminal skin', () => {
    const palette = resolveTerminalPalette(document.documentElement);
    expect(palette['--surface']).toBe('#0f141a');
    expect(palette['--text']).toBe('#c9d1d9');
    expect(palette['--accent']).toBe('#3fb950');
  });

  it('routes the derived colour aliases through the sandbox semantic tokens', () => {
    const palette = resolveTerminalPalette(document.documentElement);
    expect(palette['--color-background-primary']).toBe('var(--surface)');
    expect(palette['--color-text-primary']).toBe('var(--text)');
  });

  it('passes the terminal mono stack for both the sans and mono font variables', () => {
    const palette = resolveTerminalPalette(document.documentElement);
    expect(palette['--font-sans']).toBe(TERMINAL_MONO_FONT_STACK);
    expect(palette['--font-mono']).toBe(TERMINAL_MONO_FONT_STACK);
  });

  it('drops host values that could escape the declaration or load a resource', () => {
    document.documentElement.style.setProperty('--surface', 'url(https://example.test/x.png)');
    document.documentElement.style.setProperty('--text', '#fff; } body { display:none');
    const palette = resolveTerminalPalette(document.documentElement);
    expect(palette['--surface']).toBeUndefined();
    expect(palette['--text']).toBeUndefined();
  });

  it('adds the palette as a final override block so it beats the dark theme rules', () => {
    const css = buildSandboxStylesheet('dark', resolveTerminalPalette(document.documentElement));
    expect(css.endsWith('}')).toBe(true);
    expect(css).toContain(':root:root {\n  --surface: #0f141a;');
    expect(css.indexOf(':root:root {')).toBeGreaterThan(css.indexOf('html.dark'));
  });

  it('leaves the theme stylesheet unchanged when no palette is passed', () => {
    expect(buildSandboxStylesheet('dark')).not.toContain(':root:root');
    expect(buildSandboxStylesheet('dark', {})).toBe(buildSandboxStylesheet('dark'));
  });
});
